"""Module C — 이벤트 스터디 프레임.

A 모듈의 각 이벤트(편입일, 락업 해제일, 실적일)에 대해 [T-pre, T+post] 거래일 윈도우를 정의하고,
윈도우 내 비정상 플래그(anomaly_flags.flagged)를 이벤트에 귀속시켜 event_study_hits에 저장한다.
사후에 "이벤트 며칠 전부터 신호가 나왔는가"를 자동 집계한다.

rel_trading_day: 이벤트일 기준 거래일 오프셋 (음수=이벤트 이전 = 선행 신호).
모든 산출은 inferred.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .. import db
from ..calendar_utils import business_days_until
from ..config import load_spcx


def build_event_study(con, cfg: dict | None = None) -> int:
    """이벤트 윈도우 내 flagged 비정상을 event_study_hits에 귀속. 반환: 기록 수."""
    cfg = cfg or load_spcx()
    win = cfg.get("observe", {}).get("event_study_window", {"pre": 15, "post": 5})
    pre, post = int(win.get("pre", 15)), int(win.get("post", 5))
    now = datetime.now()

    events = con.execute(
        "SELECT event_id, event_date FROM events WHERE event_date IS NOT NULL").fetchall()
    # 윈도우 캘린더 날짜 범위로 1차 필터 후 거래일 오프셋 계산
    con.execute("DELETE FROM event_study_hits")  # 멱등 재계산
    rows: list[dict] = []
    for event_id, edate in events:
        lo = edate - timedelta(days=pre * 2 + 7)   # 거래일→달력일 여유
        hi = edate + timedelta(days=post * 2 + 7)
        flags = con.execute(
            "SELECT series, obs_date, zscore FROM anomaly_flags "
            "WHERE flagged AND obs_date BETWEEN ? AND ?", [lo, hi]).fetchall()
        for series, obs_date, z in flags:
            rel = business_days_until(obs_date, edate)  # 음수=이벤트 이전
            if -pre <= rel <= post:
                rows.append({
                    "event_id": event_id, "series": series, "obs_date": obs_date,
                    "rel_trading_day": rel, "zscore": z,
                    "confidence": "inferred", "created_at": now,
                })
    if rows:
        db.upsert(con, "event_study_hits", rows, ["event_id", "series", "obs_date"])
    return len(rows)


def summarize(con) -> list[dict]:
    """이벤트별 집계: 히트 수, 최선행 신호 거래일(가장 음수), 선행(이벤트 이전) 히트 수."""
    rows = con.execute(
        "SELECT event_id, COUNT(*) AS hits, MIN(rel_trading_day) AS earliest, "
        "SUM(CASE WHEN rel_trading_day < 0 THEN 1 ELSE 0 END) AS pre_hits "
        "FROM event_study_hits GROUP BY event_id ORDER BY hits DESC").fetchall()
    return [{"event_id": r[0], "hits": r[1], "earliest_rel_day": r[2],
             "pre_event_hits": r[3]} for r in rows]
