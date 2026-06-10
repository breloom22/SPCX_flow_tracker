"""A2 — 락업 캘린더.

두 단계 동작 (DECISIONS.md):
  1. data/extracted/lockup_*.yaml (Claude Code가 424B4/S-1 에서 추출, Pydantic 검증 통과)이 있으면 적재.
  2. 없으면 config의 *보도 기준* staggered 구조를 confidence='rule_based', needs_review=true로 잠정 적재.

트리거 해석:
  - absolute_days N      → 'date of prospectus'(=anchor_date) + N일
  - absolute_date ISO    → 그대로
  - earnings_relative    → trigger_ref="<quarter>|<n_td>" (예 "2026Q2|2") 또는 정수 n
                           → earnings_schedule[quarter] + n거래일. quarter 미지정 정수면 'first' 사용.
  - price_condition      → 날짜 미정 (resolved_date=None, needs_review)

출력: 날짜별 누적 매도 *가능* 물량 (주식 수, USD 명목, IPO float 대비 overhang %).
주의: USD는 해제 '가능' 물량의 명목가이지 예측 매도액이 아니다.
"""
from __future__ import annotations

import glob
from datetime import date, datetime, timedelta

import yaml
from pydantic import ValidationError

from ..calendar_utils import add_trading_days
from ..config import EXTRACTED_DIR
from ..schemas import LockupExtraction


def _float_shares(cfg: dict) -> float:
    """IPO 유통 주식수 = free_float_usd / offer_price."""
    ipo = cfg["ipo"]
    return ipo["free_float_usd"] / ipo["offer_price_usd"]


def _earnings_schedule(cfg: dict, override_first: date | None = None) -> dict:
    """분기 실적일 추정 스케줄. {'2026Q2': date, ..., 'first': date}.

    config의 earnings_schedule(추정)을 사용하고, 없으면 earnings_date_estimate를 first로.
    """
    lk = cfg["lockup"]
    sched: dict = {}
    raw = lk.get("earnings_schedule") or {}
    for k, v in raw.items():
        if k == "confirmed":
            continue
        sched[k] = v if isinstance(v, date) else date.fromisoformat(str(v))
    first = override_first or sched.get("2026Q2") or lk.get("earnings_date_estimate")
    if first is not None:
        sched["first"] = first if isinstance(first, date) else date.fromisoformat(str(first))
    return sched


def resolve_trigger(trigger_type: str, trigger_ref, anchor_date: date,
                    earnings_schedule: dict) -> date | None:
    """트리거 → 절대 날짜. 날짜 불명(price_condition, 미지정 실적분기)이면 None."""
    if trigger_type == "absolute_days":
        return anchor_date + timedelta(days=int(trigger_ref))
    if trigger_type == "absolute_date":
        v = trigger_ref
        return v if isinstance(v, date) else date.fromisoformat(str(v))
    if trigger_type == "earnings_relative":
        ref = str(trigger_ref)
        if "|" in ref:
            qkey, n = ref.split("|", 1)
            ed = earnings_schedule.get(qkey.strip())
            n = int(n)
        else:
            ed = earnings_schedule.get("first")
            n = int(ref)
        if ed is None:
            return None
        return add_trading_days(ed, n)
    if trigger_type == "price_condition":
        return None
    return None


def load_extracted_tranches(ticker: str, extracted_dir=None) -> LockupExtraction | None:
    """data/extracted/lockup_*.yaml 중 ticker 일치 + 검증 통과한 최신 1건.

    검증 실패 시 None(+ 사유는 호출측에서 리포트 처리). 스펙 §5.
    extracted_dir: 테스트 격리용 (기본 EXTRACTED_DIR).
    """
    base = extracted_dir if extracted_dir is not None else EXTRACTED_DIR
    files = sorted(glob.glob(str(base / "lockup_*.yaml")))
    chosen: LockupExtraction | None = None
    for f in files:
        with open(f, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
        if not raw:
            continue
        try:
            ext = LockupExtraction.model_validate(raw)
        except ValidationError:
            continue
        if ext.ticker.upper() == ticker.upper():
            chosen = ext  # 정렬상 마지막 = 최신
    return chosen


def build_tranches(cfg: dict, created_at: datetime,
                   earnings_override: date | None = None,
                   extracted_dir=None) -> tuple[list[dict], str]:
    """트랜치 DB 행 목록 + 데이터 소스 모드('extracted' | 'config_reported') 반환.

    extracted_dir: 테스트 격리용 (기본 EXTRACTED_DIR).
    """
    ticker = cfg["ticker"]
    listing = cfg["ipo"]["listing_date"]
    pricing = cfg["ipo"].get("pricing_date", listing)
    float_shares = _float_shares(cfg)
    price = cfg["ipo"]["offer_price_usd"]

    ext = load_extracted_tranches(ticker, extracted_dir)
    rows: list[dict] = []

    if ext is not None:
        # 'date of prospectus' ≈ 프라이싱일. earnings_schedule는 config(추정) 사용.
        anchor = pricing
        sched = _earnings_schedule(cfg, override_first=ext.earnings_date_estimate or earnings_override)
        for t in ext.tranches:
            resolved = resolve_trigger(t.trigger_type, t.trigger_ref, anchor, sched)
            shares = t.release_shares
            if shares is None and t.release_fraction is not None:
                shares = t.release_fraction * float_shares
            est_usd = shares * price if shares is not None else None
            rows.append({
                "tranche_id": t.id,
                "holder_group": t.holder_group,
                "classification": t.classification,
                "trigger_type": t.trigger_type,
                "trigger_ref": str(t.trigger_ref),
                "condition": t.condition,
                "release_shares": shares,
                "release_fraction": t.release_fraction,
                "est_usd": est_usd,
                "resolved_date": resolved,
                "confidence": ext.provenance.confidence,
                "needs_review": t.needs_review,
                "source": ext.provenance.source_accession or ext.provenance.source_url,
                "created_at": created_at,
            })
        return rows, "extracted"

    # --- fallback: config 보도 구조 ---
    anchor = listing
    sched = _earnings_schedule(cfg, override_first=earnings_override)
    for t in cfg["lockup"]["reported_tranches"]:
        resolved = resolve_trigger(t["trigger_type"], t["trigger_ref"], anchor, sched)
        frac = t.get("release_fraction")
        shares = frac * float_shares if frac is not None else None
        est_usd = shares * price if shares is not None else None
        rows.append({
            "tranche_id": t["id"],
            "holder_group": t["holder_group"],
            "classification": t["classification"],
            "trigger_type": t["trigger_type"],
            "trigger_ref": str(t["trigger_ref"]),
            "condition": t["condition"],
            "release_shares": shares,
            "release_fraction": frac,
            "est_usd": est_usd,
            "resolved_date": resolved,
            "confidence": t.get("confidence", "rule_based"),
            "needs_review": t.get("needs_review", True),
            "source": t.get("source", "config_reported"),
            "created_at": created_at,
        })
    return rows, "config_reported"


def build_calendar(tranche_rows: list[dict], cfg: dict,
                   created_at: datetime) -> list[dict]:
    """트랜치 → 날짜별 누적 해제 시계열.

    날짜 불명(resolved_date=None) 또는 물량 불명(release_shares=None) 트랜치는 누적에서 제외
    (빈 값을 추정으로 채우지 않음, §5). confidence는 그 날짜 트랜치 중 가장 약한 신뢰도.
    pct_of_float = 누적 해제 가능 주식 / IPO float (overhang 비율, 100% 초과 가능).
    """
    float_shares = _float_shares(cfg)
    price = cfg["ipo"]["offer_price_usd"]

    dated = [r for r in tranche_rows
             if r["resolved_date"] is not None and r["release_shares"] is not None]
    if not dated:
        return []

    conf_rank = {"deterministic": 0, "rule_based": 1, "inferred": 2}
    by_date: dict[date, list[dict]] = {}
    for r in dated:
        by_date.setdefault(r["resolved_date"], []).append(r)

    cum_shares = 0.0
    out: list[dict] = []
    for d in sorted(by_date):
        day_rows = by_date[d]
        cum_shares += sum(r["release_shares"] for r in day_rows)
        weakest = max(conf_rank.get(r["confidence"], 1) for r in day_rows)
        conf = [k for k, v in conf_rank.items() if v == weakest][0]
        out.append({
            "release_date": d,
            "cumulative_shares": cum_shares,
            "cumulative_usd": cum_shares * price,
            "pct_of_float": cum_shares / float_shares if float_shares else 0.0,
            "confidence": conf,
            "created_at": created_at,
        })
    return out
