"""내부자 Form 4/144 watcher (Module B / A3 공유).

EDGAR submissions에서 SPCX(CIK)의 Form 4 / 144 공시를 감시한다.
락업 해제 후 가장 빠른 확정 신호. 상장 전에는 공시가 없으므로 빈 결과(정상).

동작:
- 신규 Form 4/144 발견 시 원문을 data/inbox/ 캐시 + inbox_docs 기록 (§5.2 10b5-1 vs 재량 분류 대기).
- Claude Code가 각주를 읽고 data/extracted/form4_*.yaml (Form4Classification) 작성 → Phase 3에서 적재.
- 본 어댑터는 LLM 호출 없음(파이프라인 100% 결정적).
"""
from __future__ import annotations

from datetime import datetime

import httpx

from .. import db
from ..config import INBOX_DIR, load_sources, load_spcx
from .base import Adapter

INSIDER_FORMS = {"4", "4/A", "144", "144/A", "3", "5"}


class InsiderWatcher(Adapter):
    series = "insider_form4_144"
    latency_days = 0

    def __init__(self, cik: str | None = None, cfg: dict | None = None,
                 lister=None, downloader=None, max_cache: int = 10, inbox_dir=None):
        self.cfg = cfg or load_spcx()
        self.cik = (cik or self.cfg.get("edgar_cik") or "1181412").zfill(10)
        self.ua = load_sources()["edgar"]["user_agent"]
        self._lister = lister          # 테스트 주입: () -> submissions dict
        self._downloader = downloader  # 테스트 주입: (url) -> text
        self.max_cache = max_cache
        self.inbox_dir = inbox_dir if inbox_dir is not None else INBOX_DIR

    def _headers(self):
        return {"User-Agent": self.ua, "Accept-Encoding": "gzip, deflate"}

    def fetch(self) -> dict:
        if self._lister is not None:
            return self._lister()
        url = f"https://data.sec.gov/submissions/CIK{self.cik}.json"
        with httpx.Client(timeout=20, headers=self._headers()) as c:
            r = c.get(url)
            r.raise_for_status()
            return r.json()

    def normalize(self, sub: dict) -> list[dict]:
        recent = sub.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        accs = recent.get("accessionNumber", [])
        docs = recent.get("primaryDocument", [])
        dates = recent.get("filingDate", [])
        out = []
        for i, f in enumerate(forms):
            if f in INSIDER_FORMS:
                out.append({
                    "form": f, "accession": accs[i],
                    "primary_doc": docs[i] if i < len(docs) else "",
                    "filing_date": dates[i] if i < len(dates) else None,
                })
        return out

    def upsert(self, con, filings: list[dict]) -> int:
        now = datetime.now()
        n = 0
        for fl in filings[: self.max_cache]:
            acc = fl["accession"]
            acc_nodash = acc.replace("-", "")
            doc_id = f"insider_{acc}"
            existing = con.execute(
                "SELECT 1 FROM inbox_docs WHERE doc_id=?", [doc_id]).fetchone()
            if existing:
                continue
            # 원문 캐시 (best-effort)
            path = ""
            if fl.get("primary_doc"):
                url = (f"https://www.sec.gov/Archives/edgar/data/{int(self.cik)}/"
                       f"{acc_nodash}/{fl['primary_doc']}")
                try:
                    if self._downloader is not None:
                        text = self._downloader(url)
                    else:
                        with httpx.Client(timeout=20, headers=self._headers()) as c:
                            rr = c.get(url)
                            rr.raise_for_status()
                            text = rr.text
                    p = self.inbox_dir / f"{self.cfg['ticker']}_{fl['form'].replace('/', '-')}_{fl['filing_date']}_{acc_nodash}.html"
                    p.write_text(text, encoding="utf-8")
                    path = str(p)
                except Exception:  # noqa: BLE001
                    path = url  # 다운로드 실패 시 URL만 기록
            db.upsert(con, "inbox_docs", [{
                "doc_id": doc_id, "doc_type": f"insider_{fl['form']}",
                "path": path, "discovered_at": now, "processed": False,
                "notes": f"Form {fl['form']} {fl['filing_date']} — 10b5-1/재량 분류 대기(§5.2)",
            }], ["doc_id"])
            n += 1
        return n
