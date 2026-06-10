"""13F-HR 파서 + SPCX 보유자 탐색 (Module A3 보강, Phase 3).

13F 정보테이블(XML)에서 보유 종목(issuer, cusip, value, shares)을 파싱한다.
EDGAR 풀텍스트 검색으로 SPCX(CUSIP/회사명)를 보유한 13F 제출자를 탐색한다.
- 13F는 분기·45일 지연. SPCX는 6/30 포지션이 8월 중순 공개 → 그 전엔 결과 없음(정상).
- 광범위 13F 스캔은 무겁다 → daily 루프 미포함. `form13f` 명령으로 타깃 검색.

파싱은 네임스페이스 무관(로컬 태그명 기반) 방어적 처리.
"""
from __future__ import annotations

import re
from datetime import datetime

import httpx

from .. import db
from ..config import load_sources, load_spcx


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_13f_infotable(xml_text: str) -> list[dict]:
    """13F 정보테이블 XML → [{issuer, cusip, value_usd, shares, put_call}]."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    out = []
    for el in root.iter():
        if _localname(el.tag) != "infoTable":
            continue
        rec = {"issuer": None, "cusip": None, "value_usd": None, "shares": None,
               "put_call": None}
        for child in el.iter():
            ln = _localname(child.tag)
            txt = (child.text or "").strip()
            if ln == "nameOfIssuer":
                rec["issuer"] = txt
            elif ln == "cusip":
                rec["cusip"] = txt
            elif ln == "value":
                rec["value_usd"] = _to_float(txt)
            elif ln == "sshPrnamt":
                rec["shares"] = _to_float(txt)
            elif ln == "putCall":
                rec["put_call"] = txt or None
        if rec["issuer"] or rec["cusip"]:
            out.append(rec)
    return out


def _to_float(s: str):
    try:
        return float(s.replace(",", ""))
    except (ValueError, AttributeError):
        return None


# 13F value 단위: 과거 천($000), 2023+ 일부 정수($). 휴리스틱은 호출측에서.

def search_spcx_13f_holders(cfg: dict | None = None, client=None,
                            company_terms=None) -> dict:
    """EDGAR 풀텍스트로 13F-HR 중 SPCX 보유 가능 제출자 탐색.

    반환: {found, count, message, filings:[{accession, filer, date}]}.
    SPCX가 13F에 등장하기 전(8월 이전)에는 결과 0(정상).
    """
    cfg = cfg or load_spcx()
    terms = company_terms or ["Space Exploration Technologies", cfg["ticker"]]
    ua = load_sources()["edgar"]["user_agent"]
    summary = {"found": False, "count": 0, "message": "", "filings": []}
    headers = {"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}
    try:
        with (client or httpx.Client(timeout=20, headers=headers)) as c:
            r = c.get("https://efts.sec.gov/LATEST/search-index",
                      params={"q": f'"{terms[0]}"', "forms": "13F-HR"})
            r.raise_for_status()
            data = r.json()
    except Exception as e:  # noqa: BLE001
        summary["message"] = f"13F 검색 실패/결과없음: {e!r}"[:160]
        return summary
    hits = data.get("hits", {}).get("hits", [])
    for h in hits[:50]:
        src = h.get("_source", {})
        summary["filings"].append({
            "accession": src.get("adsh"),
            "filer": (src.get("display_names") or [None])[0],
            "date": src.get("file_date"),
        })
    summary["count"] = len(summary["filings"])
    summary["found"] = summary["count"] > 0
    summary["message"] = (f"13F-HR {summary['count']}건에서 '{terms[0]}' 매칭"
                          if summary["found"] else
                          f"'{terms[0]}' 보유 13F 미발견(8월 공개 전 정상).")
    return summary
