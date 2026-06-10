"""EDGAR 어댑터 — SPCX 공시 탐색 + 424B4(최종 prospectus) 원문 inbox 캐시.

스펙 §A2/§5:
- EDGAR full-text search API로 SPCX 관련 공시를 조회한다.
- 발견한 prospectus(424B4 우선, 없으면 S-1/424 계열)를 data/inbox/ 에 원문 캐시.
- 발견 실패는 정상 상태(상장 전이라 424B4 부재 가능) → 파이프라인 비중단, inbox_docs에 기록 안 함.
- EDGAR 요구사항: 식별 가능한 User-Agent 헤더 필수 (config/sources.yaml).

Claude Code는 이후 운영 세션에서 inbox 원문을 읽고 락업 트랜치를 data/extracted/ 에 추출한다.
이 어댑터는 LLM 호출을 하지 않는다 (스펙 §5: 파이프라인은 100% 결정적).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx

from ..config import INBOX_DIR, RAW_DIR, load_sources

# 우선순위: 최종 prospectus > 등록신고서
PROSPECTUS_FORMS = ["424B4", "424B3", "424B5", "S-1/A", "S-1", "F-1/A", "F-1"]


class EdgarClient:
    def __init__(self, sources: dict | None = None, timeout: float = 20.0):
        src = (sources or load_sources())["edgar"]
        self.base_url = src["base_url"]
        self.ua = src["user_agent"]
        self.fulltext_api = "https://efts.sec.gov/LATEST/search-index"
        self.timeout = timeout

    def _headers(self) -> dict:
        return {"User-Agent": self.ua, "Accept-Encoding": "gzip, deflate"}

    def fulltext_search(self, query: str, forms: list[str] | None = None) -> dict:
        """EDGAR full-text search (efts.sec.gov). 2001년 이후 공시 본문 검색."""
        params = {"q": f'"{query}"'}
        if forms:
            params["forms"] = ",".join(forms)
        url = "https://efts.sec.gov/LATEST/search-index"
        with httpx.Client(timeout=self.timeout, headers=self._headers()) as c:
            r = c.get(url, params=params)
            r.raise_for_status()
            return r.json()

    def find_company_cik(self, ticker: str) -> str | None:
        """ticker → CIK (company_tickers.json). 미상장/미등록이면 None."""
        url = "https://www.sec.gov/files/company_tickers.json"
        with httpx.Client(timeout=self.timeout, headers=self._headers()) as c:
            r = c.get(url)
            r.raise_for_status()
            data = r.json()
        for _, rec in data.items():
            if str(rec.get("ticker", "")).upper() == ticker.upper():
                return str(rec["cik_str"]).zfill(10)
        return None

    def list_filings(self, cik: str) -> dict:
        """data.sec.gov submissions — 최근 공시 목록."""
        url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        with httpx.Client(timeout=self.timeout, headers=self._headers()) as c:
            r = c.get(url)
            r.raise_for_status()
            return r.json()


def discover_and_cache(ticker: str, company_name: str,
                       client: EdgarClient | None = None) -> dict:
    """SPCX prospectus를 탐색해 inbox에 캐시.

    반환 요약 dict: {found, cik, cached_paths, message}. 네트워크 실패도 dict로 캡슐화.
    """
    client = client or EdgarClient()
    today = datetime.now(timezone.utc).date().isoformat()
    summary = {"found": False, "cik": None, "cached_paths": [], "message": ""}

    # 1) CIK 조회
    try:
        cik = client.find_company_cik(ticker)
    except Exception as e:  # noqa: BLE001
        summary["message"] = f"CIK 조회 실패(네트워크): {e!r}"
        return summary

    if not cik:
        summary["message"] = (
            f"{ticker} CIK 미발견 — EDGAR 미등록(상장 전 또는 미존재). "
            f"424B4는 프라이싱 이후 게시되므로 정상일 수 있음. needs_review."
        )
        return summary
    summary["cik"] = cik

    # 2) 공시 목록에서 prospectus 후보 탐색
    try:
        sub = client.list_filings(cik)
    except Exception as e:  # noqa: BLE001
        summary["message"] = f"공시목록 조회 실패: {e!r}"
        return summary

    recent = sub.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accessions = recent.get("accessionNumber", [])
    primary_docs = recent.get("primaryDocument", [])
    dates = recent.get("filingDate", [])

    # 우선순위 순으로 첫 매칭
    chosen_idx = None
    for want in PROSPECTUS_FORMS:
        for i, f in enumerate(forms):
            if f == want:
                chosen_idx = i
                break
        if chosen_idx is not None:
            break

    if chosen_idx is None:
        summary["message"] = f"{ticker}(CIK {cik}) prospectus 계열 공시 미발견. needs_review."
        # 원시 submissions는 raw에 캐시(재조회 회피)
        raw_path = RAW_DIR / f"edgar_submissions_{ticker}_{today}.json"
        raw_path.write_text(json.dumps(sub)[:2_000_000], encoding="utf-8")
        summary["cached_paths"].append(str(raw_path))
        return summary

    acc = accessions[chosen_idx].replace("-", "")
    doc = primary_docs[chosen_idx]
    fdate = dates[chosen_idx]
    form = forms[chosen_idx]
    doc_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{doc}"

    try:
        with httpx.Client(timeout=client.timeout, headers=client._headers()) as c:
            r = c.get(doc_url)
            r.raise_for_status()
            content = r.text
    except Exception as e:  # noqa: BLE001
        summary["message"] = f"prospectus 다운로드 실패: {e!r}"
        return summary

    inbox_path = INBOX_DIR / f"{ticker}_{form.replace('/', '-')}_{fdate}_{acc}.html"
    inbox_path.write_text(content, encoding="utf-8")
    summary.update({
        "found": True,
        "cached_paths": [str(inbox_path)],
        "message": f"{form} 캐시 완료: {inbox_path.name} (acc {accessions[chosen_idx]})",
        "form": form,
        "accession": accessions[chosen_idx],
        "doc_url": doc_url,
    })
    return summary
