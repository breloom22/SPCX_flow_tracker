"""미국(나스닥) 거래일 계산 유틸.

상장일 + 15거래일(편입 추정), 실적일 + 2거래일(락업 1차) 등 거래일 기준 날짜 계산에 사용.
pandas_market_calendars의 XNAS(나스닥) 캘린더로 휴장일을 정확히 반영한다.
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pandas_market_calendars as mcal

_CAL = mcal.get_calendar("NASDAQ")


def trading_days(start: date, end: date) -> list[date]:
    """[start, end] 사이의 나스닥 거래일 목록."""
    sched = _CAL.schedule(start_date=start.isoformat(), end_date=end.isoformat())
    return [d.date() for d in sched.index]


def add_trading_days(start: date, n: int) -> date:
    """start(포함하지 않음)로부터 n번째 거래일을 반환.

    n=15 → start 다음 15번째 거래일. start 자체가 거래일이 아니어도 동작.
    """
    if n <= 0:
        raise ValueError("n은 1 이상이어야 한다")
    # 넉넉한 범위를 잡아 거래일을 모은다 (주말·휴일 여유로 2.2배 + 10일)
    horizon = start + timedelta(days=int(n * 2.2) + 14)
    days = trading_days(start + timedelta(days=1), horizon)
    if len(days) < n:
        # 범위 확장 재시도
        horizon = start + timedelta(days=int(n * 3) + 30)
        days = trading_days(start + timedelta(days=1), horizon)
    return days[n - 1]


def is_trading_day(d: date) -> bool:
    return len(trading_days(d, d)) == 1


def next_trading_day(d: date) -> date:
    return add_trading_days(d, 1)


def business_days_until(target: date, frm: date) -> int:
    """frm → target 까지 남은 거래일 수 (D-N 카운트다운용). 과거면 음수."""
    if target == frm:
        return 0
    if target > frm:
        return len(trading_days(frm + timedelta(days=1), target))
    return -len(trading_days(target + timedelta(days=1), frm))
