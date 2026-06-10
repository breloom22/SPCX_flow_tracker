from datetime import date, datetime

import pytest

from src import db
from src.calendar_utils import add_trading_days, business_days_until
from src.mechanical import lockup as a2
from src.mechanical import holders as a3
from tests.fixtures import make_cfg


# ---------- calendar_utils ----------

def test_add_trading_days_skips_weekend():
    # 2026-06-12는 금요일. +1 거래일 = 6/15(월)
    assert add_trading_days(date(2026, 6, 12), 1) == date(2026, 6, 15)


def test_add_trading_days_15():
    # 상장 6/12 + 15거래일 (7월 초). 정확한 날짜는 휴장일 반영.
    d = add_trading_days(date(2026, 6, 12), 15)
    assert d.month == 7  # 7월 초로 떨어져야 함
    # 거래일 수가 정확히 15여야
    assert business_days_until(d, date(2026, 6, 12)) == 15


# ---------- A2 lockup ----------

def test_resolve_trigger_absolute_days():
    assert a2.resolve_trigger("absolute_days", 366, date(2026, 6, 12), {}) \
        == date(2027, 6, 13)


def test_resolve_trigger_earnings_relative_first():
    # 'first' 실적 8/14(금) + 2거래일 = 8/18(화)
    r = a2.resolve_trigger("earnings_relative", 2, date(2026, 6, 12),
                           {"first": date(2026, 8, 14)})
    assert r == date(2026, 8, 18)


def test_resolve_trigger_earnings_relative_quarter_key():
    # "2026Q3|2" → schedule[2026Q3] + 2거래일. 11/13(금)+2td=11/17(화)
    r = a2.resolve_trigger("earnings_relative", "2026Q3|2", date(2026, 6, 12),
                           {"2026Q3": date(2026, 11, 13)})
    assert r == date(2026, 11, 17)


def test_resolve_trigger_unknown_quarter_none():
    # 스케줄에 없는 분기 → None (날짜 미정, 추정으로 채우지 않음)
    assert a2.resolve_trigger("earnings_relative", "2099Q9|2", date(2026, 6, 12), {}) is None


def test_resolve_trigger_price_condition_none():
    assert a2.resolve_trigger("price_condition", ">$200", date(2026, 6, 12), {}) is None


def test_build_tranches_config_fallback(tmp_path):
    # extracted_dir를 빈 디렉토리로 격리 → config 보도구조 fallback
    cfg = make_cfg()
    rows, mode = a2.build_tranches(cfg, datetime(2026, 6, 10, 12, 0),
                                   extracted_dir=tmp_path)
    assert mode == "config_reported"
    assert len(rows) == 2
    musk = next(r for r in rows if r["tranche_id"] == "musk_366d")
    # 머스크는 release_fraction None → shares None (빈 값 유지, §5)
    assert musk["release_shares"] is None
    insider = next(r for r in rows if r["tranche_id"] == "insider_q1")
    # 10% of 10억주 = 1억주
    assert insider["release_shares"] == pytest.approx(0.10 * 1_000_000_000)


def test_build_calendar_only_dated_with_shares(tmp_path):
    cfg = make_cfg()
    rows, _ = a2.build_tranches(cfg, datetime(2026, 6, 10, 12, 0), extracted_dir=tmp_path)
    cal = a2.build_calendar(rows, cfg, datetime(2026, 6, 10, 12, 0))
    # 물량 있는 트랜치(insider_q1)만 캘린더에 (musk는 물량 미상 → 제외)
    assert len(cal) == 1
    assert cal[0]["cumulative_shares"] == pytest.approx(1e8)
    assert cal[0]["pct_of_float"] == pytest.approx(0.10)


def test_committed_extracted_lockup_validates_and_loads():
    """실제 추출 파일(data/extracted)이 LockupExtraction으로 검증되고 적재되는지."""
    from src.config import load_spcx
    cfg = load_spcx()
    rows, mode = a2.build_tranches(cfg, datetime(2026, 6, 10, 12, 0))
    assert mode == "extracted"
    assert len(rows) == 17
    # 머스크 전량 트랜치 6.4B 존재
    musk = next(r for r in rows if r["tranche_id"] == "t16_day366_musk_all")
    assert musk["release_shares"] == 6_400_000_000
    # 주가조건부 트랜치는 날짜 미정 → 캘린더 제외
    pc = next(r for r in rows if r["trigger_type"] == "price_condition")
    assert pc["resolved_date"] is None
    cal = a2.build_calendar(rows, cfg, datetime(2026, 6, 10, 12, 0))
    # 날짜 확정 트랜치 16건(주가조건 1건 제외)이 날짜별로 집계됨
    assert len(cal) >= 10
    # 누적은 단조 증가
    cums = [c["cumulative_shares"] for c in cal]
    assert cums == sorted(cums)


# ---------- A3 holders ----------

def test_structural_pressure_unknown_forced_flagged():
    cfg = make_cfg()
    hrows = a3.seed_holder_rows(cfg, datetime(2026, 6, 10))
    p = a3.structural_sell_pressure(hrows, cfg)
    # VC Growth는 forced_seller인데 est_pct 미상 → forced_unknown_count=1
    assert p["forced_unknown_count"] == 1
    assert p["known_forced_usd"] == 0


# ---------- DB 멱등성 ----------

def test_upsert_idempotent(tmp_path):
    con = db.connect(tmp_path / "t.duckdb")
    db.init_schema(con)
    row = {"holder_name": "X", "holder_group": "g", "classification": "unknown",
           "est_pct": None, "est_shares": None, "has_redemption_obligation": False,
           "is_dxyz": False, "source": "s", "notes": None, "updated_at": datetime(2026, 6, 10)}
    db.upsert(con, "holders", [row], ["holder_name"])
    db.upsert(con, "holders", [row], ["holder_name"])  # 두 번
    n = con.execute("SELECT COUNT(*) FROM holders").fetchone()[0]
    assert n == 1  # 중복 없음
    con.close()
