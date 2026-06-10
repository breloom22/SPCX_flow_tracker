"""Module C — 롤링 z-score 베이스라인 (비정상 탐지).

각 관측 시리즈에 롤링 윈도우(기본 60거래일) 평균/표준편차 기반 z-score를 계산하고
|z| >= 임계(기본 2.0)를 '비정상'으로 플래그한다. 결과는 anomaly_flags 테이블.

해석 가능성 우선(§C6, ML 금지). 가격 레벨은 추세가 있어 z-score가 부적절하므로
close:<T> 는 일별 수익률(ret)로 변환 후 z-score를 매긴다.

모든 산출은 inferred 성격(베이스라인 대비 통계적 비정상이라는 추정).
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd

from .. import db
from ..config import load_spcx

# z-score를 매길 시리즈 prefix (가격 레벨 제외; close는 수익률로 변환)
FLAGGABLE_PREFIXES = ("volume", "dollar_volume", "etf_flow_usd", "shares_out", "aum")


def _load_series(con, series: str) -> pd.Series:
    rows = con.execute(
        "SELECT obs_date, value FROM observations WHERE series=? ORDER BY obs_date",
        [series]).fetchall()
    if not rows:
        return pd.Series(dtype=float)
    idx = [r[0] for r in rows]
    return pd.Series([r[1] for r in rows], index=idx, dtype=float)


def _series_list(con) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT DISTINCT series FROM observations ORDER BY series").fetchall()]


def compute_anomalies(con, cfg: dict | None = None) -> int:
    """모든 적격 시리즈에 z-score 계산 → anomaly_flags upsert. 반환: 기록 행 수."""
    cfg = cfg or load_spcx()
    obs = cfg.get("observe", {})
    window = int(obs.get("zscore_window", 60))
    thr = float(obs.get("zscore_flag_threshold", 2.0))
    now = datetime.now()
    min_obs = max(10, window // 3)  # 최소 관측 수 (윈도우 미충족 시 가용 데이터로)

    rows: list[dict] = []
    for series in _series_list(con):
        prefix = series.split(":", 1)[0]
        s = _load_series(con, series)
        if series.startswith("close:"):
            s = s.pct_change()
            flag_series = "ret:" + series.split(":", 1)[1]
        elif prefix in FLAGGABLE_PREFIXES:
            flag_series = series
        else:
            continue
        s = s.dropna()
        if len(s) < min_obs:
            continue
        roll_mean = s.rolling(window, min_periods=min_obs).mean()
        roll_std = s.rolling(window, min_periods=min_obs).std()
        z = (s - roll_mean) / roll_std
        z = z.replace([float("inf"), float("-inf")], pd.NA).dropna()
        for d, zv in z.items():
            rows.append({
                "series": flag_series, "obs_date": d, "value": float(s.loc[d]),
                "zscore": float(zv), "flagged": bool(abs(zv) >= thr),
                "confidence": "inferred", "created_at": now,
            })
    if rows:
        db.upsert(con, "anomaly_flags", rows, ["series", "obs_date"])
    return len(rows)


def latest_flags(con, limit: int = 20) -> list[dict]:
    """최신 비정상 플래그 (가장 최근 날짜 우선, |z| 큰 순)."""
    rows = con.execute(
        "SELECT series, obs_date, value, zscore FROM anomaly_flags "
        "WHERE flagged ORDER BY obs_date DESC, ABS(zscore) DESC LIMIT ?", [limit]).fetchall()
    return [{"series": r[0], "obs_date": r[1], "value": r[2], "zscore": r[3]} for r in rows]
