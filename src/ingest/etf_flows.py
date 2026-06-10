"""ETF 설정/환매(creation/redemption) 프록시 어댑터 (Module B).

shares outstanding 변화 × 종가 ≈ 순설정(+)/순환매(-) 달러 플로우.
BTC 현물 ETF(IBIT·FBTC) 순유출은 크로스에셋 유동성 흡수 프록시(C 모듈).

소스: yfinance Ticker.get_shares_full (무료, 단 일부 티커는 None 반환 → 해당 시리즈 stale).
시리즈:
    shares_out:<TICKER>     발행주식수
    etf_flow_usd:<TICKER>   Δshares_out × close (설정/환매 달러 프록시)

데이터 미가용 티커는 빈 결과(추정으로 채우지 않음, §5). latency_days=1.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .. import db
from ..config import load_spcx
from .base import Adapter


def _flow_tickers(cfg: dict) -> list[str]:
    o = cfg.get("observe", {})
    out = []
    for key in ("index_etfs", "thematic_etfs", "btc_etfs", "closed_end"):
        out += list(o.get(key, []))
    seen = set()
    return [t for t in out if not (t in seen or seen.add(t))]


class EtfFlowAdapter(Adapter):
    series = "etf_flows"
    latency_days = 1

    def __init__(self, cfg: dict | None = None, tickers: list[str] | None = None,
                 lookback_days: int = 90, provider=None):
        self.cfg = cfg or load_spcx()
        self.tickers = tickers or _flow_tickers(self.cfg)
        self.lookback_days = lookback_days
        self._provider = provider  # 테스트 주입: ticker -> {date: (shares, close)}

    def fetch(self) -> dict:
        if self._provider is not None:
            return {t: self._provider(t) for t in self.tickers}
        import yfinance as yf
        start = (datetime.now().date() - timedelta(days=self.lookback_days)).isoformat()
        out: dict = {}
        for tk in self.tickers:
            try:
                t = yf.Ticker(tk)
                sh = t.get_shares_full(start=start)
                if sh is None or len(sh) == 0:
                    continue
                hist = t.history(start=start, auto_adjust=True)
                closes = {idx.date(): float(v) for idx, v in hist["Close"].items()}
                series = {}
                for idx, val in sh.items():
                    d = idx.date() if hasattr(idx, "date") else idx
                    series[d] = (float(val), closes.get(d))
                out[tk] = series
            except Exception:  # noqa: BLE001 — 티커별 실패는 건너뛰고 계속
                continue
        return out

    def normalize(self, raw: dict) -> list[dict]:
        fetched = datetime.now()
        rows: list[dict] = []
        for tk, series in (raw or {}).items():
            if not series:
                continue
            items = sorted(series.items())  # [(date,(shares,close)),...]
            prev_shares = None
            for d, (shares, close) in items:
                rows.append({"series": f"shares_out:{tk}", "obs_date": d,
                             "value": shares, "source": "yfinance.shares_full",
                             "fetched_at": fetched, "latency_days": self.latency_days})
                if prev_shares is not None and close is not None:
                    flow = (shares - prev_shares) * close
                    rows.append({"series": f"etf_flow_usd:{tk}", "obs_date": d,
                                 "value": flow, "source": "yfinance(derived)",
                                 "fetched_at": fetched, "latency_days": self.latency_days})
                prev_shares = shares
        return rows

    def upsert(self, con, rows: list[dict]) -> int:
        return db.upsert(con, "observations", rows, ["series", "obs_date"])
