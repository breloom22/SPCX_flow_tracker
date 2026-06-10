from datetime import date, datetime, timedelta

import pytest

from src import db
from src.infer import baseline, event_study, signals
from tests.fixtures import make_cfg


def _obs_cfg():
    cfg = make_cfg()
    cfg["observe"] = {
        "target": "SPCX", "index_etfs": ["QQQ"], "thematic_etfs": ["UFO"],
        "closed_end": ["DXYZ"], "btc_etfs": ["IBIT"],
        "zscore_window": 30, "zscore_flag_threshold": 2.0,
        "event_study_window": {"pre": 15, "post": 5},
    }
    return cfg


def _seed_volume(con, series, base=1_000_000, n=40, spike_idx=None, spike=10_000_000):
    now = datetime(2026, 6, 9)
    start = date(2026, 4, 1)
    rows = []
    d = start
    cnt = 0
    while cnt < n:
        if d.weekday() < 5:  # 평일만
            val = spike if (spike_idx is not None and cnt == spike_idx) else base
            rows.append({"series": series, "obs_date": d, "value": float(val),
                         "source": "t", "fetched_at": now, "latency_days": 1})
            cnt += 1
        d += timedelta(days=1)
    db.upsert(con, "observations", rows, ["series", "obs_date"])
    return rows


def test_zscore_flags_spike(tmp_path):
    con = db.connect(tmp_path / "t.duckdb")
    db.init_schema(con)
    rows = _seed_volume(con, "volume:QQQ", spike_idx=39)  # 마지막 날 급등
    baseline.compute_anomalies(con, _obs_cfg())
    last_date = rows[-1]["obs_date"]
    z = con.execute("SELECT zscore, flagged FROM anomaly_flags WHERE series='volume:QQQ' "
                    "AND obs_date=?", [last_date]).fetchone()
    assert z is not None
    assert z[1] is True  # flagged
    assert z[0] > 2.0
    con.close()


def test_event_study_attributes_pre_event(tmp_path):
    con = db.connect(tmp_path / "t.duckdb")
    db.init_schema(con)
    # 비정상 플래그 1건 (2026-06-08)
    db.upsert(con, "anomaly_flags", [{
        "series": "volume:NVDA", "obs_date": date(2026, 6, 8), "value": 9e6,
        "zscore": 3.1, "flagged": True, "confidence": "inferred",
        "created_at": datetime(2026, 6, 9)}], ["series", "obs_date"])
    # 이벤트: 2026-06-12 (플래그가 이벤트 며칠 전)
    db.upsert(con, "events", [{
        "event_id": "ev1", "event_date": date(2026, 6, 12), "event_type": "x",
        "magnitude_usd": None, "confidence": "rule_based", "source": "t",
        "notes": "", "is_estimate": True, "needs_review": False,
        "created_at": datetime(2026, 6, 9)}], ["event_id"])
    n = event_study.build_event_study(con, _obs_cfg())
    assert n == 1
    hit = con.execute("SELECT rel_trading_day FROM event_study_hits WHERE event_id='ev1'").fetchone()
    assert hit[0] < 0  # 이벤트 이전 = 선행 신호
    summ = event_study.summarize(con)
    assert summ[0]["pre_event_hits"] == 1
    con.close()


def test_front_running_ranks_underperformer(tmp_path):
    con = db.connect(tmp_path / "t.duckdb")
    db.init_schema(con)
    now = datetime(2026, 6, 9)
    # funded_sell 2종목
    for t in ("AAA", "BBB"):
        db.upsert(con, "funded_sell_by_constituent", [{
            "as_of_date": date(2026, 6, 9), "ticker": t, "current_weight": 0.05,
            "est_sell_usd": 1e8, "confidence": "rule_based", "created_at": now}],
            ["as_of_date", "ticker"])
    # close 시리즈: AAA 하락(-10%), BBB 상승(+10%) over 16 pts
    start = date(2026, 5, 1)
    for t, p0, p1 in (("AAA", 100, 90), ("BBB", 100, 110)):
        rows = []
        d = start
        cnt = 0
        while cnt < 16:
            if d.weekday() < 5:
                val = p0 + (p1 - p0) * (cnt / 15)
                rows.append({"series": f"close:{t}", "obs_date": d, "value": float(val),
                             "source": "t", "fetched_at": now, "latency_days": 1})
                cnt += 1
            d += timedelta(days=1)
        db.upsert(con, "observations", rows, ["series", "obs_date"])
    res = signals.front_running(con, _obs_cfg(), top_n=2)
    assert res["status"] == "ok"
    # AAA가 상대약세(선행매도 의심) 1위
    assert res["signals"][0]["ticker"] == "AAA"
    assert res["signals"][0]["rel_return"] < 0
    con.close()


def test_cross_asset_absorption_degraded_when_empty(tmp_path):
    con = db.connect(tmp_path / "t.duckdb")
    db.init_schema(con)
    res = signals.cross_asset_absorption(con, _obs_cfg())
    assert res["status"] == "degraded"
    con.close()
