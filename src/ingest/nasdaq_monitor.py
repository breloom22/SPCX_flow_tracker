"""나스닥100 편입 발표 모니터 (Module B/D).

나스닥 보도자료/지수 변경 발표 페이지를 폴링해 SPCX 편입 관련 발표를 탐지한다.
탐지 시 원문을 data/inbox/ 캐시 + inbox_docs 기록 → Claude Code가 EventExtraction으로
편입 확정일/실적일을 추출 → 이벤트 캘린더 갱신(§5.3).

스크래핑이 깨지기 쉬운 소스(나스닥/Akamai 차단 가능) → 실패 시 stale(파이프라인 비중단).
키워드 매칭만 결정적으로 수행(LLM 호출 없음).
"""
from __future__ import annotations

import re
from datetime import datetime

import httpx

from .. import db
from ..config import INBOX_DIR, RAW_DIR, load_spcx
from .base import Adapter

# 나스닥 지수 변경 보도자료 후보 URL (차단 시 폴백 순회)
DEFAULT_FEEDS = [
    "https://www.globenewswire.com/en/search/organization/Nasdaq%2520Inc.",
    "https://www.nasdaq.com/press-releases",
]
_BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}


class NasdaqInclusionMonitor(Adapter):
    series = "nasdaq100_inclusion_news"
    latency_days = 0

    def __init__(self, cfg: dict | None = None, feeds: list[str] | None = None,
                 fetcher=None, inbox_dir=None):
        self.cfg = cfg or load_spcx()
        self.ticker = self.cfg["ticker"]
        self.feeds = feeds or DEFAULT_FEEDS
        self._fetcher = fetcher
        self.inbox_dir = inbox_dir if inbox_dir is not None else INBOX_DIR
        # 매칭 키워드 (티커 + 편입/지수)
        self.keywords = [self.ticker, "Nasdaq-100", "Nasdaq 100", "NASDAQ-100"]
        self.inclusion_terms = ["addition", "added", "join", "inclusion", "index change",
                                "annual reconstitution", "special rebalance"]

    def fetch(self) -> list[dict]:
        """각 피드의 (url, text)를 반환. 실패한 피드는 건너뜀. 전부 실패면 예외."""
        out = []
        for url in self.feeds:
            try:
                if self._fetcher is not None:
                    text = self._fetcher(url)
                else:
                    with httpx.Client(timeout=20, headers=_BROWSER_HEADERS,
                                      follow_redirects=True) as c:
                        r = c.get(url)
                        if r.status_code != 200 or not r.text:
                            continue
                        text = r.text
                out.append({"url": url, "text": text})
            except Exception:  # noqa: BLE001
                continue
        if not out:
            raise RuntimeError("나스닥 보도자료 피드 도달 불가(차단/네트워크)")
        return out

    def normalize(self, feeds: list[dict]) -> list[dict]:
        """키워드 매칭되는 피드를 히트로 반환."""
        hits = []
        for f in feeds:
            text = f["text"]
            low = text.lower()
            # 티커/지수 키워드 AND 편입 관련 용어
            has_ticker = any(k.lower() in low for k in self.keywords)
            has_incl = any(t in low for t in self.inclusion_terms)
            if has_ticker and has_incl:
                # 매칭 스니펫 추출
                m = re.search(re.escape(self.ticker), text, re.IGNORECASE)
                pos = m.start() if m else 0
                snippet = re.sub(r"<[^>]+>", " ", text[max(0, pos - 200):pos + 400])
                snippet = re.sub(r"\s+", " ", snippet).strip()
                hits.append({"url": f["url"], "snippet": snippet[:500], "full": text})
        return hits

    def upsert(self, con, hits: list[dict]) -> int:
        now = datetime.now()
        n = 0
        for i, h in enumerate(hits):
            today = now.date().isoformat()
            doc_id = f"nasdaq_news_{today}_{i}"
            if con.execute("SELECT 1 FROM inbox_docs WHERE doc_id=?", [doc_id]).fetchone():
                continue
            path = self.inbox_dir / f"nasdaq_news_{today}_{i}.html"
            path.write_text(h["full"][:2_000_000], encoding="utf-8")
            db.upsert(con, "inbox_docs", [{
                "doc_id": doc_id, "doc_type": "nasdaq_inclusion_news",
                "path": str(path), "discovered_at": now, "processed": False,
                "notes": f"편입 발표 후보 → EventExtraction 추출(§5.3): {h['snippet'][:80]}",
            }], ["doc_id"])
            n += 1
        return n
