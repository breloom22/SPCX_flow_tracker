"""A3 — 보유자/분배 트래커.

Phase 1: config seed 보유자 적재 + 분류별 구조적 매도 압력 요약.
Phase 2+: 13F / N-PORT / Form 4·144를 EDGAR에서 수집해 갱신 (ingest/edgar.py).

DXYZ(폐쇄형 펀드)의 시장가 vs NAV 괴리(프리미엄/디스카운트)는 리테일 과열 게이지로
별도 시계열화한다 — Phase 1에서는 보유자 플래그(is_dxyz)만 세팅, 시계열은 C모듈(Phase 2).

구조적 매도 압력: classification == 'forced_seller' (만기 도래 VC 펀드 등 LP 자본 반환 의무)
보유자의 추정 보유분을 합산. est_pct가 없는 보유자는 합산 불가 → needs_review로 표시.
"""
from __future__ import annotations

from datetime import datetime


def seed_holder_rows(cfg: dict, updated_at: datetime) -> list[dict]:
    rows = []
    total_shares = cfg["shares"]["total_shares_outstanding"]
    for h in cfg["holders"]:
        est_pct = h.get("est_pct")
        est_shares = est_pct * total_shares if est_pct is not None else None
        rows.append({
            "holder_name": h["name"],
            "holder_group": h.get("group"),
            "classification": h.get("classification", "unknown"),
            "est_pct": est_pct,
            "est_shares": est_shares,
            "has_redemption_obligation": bool(h.get("has_redemption_obligation", False)),
            "is_dxyz": bool(h.get("is_dxyz", False)),
            "source": h.get("source", "config seed"),
            "notes": h.get("note"),
            "updated_at": updated_at,
        })
    return rows


def structural_sell_pressure(holder_rows: list[dict], cfg: dict) -> dict:
    """forced_seller 보유자들의 추정 매도 압력 요약.

    반환: {known_forced_usd, forced_unknown_count, by_classification}
    known_forced_usd: est_pct가 있는 forced_seller 보유분의 시장가 합 (없으면 0).
    """
    price = cfg["ipo"]["offer_price_usd"]
    by_class: dict[str, dict] = {}
    known_forced_shares = 0.0
    forced_unknown = 0

    for h in holder_rows:
        c = h["classification"]
        b = by_class.setdefault(c, {"count": 0, "known_shares": 0.0, "unknown_pct_count": 0})
        b["count"] += 1
        if h["est_shares"] is not None:
            b["known_shares"] += h["est_shares"]
        else:
            b["unknown_pct_count"] += 1
        if c == "forced_seller":
            if h["est_shares"] is not None:
                known_forced_shares += h["est_shares"]
            else:
                forced_unknown += 1

    return {
        "known_forced_usd": known_forced_shares * price,
        "known_forced_shares": known_forced_shares,
        "forced_unknown_count": forced_unknown,  # 보유분 미상 forced_seller 수 (needs_review)
        "by_classification": by_class,
    }
