"""yfinance OHLCV 어댑터 (Module B).

관측 유니버스(config.observe + nasdaq100_top_constituents)의 일별 종가/거래량을
observations 테이블에 적재한다. 시리즈 네이밍:
    close:<TICKER>          조정 종가
    volume:<TICKER>         거래량
    dollar_volume:<TICKER>  종가 × 거래량 (유동성/관심도)

원칙(§B): fetch→normalize→upsert. 실패 시 stale. 미상장 종목(SPCX 상장 전)은 빈 결과 → 정상.
auto_adjust=True. 지연시세이므로 latency_days=1 표기.
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd

from .. import db
from ..config import load_spcx
from .base import Adapter, IngestResult


def build_universe(cfg: dict) -> list[str]:
    o = cfg.get("observe", {})
    tickers: list[str] = []
    tickers.append(o.get("target", cfg["ticker"]))
    for key in ("index_etfs", "thematic_etfs", "closed_end", "btc_etfs"):
        tickers += list(o.get(key, []))
    # 편입 선행매매 탐지 대상
    tickers += [m["ticker"] for m in cfg.get("nasdaq100_top_constituents", {}).get("members", [])]
    # 중복 제거(순서 보존)
    seen = set()
    out = []
    for t in tickers:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


class MarketDataAdapter(Adapter):
    series = "market_ohlcv"
    latency_days = 1

    def __init__(self, cfg: dict | None = None, period: str = "1y",
                 tickers: list[str] | None = None, downloader=None):
        self.cfg = cfg or load_spcx()
        self.period = period
        self.tickers = tickers or build_universe(self.cfg)
        # 테스트에서 주입 가능한 다운로더 (기본 yfinance.download)
        self._downloader = downloader

    def fetch(self) -> pd.DataFrame:
        if self._downloader is not None:
            return self._downloader(self.tickers, self.period)
        import yfinance as yf
        df = yf.download(self.tickers, period=self.period, interval="1d",
                         auto_adjust=True, progress=False, threads=True)
        return df

    def normalize(self, raw: pd.DataFrame) -> list[dict]:
        if raw is None or len(raw) == 0:
            return []
        fetched = datetime.now()
        rows: list[dict] = []
        # 단일 티커면 단일 인덱스 컬럼, 복수면 MultiIndex (field, ticker)
        multi = isinstance(raw.columns, pd.MultiIndex)
        fields = ["Close", "Volume"]
        for field in fields:
            if multi:
                if field not in raw.columns.get_level_values(0):
                    continue
                sub = raw[field]
                tickers = list(sub.columns)
            else:
                if field not in raw.columns:
                    continue
                sub = raw[[field]]
                sub.columns = [self.tickers[0]]
                tickers = [self.tickers[0]]
            for tk in tickers:
                s = sub[tk].dropna()
                prefix = "close" if field == "Close" else "volume"
                for idx, val in s.items():
                    d = idx.date() if hasattr(idx, "date") else idx
                    rows.append({"series": f"{prefix}:{tk}", "obs_date": d,
                                 "value": float(val), "source": "yfinance",
                                 "fetched_at": fetched, "latency_days": self.latency_days})
        # dollar_volume = close × volume (날짜·티커 매칭)
        rows += self._dollar_volume(rows, fetched)
        return rows

    def _dollar_volume(self, rows: list[dict], fetched) -> list[dict]:
        closes: dict = {}
        vols: dict = {}
        for r in rows:
            s = r["series"]
            if s.startswith("close:"):
                closes[(s[6:], r["obs_date"])] = r["value"]
            elif s.startswith("volume:"):
                vols[(s[7:], r["obs_date"])] = r["value"]
        out = []
        for key, c in closes.items():
            v = vols.get(key)
            if v is not None:
                out.append({"series": f"dollar_volume:{key[0]}", "obs_date": key[1],
                            "value": c * v, "source": "yfinance(derived)",
                            "fetched_at": fetched, "latency_days": self.latency_days})
        return out

    def upsert(self, con, rows: list[dict]) -> int:
        return db.upsert(con, "observations", rows, ["series", "obs_date"])
