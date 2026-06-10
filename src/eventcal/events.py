"""Module D — 이벤트 캘린더 빌더.

확정/추정 이벤트를 단일 테이블(events)로 생성한다.
{event_id, event_date, event_type, magnitude_usd, confidence, source, notes, is_estimate, needs_review}

초기 이벤트(스펙 §D):
  - 프라이싱(6/11), 상장(6/12)
  - 상장+15거래일 나스닥100 편입 추정일
  - 나스닥 공식 편입 발표 (날짜 미정 → 모니터링 대상, 날짜 없는 placeholder는 생성 안 함)
  - Q2 실적일(추정 8월)
  - 실적+2거래일 락업 1차 해제
  - 13F 공개일 (6/30 기준 포지션, 8월 중순)
  - S&P 정기 리뷰 (2027 시나리오)

이벤트 magnitude_usd는 A1/A2 계산 결과를 주입받아 채운다(편입 매수 규모, 락업 해제 USD 등).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from ..calendar_utils import add_trading_days


def build_events(cfg: dict, created_at: datetime, *,
                 inclusion_buy_usd: float | None = None,
                 inclusion_date_override: date | None = None,
                 earnings_override: date | None = None,
                 lockup_calendar: list[dict] | None = None,
                 lockup_tranches: list[dict] | None = None) -> list[dict]:
    """이벤트 캘린더 생성. 락업 이벤트는 A2 캘린더/트랜치(extracted 우선)에서 파생해
    A2와 일관성을 유지한다. A2 dated 데이터가 없으면 보도기준 rough 이벤트로 fallback.
    """
    ipo = cfg["ipo"]
    n100 = cfg["nasdaq100"]
    listing = ipo["listing_date"]
    pricing = ipo["pricing_date"]
    earnings = earnings_override or cfg["lockup"]["earnings_date_estimate"]

    incl_date = inclusion_date_override or add_trading_days(
        listing, n100["fast_entry_trading_days"])
    f13_date = date(listing.year, 8, 14)  # 13F: 6/30 포지션 → 8/14 마감(45일)

    rows = [
        _ev("ipo_pricing", pricing, "ipo_pricing", ipo["proceeds_usd"],
            "deterministic", ipo["source"], "공모가 확정", is_est=False),
        _ev("ipo_listing", listing, "ipo_listing", None,
            "deterministic", ipo["source"], "나스닥 상장", is_est=False),
        _ev("nasdaq100_inclusion_est", incl_date, "index_inclusion", inclusion_buy_usd,
            "rule_based", "상장+15거래일 (2026-05-01 패스트엔트리 규정)",
            "나스닥100 편입 추정일. 나스닥 공식 발표 시 교체.",
            is_est=(inclusion_date_override is None)),
        _ev("q2_earnings_est", earnings, "earnings", None, "rule_based",
            cfg["lockup"].get("source", "press_report"),
            "첫 분기 실적 발표 (추정 8월, 미정). 확정 시 락업 캘린더 자동 재계산.",
            is_est=not cfg["lockup"].get("earnings_date_confirmed", False),
            needs_review=not cfg["lockup"].get("earnings_date_confirmed", False)),
    ]

    # --- 락업 이벤트: A2 파생 ---
    rows.extend(_lockup_events(cfg, listing, lockup_calendar, lockup_tranches))

    rows += [
        _ev("form13f_q2_disclosure", f13_date, "disclosure", None,
            "deterministic", "SEC 13F 규정(45일)",
            "6/30 기준 기관 포지션 공개 마감. 프리IPO 보유자 확인 가능.", is_est=False),
        _ev("sp500_review_2027", date(2027, 3, 1), "index_review", None,
            "rule_based", cfg["sp500"]["note"],
            "S&P500 정기 리뷰 (2027 편입 시나리오 모니터링).", is_est=True),
    ]
    for r in rows:
        r["created_at"] = created_at
    return rows


def _lockup_events(cfg: dict, listing: date,
                   cal: list[dict] | None, tranches: list[dict] | None) -> list[dict]:
    """A2 캘린더/트랜치에서 핵심 락업 마일스톤 이벤트를 파생."""
    out: list[dict] = []
    if cal:
        first = cal[0]
        last = cal[-1]
        out.append(_ev(
            "lockup_first_release", first["release_date"], "lockup_release",
            first["cumulative_usd"], first["confidence"], "A2 (EDGAR 추출)",
            f"락업 1차 해제 (해제가능 {first['cumulative_shares']/1e6:.0f}M주, "
            f"IPO float 대비 {first['pct_of_float']*100:.0f}%).", is_est=True))
        out.append(_ev(
            "lockup_final_overhang", last["release_date"], "lockup_release",
            last["cumulative_usd"], last["confidence"], "A2 (EDGAR 추출)",
            f"락업 누적 해제 완료 (총 {last['cumulative_shares']/1e6:.0f}M주, "
            f"IPO float 대비 {last['pct_of_float']*100:.0f}% overhang).", is_est=True))
    # 머스크 트랜치 (별도 마일스톤)
    if tranches:
        musk = next((t for t in tranches
                     if t.get("holder_group") == "founder_musk"
                     and t.get("resolved_date")), None)
        if musk:
            out.append(_ev(
                "lockup_musk_release", musk["resolved_date"], "lockup_release",
                musk["est_usd"], musk["confidence"], "A2 (EDGAR 추출)",
                f"머스크 전량 락업 해제 ({(musk['release_shares'] or 0)/1e9:.1f}B주, "
                "조기해제 없음).", is_est=True, needs_review=musk.get("needs_review", False)))
    if not out:
        # fallback: dated 데이터 없음 → 보도기준 rough
        musk_release = listing + timedelta(days=366)
        out.append(_ev("lockup_musk_366d", musk_release, "lockup_release", None,
                        "rule_based", "press_report",
                        "머스크 366일 락업 해제 (보유비중 미상).", is_est=True, needs_review=True))
    return out


def _ev(event_id: str, d: date, etype: str, mag: float | None,
        confidence: str, source: str, notes: str,
        *, is_est: bool = False, needs_review: bool = False) -> dict:
    return {
        "event_id": event_id,
        "event_date": d,
        "event_type": etype,
        "magnitude_usd": mag,
        "confidence": confidence,
        "source": source,
        "notes": notes,
        "is_estimate": is_est,
        "needs_review": needs_review,
        "created_at": None,  # build_events에서 채움
    }
