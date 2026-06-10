"""Module C — 추론 신호: 편입 선행매매 / 리테일 과열 / 크로스에셋 흡수.

모두 inferred. 데이터 미가용 컴포넌트는 추정으로 채우지 않고 'degraded'로 명시(§5).
inferences 테이블에 (name, obs_date, value, confidence, detail) 적재.
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd

from .. import db
from ..config import load_spcx
from .baseline import _load_series


def _zlast(s: pd.Series, window: int = 60, min_obs: int = 10) -> float | None:
    """시리즈 최신값의 롤링 z-score."""
    s = s.dropna()
    if len(s) < min_obs:
        return None
    m = s.rolling(window, min_periods=min_obs).mean()
    sd = s.rolling(window, min_periods=min_obs).std()
    z = (s - m) / sd
    z = z.replace([float("inf"), float("-inf")], pd.NA).dropna()
    return float(z.iloc[-1]) if len(z) else None


def _cum_return(con, ticker: str, lookback: int) -> float | None:
    s = _load_series(con, f"close:{ticker}").dropna()
    if len(s) < 2:
        return None
    s = s.iloc[-(lookback + 1):]
    if len(s) < 2 or s.iloc[0] == 0:
        return None
    return float(s.iloc[-1] / s.iloc[0] - 1.0)


def front_running(con, cfg: dict | None = None, top_n: int = 10) -> dict:
    """편입 선행매매 탐지(§C3).

    A1 funded_sell 상위 종목의 [lookback] 누적수익률을 바스켓(동일가중 평균) 대비 상대화.
    유의한 상대 약세(음의 상대수익률) = 편입 전 선행 매도 추정.
    """
    cfg = cfg or load_spcx()
    lookback = int(cfg.get("observe", {}).get("event_study_window", {}).get("pre", 15))
    now = datetime.now()
    today = now.date()

    tickers = [r[0] for r in con.execute(
        "SELECT ticker FROM funded_sell_by_constituent "
        "ORDER BY as_of_date DESC, est_sell_usd DESC LIMIT ?", [top_n]).fetchall()]
    rets = {t: _cum_return(con, t, lookback) for t in tickers}
    have = {t: r for t, r in rets.items() if r is not None}
    if len(have) < 2:
        return {"status": "degraded", "reason": "구성종목 시세 부족", "signals": []}

    basket = sum(have.values()) / len(have)
    rows, signals = [], []
    for t, r in have.items():
        rel = r - basket
        rows.append({"name": f"front_run_rel:{t}", "obs_date": today, "value": rel,
                     "confidence": "inferred",
                     "detail": f"상대수익률 vs 바스켓 {lookback}거래일 (음수=선행매도 추정)",
                     "created_at": now})
        signals.append({"ticker": t, "rel_return": rel, "abs_return": r})
    db.upsert(con, "inferences", rows, ["name", "obs_date"])
    signals.sort(key=lambda x: x["rel_return"])  # 약한 순(선행매도 의심 상위)
    return {"status": "ok", "basket_return": basket, "lookback": lookback,
            "signals": signals}


def dxyz_retail_gauge(con, cfg: dict | None = None) -> dict:
    """DXYZ 리테일 과열 게이지(§C4).

    NAV(nav:DXYZ) 있으면 프리미엄=(price-NAV)/NAV. 없으면 거래대금/모멘텀 z-score 합성(degraded).
    """
    cfg = cfg or load_spcx()
    now = datetime.now()
    today = now.date()
    price = _load_series(con, "close:DXYZ").dropna()
    nav = _load_series(con, "nav:DXYZ").dropna()

    if len(price) and len(nav):
        common = price.index.intersection(nav.index)
        if len(common):
            d = max(common)
            premium = float(price.loc[d] / nav.loc[d] - 1.0)
            db.upsert(con, "inferences", [{
                "name": "dxyz_premium", "obs_date": d, "value": premium,
                "confidence": "inferred", "detail": "(시장가-NAV)/NAV",
                "created_at": now}], ["name", "obs_date"])
            return {"status": "ok", "metric": "premium", "value": premium, "date": d}

    # 폴백: 거래대금 z + 가격모멘텀 z (NAV 부재 → needs_review)
    zdv = _zlast(_load_series(con, "dollar_volume:DXYZ"))
    ret5 = _cum_return(con, "DXYZ", 5)
    comps = [c for c in (zdv, (ret5 * 10 if ret5 is not None else None)) if c is not None]
    if not comps:
        return {"status": "degraded", "reason": "DXYZ 시세 부족"}
    gauge = sum(comps) / len(comps)
    db.upsert(con, "inferences", [{
        "name": "dxyz_retail_gauge", "obs_date": today, "value": gauge,
        "confidence": "inferred",
        "detail": "NAV 부재 → 거래대금 z + 5일 모멘텀 합성 (proxy, needs_review)",
        "created_at": now}], ["name", "obs_date"])
    return {"status": "proxy", "metric": "gauge_proxy", "value": gauge,
            "note": "NAV(nav:DXYZ) 적재 시 실프리미엄으로 대체"}


def cross_asset_absorption(con, cfg: dict | None = None) -> dict:
    """크로스에셋 흡수 지수(§C5): BTC ETF 순유출 + MMF 유출 + QQQ 환매 결합.

    "IPO가 주변 유동성을 빨아들이는 강도". 가용 컴포넌트만 z-score 표준화 후 합성.
    흡수↑(양수) = 주변 자산에서 자금 이탈(순유출).
    """
    cfg = cfg or load_spcx()
    o = cfg.get("observe", {})
    now = datetime.now()
    today = now.date()

    comps = {}
    missing = []
    # BTC ETF: 순유출이면 흡수↑ → -flow의 z
    for t in o.get("btc_etfs", []):
        z = _zlast(_load_series(con, f"etf_flow_usd:{t}"))
        if z is None:
            missing.append(f"etf_flow_usd:{t}")
        else:
            comps[f"btc_outflow:{t}"] = -z
    # QQQ 환매: -flow의 z
    zq = _zlast(_load_series(con, "etf_flow_usd:QQQ"))
    if zq is None:
        missing.append("etf_flow_usd:QQQ")
    else:
        comps["qqq_redemption"] = -zq
    # MMF 유출(있으면): mmf_flow 시리즈 음수면 유출 → 흡수↑
    zm = _zlast(_load_series(con, "mmf_flow"))
    if zm is None:
        missing.append("mmf_flow")
    else:
        comps["mmf_outflow"] = -zm

    if not comps:
        return {"status": "degraded", "reason": "흡수 컴포넌트 전무", "missing": missing}
    index = sum(comps.values()) / len(comps)
    db.upsert(con, "inferences", [{
        "name": "cross_asset_absorption", "obs_date": today, "value": index,
        "confidence": "inferred",
        "detail": f"컴포넌트 {len(comps)}개 z 평균; 결측 {len(missing)}개",
        "created_at": now}], ["name", "obs_date"])
    return {"status": "ok" if not missing else "partial", "index": index,
            "components": comps, "missing": missing}


def retail_overheating(con, cfg: dict | None = None) -> dict:
    """리테일 과열 게이지(§C4): DXYZ 게이지 + 우주테마 ETF 유입 합성."""
    cfg = cfg or load_spcx()
    o = cfg.get("observe", {})
    now = datetime.now()
    today = now.date()
    comps = {}
    dz = dxyz_retail_gauge(con, cfg)
    if dz.get("value") is not None:
        comps["dxyz"] = dz["value"] if dz.get("metric") != "premium" else dz["value"] * 10
    for t in o.get("thematic_etfs", []):
        z = _zlast(_load_series(con, f"dollar_volume:{t}"))
        if z is not None:
            comps[f"thematic:{t}"] = z
    if not comps:
        return {"status": "degraded", "reason": "리테일 과열 컴포넌트 전무"}
    gauge = sum(comps.values()) / len(comps)
    db.upsert(con, "inferences", [{
        "name": "retail_overheating", "obs_date": today, "value": gauge,
        "confidence": "inferred", "detail": f"컴포넌트 {len(comps)}개 합성",
        "created_at": now}], ["name", "obs_date"])
    return {"status": "ok", "gauge": gauge, "components": comps}
