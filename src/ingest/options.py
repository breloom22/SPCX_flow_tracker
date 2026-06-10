"""옵션 OI/IV/풋콜 어댑터 (Module B, Phase 3).

SPCX 옵션 상장 후 yfinance options로 콜/풋 OI, ATM IV, 풋콜비를 관측한다.
딜러 감마 추정은 v2 — 우선 OI·스큐만(스펙).

시리즈 (가장 가까운 만기 기준):
    oi_call:<T>           콜 OI 합
    oi_put:<T>            풋 OI 합
    put_call_oi:<T>       풋/콜 OI 비율
    iv_atm:<T>            ATM 내재변동성(근사)

옵션 미상장(SPCX 상장 직후)은 빈 결과 → 정상. latency_days=1.
"""
from __future__ import annotations

from datetime import datetime

from .. import db
from ..config import load_spcx
from .base import Adapter


class OptionsAdapter(Adapter):
    series = "options"
    latency_days = 1

    def __init__(self, cfg: dict | None = None, tickers: list[str] | None = None,
                 chain_provider=None):
        self.cfg = cfg or load_spcx()
        self.tickers = tickers or [self.cfg["ticker"]]
        # 테스트 주입: ticker -> {'spot':float, 'calls':[{strike,openInterest,impliedVolatility}], 'puts':[...]}
        self._chain_provider = chain_provider

    def fetch(self) -> dict:
        out = {}
        if self._chain_provider is not None:
            for t in self.tickers:
                c = self._chain_provider(t)
                if c:
                    out[t] = c
            return out
        import yfinance as yf
        for t in self.tickers:
            try:
                tk = yf.Ticker(t)
                exps = tk.options
                if not exps:
                    continue
                ch = tk.option_chain(exps[0])  # 최근접 만기
                spot = None
                try:
                    spot = float(tk.fast_info["last_price"])
                except Exception:  # noqa: BLE001
                    spot = None
                out[t] = {
                    "spot": spot,
                    "calls": ch.calls[["strike", "openInterest", "impliedVolatility"]]
                             .to_dict("records"),
                    "puts": ch.puts[["strike", "openInterest", "impliedVolatility"]]
                            .to_dict("records"),
                }
            except Exception:  # noqa: BLE001
                continue
        return out

    def normalize(self, raw: dict) -> list[dict]:
        now = datetime.now()
        d = now.date()
        rows = []
        for t, ch in (raw or {}).items():
            calls, puts = ch.get("calls", []), ch.get("puts", [])
            oi_c = sum((x.get("openInterest") or 0) for x in calls)
            oi_p = sum((x.get("openInterest") or 0) for x in puts)
            for name, val in (("oi_call", oi_c), ("oi_put", oi_p)):
                rows.append({"series": f"{name}:{t}", "obs_date": d, "value": float(val),
                             "source": "yfinance.options", "fetched_at": now,
                             "latency_days": self.latency_days})
            if oi_c > 0:
                rows.append({"series": f"put_call_oi:{t}", "obs_date": d,
                             "value": oi_p / oi_c, "source": "yfinance(derived)",
                             "fetched_at": now, "latency_days": self.latency_days})
            # ATM IV: spot에 가장 가까운 strike의 콜/풋 IV 평균
            iv = self._atm_iv(ch)
            if iv is not None:
                rows.append({"series": f"iv_atm:{t}", "obs_date": d, "value": iv,
                             "source": "yfinance.options", "fetched_at": now,
                             "latency_days": self.latency_days})
        return rows

    @staticmethod
    def _atm_iv(ch: dict) -> float | None:
        spot = ch.get("spot")
        if not spot:
            return None
        best = None
        for side in ("calls", "puts"):
            for x in ch.get(side, []):
                k = x.get("strike")
                iv = x.get("impliedVolatility")
                if k is None or iv is None:
                    continue
                dist = abs(k - spot)
                if best is None or dist < best[0]:
                    best = (dist, iv)
        return float(best[1]) if best else None

    def upsert(self, con, rows: list[dict]) -> int:
        return db.upsert(con, "observations", rows, ["series", "obs_date"])
