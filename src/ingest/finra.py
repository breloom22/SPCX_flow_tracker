"""FINRA 공매도 잔고 + ATS(다크풀) 어댑터 (Module B, Phase 3).

- ShortInterestAdapter: 격주 공매도 잔고 (~1주 지연). 시리즈 short_interest:<T>, days_to_cover:<T>.
- AtsAdapter: 주별 ATS/다크풀 체결량 (2~4주 지연). 시리즈 ats_volume:<T>.

FINRA 공식 파일은 인증/차단이 잦다(스펙이 경고). fetcher 주입 가능, 실패 시 stale.
파싱은 헤더명 기반 방어적 처리(파이프/CSV 구분자 자동 감지). 대상 티커만 추출.
지연 메타데이터를 반드시 latency_days로 표기.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime

from .. import db
from ..config import RAW_DIR, load_spcx
from .base import Adapter


def _sniff_rows(text: str) -> list[list[str]]:
    delim = "|" if text.count("|") > text.count(",") else ","
    return [r for r in csv.reader(io.StringIO(text), delimiter=delim) if r]


def _col(header: list[str], *cands: str) -> int | None:
    low = [h.strip().lower() for h in header]
    for c in cands:
        for i, h in enumerate(low):
            if c in h:
                return i
    return None


def _num(r, idx):
    if idx is None or idx >= len(r):
        return None
    v = r[idx].replace(",", "").strip()
    try:
        return float(v)
    except ValueError:
        return None


class ShortInterestAdapter(Adapter):
    series = "short_interest"
    latency_days = 7

    def __init__(self, cfg: dict | None = None, tickers: list[str] | None = None,
                 fetcher=None):
        self.cfg = cfg or load_spcx()
        self.tickers = set(t.upper() for t in (tickers or [self.cfg["ticker"]]))
        self._fetcher = fetcher

    def fetch(self) -> str:
        if self._fetcher is None:
            raise NotImplementedError("FINRA short interest 다운로드 URL 미설정(인증/차단). fetcher 주입 필요.")
        text = self._fetcher()
        (RAW_DIR / f"finra_si_{datetime.now().date().isoformat()}.txt").write_text(
            text, encoding="utf-8")
        return text

    def normalize(self, text: str) -> list[dict]:
        rows = _sniff_rows(text)
        if not rows:
            return []
        header = rows[0]
        ci_sym = _col(header, "symbol", "ticker")
        ci_si = _col(header, "currentshortposition", "short interest", "shortposition")
        ci_dtc = _col(header, "daystocover", "days to cover")
        ci_date = _col(header, "settlementdate", "settlement", "date")
        now = datetime.now()
        out = []
        for r in rows[1:]:
            if ci_sym is None or ci_sym >= len(r):
                continue
            sym = r[ci_sym].strip().upper()
            if sym not in self.tickers:
                continue
            d = now.date()
            if ci_date is not None and ci_date < len(r):
                try:
                    d = datetime.strptime(r[ci_date].strip()[:10].replace("/", "-"),
                                          "%Y-%m-%d").date()
                except ValueError:
                    pass
            si = _num(r, ci_si)
            dtc = _num(r, ci_dtc)
            if si is not None:
                out.append({"series": f"short_interest:{sym}", "obs_date": d, "value": si,
                            "source": "finra", "fetched_at": now,
                            "latency_days": self.latency_days})
            if dtc is not None:
                out.append({"series": f"days_to_cover:{sym}", "obs_date": d, "value": dtc,
                            "source": "finra", "fetched_at": now,
                            "latency_days": self.latency_days})
        return out

    def upsert(self, con, rows: list[dict]) -> int:
        return db.upsert(con, "observations", rows, ["series", "obs_date"])


class AtsAdapter(Adapter):
    series = "ats_darkpool"
    latency_days = 21

    def __init__(self, cfg: dict | None = None, tickers: list[str] | None = None,
                 fetcher=None):
        self.cfg = cfg or load_spcx()
        self.tickers = set(t.upper() for t in (tickers or [self.cfg["ticker"]]))
        self._fetcher = fetcher

    def fetch(self) -> str:
        if self._fetcher is None:
            raise NotImplementedError("FINRA OTC Transparency 다운로드 미설정(차단). fetcher 주입 필요.")
        text = self._fetcher()
        (RAW_DIR / f"finra_ats_{datetime.now().date().isoformat()}.txt").write_text(
            text, encoding="utf-8")
        return text

    def normalize(self, text: str) -> list[dict]:
        rows = _sniff_rows(text)
        if not rows:
            return []
        header = rows[0]
        ci_sym = _col(header, "issuesymbol", "symbol", "ticker")
        ci_vol = _col(header, "totalweeklyshare", "sharequantity", "volume")
        ci_date = _col(header, "weekstart", "weekofdate", "date")
        now = datetime.now()
        # 같은 (sym,date) 여러 MPID 합산
        agg: dict = {}
        for r in rows[1:]:
            if ci_sym is None or ci_sym >= len(r):
                continue
            sym = r[ci_sym].strip().upper()
            if sym not in self.tickers:
                continue
            d = now.date()
            if ci_date is not None and ci_date < len(r):
                try:
                    d = datetime.strptime(r[ci_date].strip()[:10].replace("/", "-"),
                                          "%Y-%m-%d").date()
                except ValueError:
                    pass
            vol = _num(r, ci_vol) or 0.0
            agg[(sym, d)] = agg.get((sym, d), 0.0) + vol
        return [{"series": f"ats_volume:{sym}", "obs_date": d, "value": v,
                 "source": "finra_otc", "fetched_at": now, "latency_days": self.latency_days}
                for (sym, d), v in agg.items()]

    def upsert(self, con, rows: list[dict]) -> int:
        return db.upsert(con, "observations", rows, ["series", "obs_date"])
