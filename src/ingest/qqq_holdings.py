"""QQQ(나스닥100 ETF) 보유내역 어댑터 (Module B).

목적:
- 보유내역 스냅샷 → etf_holdings 테이블 (전일 대비 diff로 리밸런스 실집행 감지).
- 총자산(AUM) → observations(aum:QQQ) (A1 입력의 rule_based 추정을 실측으로 대체 가능).

소스: Invesco 공식 CSV. 단, Invesco는 봇 차단(403)이 잦다(스펙이 경고한 '깨지기 쉬운 소스').
정책(§B):
1. 라이브 CSV 시도 → 성공 시 data/raw/qqq_holdings_<date>.csv 캐시 후 파싱.
2. 실패 시 data/raw/qqq_holdings_*.csv 수동 드롭(가장 최신) 폴백 파싱.
3. 둘 다 없으면 예외 → base.run()이 stale 처리(파이프라인 비중단).

파싱은 헤더명 기반으로 방어적으로 수행(컬럼 순서 변동 대응).
"""
from __future__ import annotations

import csv
import glob
import io
from datetime import datetime

import httpx

from .. import db
from ..config import RAW_DIR, load_sources
from .base import Adapter

_BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "text/csv,application/csv,*/*",
    "Referer": "https://www.invesco.com/",
    "Accept-Language": "en-US,en;q=0.9",
}


def _find_col(header: list[str], *cands: str) -> int | None:
    low = [h.strip().lower() for h in header]
    for c in cands:
        c = c.lower()
        for i, h in enumerate(low):
            if c in h:
                return i
    return None


class QQQHoldingsAdapter(Adapter):
    series = "qqq_holdings"
    latency_days = 1

    def __init__(self, etf: str = "QQQ", url: str | None = None, fetcher=None):
        self.etf = etf
        src = load_sources()["series"]["qqq_holdings"]
        self.url = url or src.get("url")
        self._fetcher = fetcher  # 테스트 주입

    def fetch(self) -> str:
        # 1) 라이브 시도
        if self._fetcher is not None:
            text = self._fetcher(self.url)
        else:
            text = None
            try:
                with httpx.Client(timeout=25, headers=_BROWSER_HEADERS,
                                  follow_redirects=True) as c:
                    r = c.get(self.url)
                    if r.status_code == 200 and r.text.strip():
                        text = r.text
            except Exception:  # noqa: BLE001
                text = None
        if text:
            today = datetime.now().date().isoformat()
            (RAW_DIR / f"qqq_holdings_{self.etf}_{today}.csv").write_text(text, encoding="utf-8")
            return text
        # 2) 수동 드롭 폴백 (가장 최신)
        drops = sorted(glob.glob(str(RAW_DIR / f"qqq_holdings_{self.etf}_*.csv")))
        if drops:
            return open(drops[-1], encoding="utf-8").read()
        raise RuntimeError(f"{self.etf} 보유내역 소스 도달 불가(Invesco 403) + 수동 CSV 없음")

    def normalize(self, raw: str) -> dict:
        """반환: {'holdings': [...etf_holdings rows...], 'aum': float|None}."""
        reader = csv.reader(io.StringIO(raw))
        rows = [r for r in reader if r]
        # 헤더 행 탐색 (Invesco CSV는 상단에 메타 라인이 있을 수 있음)
        header_idx = 0
        for i, r in enumerate(rows[:5]):
            joined = ",".join(r).lower()
            if "weight" in joined and ("ticker" in joined or "holding" in joined):
                header_idx = i
                break
        header = rows[header_idx]
        ci_tkr = _find_col(header, "holding ticker", "ticker")
        ci_w = _find_col(header, "weight")
        ci_sh = _find_col(header, "shares", "par value")
        ci_mv = _find_col(header, "marketvalue", "market value")
        ci_name = _find_col(header, "name")

        fetched = datetime.now()
        obs_date = fetched.date()
        holdings = []
        total_mv = 0.0
        for r in rows[header_idx + 1:]:
            if ci_tkr is None or ci_tkr >= len(r):
                continue
            tkr = r[ci_tkr].strip().upper()
            if not tkr or tkr in ("--", "CASH"):
                continue

            def num(idx):
                if idx is None or idx >= len(r):
                    return None
                v = r[idx].replace(",", "").replace("$", "").replace("%", "").strip()
                try:
                    return float(v)
                except ValueError:
                    return None

            w = num(ci_w)
            sh = num(ci_sh)
            mv = num(ci_mv)
            if mv:
                total_mv += mv
            holdings.append({
                "etf": self.etf, "obs_date": obs_date, "ticker": tkr,
                "weight": (w / 100.0) if (w and w > 1.5) else w,  # %→소수 정규화
                "shares": sh, "market_value": mv,
                "source": "invesco_csv", "fetched_at": fetched,
            })
        aum = total_mv if total_mv > 0 else None
        return {"holdings": holdings, "aum": aum, "obs_date": obs_date, "fetched_at": fetched}

    def upsert(self, con, payload: dict) -> int:
        holdings = payload["holdings"]
        n = db.upsert(con, "etf_holdings", holdings, ["etf", "obs_date", "ticker"])
        if payload.get("aum"):
            db.upsert(con, "observations", [{
                "series": f"aum:{self.etf}", "obs_date": payload["obs_date"],
                "value": payload["aum"], "source": "invesco_csv",
                "fetched_at": payload["fetched_at"], "latency_days": self.latency_days,
            }], ["series", "obs_date"])
        return n


def holdings_diff(con, etf: str = "QQQ") -> list[dict]:
    """최근 2개 스냅샷 날짜 간 보유내역 diff (shares 변화). 리밸런스 실집행 감지.

    반환: [{ticker, prev_shares, curr_shares, delta_shares, delta_pct}] (변화 큰 순).
    스냅샷이 2개 미만이면 빈 리스트.
    """
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT obs_date FROM etf_holdings WHERE etf=? ORDER BY obs_date DESC LIMIT 2",
        [etf]).fetchall()]
    if len(dates) < 2:
        return []
    curr_d, prev_d = dates[0], dates[1]
    curr = {t: s for t, s in con.execute(
        "SELECT ticker, shares FROM etf_holdings WHERE etf=? AND obs_date=?",
        [etf, curr_d]).fetchall()}
    prev = {t: s for t, s in con.execute(
        "SELECT ticker, shares FROM etf_holdings WHERE etf=? AND obs_date=?",
        [etf, prev_d]).fetchall()}
    out = []
    for t in set(curr) | set(prev):
        cs = curr.get(t) or 0.0
        ps = prev.get(t) or 0.0
        if cs == ps:
            continue
        out.append({"ticker": t, "prev_shares": ps, "curr_shares": cs,
                    "delta_shares": cs - ps,
                    "delta_pct": ((cs - ps) / ps) if ps else None})
    out.sort(key=lambda r: abs(r["delta_shares"]), reverse=True)
    return out
