from datetime import date, datetime

import pandas as pd
import pytest

from src import db
from src.ingest.market import MarketDataAdapter, build_universe
from src.ingest.qqq_holdings import QQQHoldingsAdapter, holdings_diff
from src.ingest.etf_flows import EtfFlowAdapter
from src.ingest.insiders import InsiderWatcher
from tests.fixtures import make_cfg


# ---------- market ----------

def _fake_yf_frame():
    idx = pd.to_datetime(["2026-06-05", "2026-06-08"])
    cols = pd.MultiIndex.from_tuples(
        [("Close", "QQQ"), ("Close", "NVDA"), ("Volume", "QQQ"), ("Volume", "NVDA")])
    data = [[700.0, 200.0, 1_000_000, 2_000_000],
            [710.0, 210.0, 1_100_000, 2_200_000]]
    return pd.DataFrame(data, index=idx, columns=cols)


def test_market_normalize_multiindex(tmp_path):
    a = MarketDataAdapter(tickers=["QQQ", "NVDA"], downloader=lambda t, p: _fake_yf_frame())
    rows = a.normalize(a.fetch())
    series = {r["series"] for r in rows}
    assert "close:QQQ" in series and "volume:NVDA" in series
    assert "dollar_volume:QQQ" in series
    dv = next(r for r in rows if r["series"] == "dollar_volume:QQQ"
              and r["obs_date"] == date(2026, 6, 8))
    assert dv["value"] == pytest.approx(710.0 * 1_100_000)


def test_market_universe_dedup():
    cfg = make_cfg()
    cfg["observe"] = {"target": "SPCX", "index_etfs": ["QQQ"], "btc_etfs": ["IBIT"]}
    u = build_universe(cfg)
    assert u[0] == "SPCX"
    assert len(u) == len(set(u))  # 중복 없음
    assert "NVDA" in u  # nasdaq100 top constituents 포함


def test_market_empty_ok(tmp_path):
    a = MarketDataAdapter(tickers=["SPCX"], downloader=lambda t, p: pd.DataFrame())
    assert a.normalize(a.fetch()) == []


# ---------- qqq holdings ----------

_SAMPLE_CSV = (
    "Fund Ticker,Holding Ticker,Name,Weight,Shares/Par Value,MarketValue\n"
    "QQQ,NVDA,NVIDIA,8.90,100000,20000000\n"
    "QQQ,AAPL,APPLE,8.00,90000,18000000\n"
    "QQQ,CASH,Cash,0.10,0,200000\n"
)


def test_qqq_normalize_and_aum():
    # normalize를 직접 호출 (fetch는 RAW_DIR에 캐시 기록 → 테스트 격리 위해 회피)
    a = QQQHoldingsAdapter(fetcher=lambda url: _SAMPLE_CSV)
    payload = a.normalize(_SAMPLE_CSV)
    hold = {h["ticker"]: h for h in payload["holdings"]}
    assert "NVDA" in hold and "AAPL" in hold
    assert "CASH" not in hold  # 현금 제외
    # weight %→소수 정규화
    assert hold["NVDA"]["weight"] == pytest.approx(0.089)
    # aum = market value 합 (NVDA+AAPL+cash 제외? cash는 holdings에서 빠지나 mv 합산엔 미포함)
    assert payload["aum"] == pytest.approx(20000000 + 18000000)


def test_qqq_holdings_diff(tmp_path):
    con = db.connect(tmp_path / "t.duckdb")
    db.init_schema(con)
    now = datetime(2026, 6, 9)
    db.upsert(con, "etf_holdings", [
        {"etf": "QQQ", "obs_date": date(2026, 6, 8), "ticker": "NVDA", "weight": 0.089,
         "shares": 100000, "market_value": 2e7, "source": "t", "fetched_at": now},
        {"etf": "QQQ", "obs_date": date(2026, 6, 9), "ticker": "NVDA", "weight": 0.090,
         "shares": 105000, "market_value": 2.1e7, "source": "t", "fetched_at": now},
    ], ["etf", "obs_date", "ticker"])
    diff = holdings_diff(con, "QQQ")
    assert len(diff) == 1
    assert diff[0]["ticker"] == "NVDA"
    assert diff[0]["delta_shares"] == pytest.approx(5000)
    con.close()


# ---------- etf flows ----------

def test_etf_flow_normalize():
    def provider(tk):
        return {date(2026, 6, 5): (1000.0, 50.0), date(2026, 6, 8): (1100.0, 51.0)}
    a = EtfFlowAdapter(cfg=make_cfg(), tickers=["IBIT"], provider=provider)
    rows = a.normalize(a.fetch())
    flow = next(r for r in rows if r["series"] == "etf_flow_usd:IBIT")
    # (1100-1000)*51 = 5100 (설정 +)
    assert flow["value"] == pytest.approx(100 * 51.0)
    assert any(r["series"] == "shares_out:IBIT" for r in rows)


# ---------- insiders ----------

def _fake_submissions():
    return {"filings": {"recent": {
        "form": ["4", "8-K", "144", "424B4"],
        "accessionNumber": ["0001-26-1", "0001-26-2", "0001-26-3", "0001-26-4"],
        "primaryDocument": ["a.html", "b.html", "c.html", "d.html"],
        "filingDate": ["2026-08-20", "2026-08-19", "2026-08-21", "2026-06-03"],
    }}}


def test_insider_normalize_filters_forms():
    w = InsiderWatcher(cik="1181412", cfg=make_cfg(), lister=_fake_submissions)
    filings = w.normalize(w.fetch())
    forms = {f["form"] for f in filings}
    assert forms == {"4", "144"}  # 8-K, 424B4 제외


def test_insider_upsert_caches_inbox(tmp_path):
    con = db.connect(tmp_path / "t.duckdb")
    db.init_schema(con)
    w = InsiderWatcher(cik="1181412", cfg=make_cfg(),
                       lister=_fake_submissions,
                       downloader=lambda url: "<html>form4</html>",
                       inbox_dir=tmp_path)
    n = w.upsert(con, w.normalize(w.fetch()))
    assert n == 2  # Form 4, 144
    docs = con.execute("SELECT doc_type FROM inbox_docs WHERE processed=FALSE").fetchall()
    assert len(docs) == 2
    # 멱등: 두 번째 호출 시 신규 0
    n2 = w.upsert(con, w.normalize(w.fetch()))
    assert n2 == 0
    con.close()
