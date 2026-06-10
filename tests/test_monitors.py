from datetime import datetime

from src import db
from src.ingest.nasdaq_monitor import NasdaqInclusionMonitor
from tests.fixtures import make_cfg

_POSITIVE = """<html><body><h1>Nasdaq Announces Index Changes</h1>
<p>Space Exploration Technologies Corp. (Nasdaq: SPCX) will be added to the
Nasdaq-100 Index, effective prior to market open. This special rebalance
reflects the inclusion of newly listed large-cap securities.</p></body></html>"""

_NEGATIVE = """<html><body><p>Nasdaq reports quarterly earnings. No index changes.</p>
</body></html>"""


def test_monitor_detects_inclusion():
    m = NasdaqInclusionMonitor(make_cfg(), feeds=["u1"],
                               fetcher=lambda url: _POSITIVE)
    hits = m.normalize(m.fetch())
    assert len(hits) == 1
    assert "SPCX" in hits[0]["snippet"] or "Space Exploration" in hits[0]["full"]


def test_monitor_ignores_irrelevant():
    m = NasdaqInclusionMonitor(make_cfg(), feeds=["u1"],
                               fetcher=lambda url: _NEGATIVE)
    assert m.normalize(m.fetch()) == []


def test_monitor_caches_to_inbox(tmp_path):
    con = db.connect(tmp_path / "t.duckdb")
    db.init_schema(con)
    m = NasdaqInclusionMonitor(make_cfg(), feeds=["u1"],
                               fetcher=lambda url: _POSITIVE, inbox_dir=tmp_path)
    n = m.upsert(con, m.normalize(m.fetch()))
    assert n == 1
    doc = con.execute("SELECT doc_type FROM inbox_docs WHERE processed=FALSE").fetchone()
    assert doc[0] == "nasdaq_inclusion_news"
    con.close()


def test_monitor_all_feeds_fail_raises():
    def boom(url):
        raise RuntimeError("blocked")
    m = NasdaqInclusionMonitor(make_cfg(), feeds=["u1", "u2"], fetcher=boom)
    # base.run은 예외를 stale로 캡슐화
    res = m.run(db.connect(":memory:"))
    assert res.ok is False and res.status == "stale"
