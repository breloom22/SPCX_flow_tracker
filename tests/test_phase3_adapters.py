from datetime import date

import pytest

from src.ingest.options import OptionsAdapter
from src.ingest.finra import ShortInterestAdapter, AtsAdapter
from src.ingest.form13f import parse_13f_infotable
from tests.fixtures import make_cfg


# ---------- options ----------

def test_options_normalize_oi_putcall_iv():
    chain = {"SPCX": {"spot": 140.0,
                      "calls": [{"strike": 135, "openInterest": 100, "impliedVolatility": 0.5},
                                {"strike": 140, "openInterest": 200, "impliedVolatility": 0.6}],
                      "puts": [{"strike": 140, "openInterest": 150, "impliedVolatility": 0.62},
                               {"strike": 145, "openInterest": 50, "impliedVolatility": 0.7}]}}
    a = OptionsAdapter(make_cfg(), tickers=["SPCX"], chain_provider=lambda t: chain.get(t))
    rows = a.normalize(a.fetch())
    by = {r["series"]: r["value"] for r in rows}
    assert by["oi_call:SPCX"] == 300
    assert by["oi_put:SPCX"] == 200
    assert by["put_call_oi:SPCX"] == pytest.approx(200 / 300)
    # ATM(spot=140) IV: 140 strike 존재 → 콜/풋 중 가장 가까운(거리0) IV
    assert by["iv_atm:SPCX"] in (0.6, 0.62)


def test_options_empty_when_no_chain():
    a = OptionsAdapter(make_cfg(), tickers=["SPCX"], chain_provider=lambda t: None)
    assert a.normalize(a.fetch()) == []


# ---------- FINRA short interest ----------

_SI = ("settlementDate|symbolCode|currentShortPositionQuantity|daysToCoverQuantity\n"
       "2026-08-15|SPCX|1000000|2.5\n"
       "2026-08-15|NVDA|50000000|1.1\n")


def test_short_interest_filters_and_parses():
    a = ShortInterestAdapter(make_cfg(), tickers=["SPCX"], fetcher=lambda: _SI)
    rows = a.normalize(_SI)  # normalize 직접 호출(RAW 기록 회피)
    by = {r["series"]: r for r in rows}
    assert "short_interest:SPCX" in by
    assert by["short_interest:SPCX"]["value"] == 1_000_000
    assert by["short_interest:SPCX"]["obs_date"] == date(2026, 8, 15)
    assert by["short_interest:SPCX"]["latency_days"] == 7
    assert "days_to_cover:SPCX" in by
    assert "short_interest:NVDA" not in by  # 대상 외 제외


def test_short_interest_interface_only_without_fetcher():
    import duckdb
    from src import db
    con = db.connect(":memory:")
    db.init_schema(con)
    a = ShortInterestAdapter(make_cfg(), tickers=["SPCX"])  # fetcher 없음
    res = a.run(con)
    assert res.ok is False and res.status == "interface_only"


# ---------- FINRA ATS ----------

_ATS = ("weekStartDate,issueSymbolIdentifier,MPID,totalWeeklyShareQuantity\n"
        "2026-08-10,SPCX,XYZ,500000\n"
        "2026-08-10,SPCX,ABC,300000\n"
        "2026-08-10,NVDA,XYZ,999\n")


def test_ats_aggregates_mpids():
    a = AtsAdapter(make_cfg(), tickers=["SPCX"], fetcher=lambda: _ATS)
    rows = a.normalize(_ATS)
    assert len(rows) == 1
    assert rows[0]["series"] == "ats_volume:SPCX"
    assert rows[0]["value"] == 800000  # 두 MPID 합산
    assert rows[0]["latency_days"] == 21


# ---------- 13F parser ----------

_13F_XML = """<?xml version="1.0"?>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
 <infoTable>
  <nameOfIssuer>SPACE EXPLORATION TECH</nameOfIssuer>
  <cusip>12345A678</cusip>
  <value>250000</value>
  <shrsOrPrnAmt><sshPrnamt>1800</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
 </infoTable>
 <infoTable>
  <nameOfIssuer>APPLE INC</nameOfIssuer>
  <cusip>037833100</cusip>
  <value>500000</value>
  <shrsOrPrnAmt><sshPrnamt>2500</sshPrnamt></shrsOrPrnAmt>
 </infoTable>
</informationTable>"""


def test_parse_13f_infotable():
    recs = parse_13f_infotable(_13F_XML)
    assert len(recs) == 2
    spcx = next(r for r in recs if "SPACE" in r["issuer"])
    assert spcx["cusip"] == "12345A678"
    assert spcx["value_usd"] == 250000
    assert spcx["shares"] == 1800


def test_parse_13f_bad_xml():
    assert parse_13f_infotable("not xml") == []
