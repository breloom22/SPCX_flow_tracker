"""Module E — 일일 리포트 생성.

reports/YYYY-MM-DD.md 를 생성한다. 섹션(스펙 §E):
  1. 카운트다운: 다가오는 이벤트 D-N + 기계적 자금 규모
  2. 기계적 플로우 현황: 편입 매수 추정, 락업 누적 해제, 구조적 매도자 잔여
  3. 관측 요약: 시리즈 최신값 + z-score (Phase 2)
  4. 추론 섹션: 선행 매매/리테일 과열/크로스에셋 (Phase 2, 모두 inferred 명시)
  5. 데이터 신선도 테이블
  + 미처리 문서(inbox) 섹션 (스펙 §5)

모든 추정치는 confidence/가정을 각주로 명시한다.
"""
from __future__ import annotations

from datetime import date, datetime

from ..calendar_utils import business_days_until


def _usd(x: float | None) -> str:
    if x is None:
        return "—"
    a = abs(x)
    if a >= 1e12:
        return f"${x/1e12:.2f}T"
    if a >= 1e9:
        return f"${x/1e9:.2f}B"
    if a >= 1e6:
        return f"${x/1e6:.1f}M"
    return f"${x:,.0f}"


def _pct(x: float | None, d=2) -> str:
    return "—" if x is None else f"{x*100:.{d}f}%"


def build_report(con, cfg: dict, today: date, *,
                 inbox_summary: dict | None = None,
                 lockup_mode: str = "config_reported") -> str:
    L: list[str] = []
    A = L.append

    A(f"# SPCX Mechanical Flow Tracker — Daily Report {today.isoformat()}")
    A("")
    A(f"> 생성: {datetime.now().isoformat(timespec='seconds')}  ·  "
      f"티커 {cfg['ticker']} ({cfg['exchange']})  ·  "
      f"신뢰도 태그: **deterministic**(계산) / **rule_based**(규칙·보도) / **inferred**(추론)")
    A("")

    # ---------- 1. 카운트다운 ----------
    A("## 1. 이벤트 카운트다운 (D-N)")
    A("")
    ev = con.execute(
        "SELECT event_id, event_date, event_type, magnitude_usd, confidence, "
        "is_estimate, needs_review, notes FROM events "
        "WHERE event_date >= ? ORDER BY event_date", [today]
    ).fetchall()
    if not ev:
        A("_다가오는 이벤트 없음._")
    else:
        A("| D-N | 날짜 | 이벤트 | 기계적 자금 | 신뢰도 | 비고 |")
        A("|---|---|---|---|---|---|")
        for eid, edate, etype, mag, conf, is_est, nrev, notes in ev:
            dn = business_days_until(edate, today)
            dn_s = "D-DAY" if dn == 0 else (f"D-{dn} (거래일)" if dn > 0 else f"D+{-dn}")
            flag = ""
            if is_est:
                flag += "⚠︎추정 "
            if nrev:
                flag += "🔎review "
            A(f"| {dn_s} | {edate.isoformat()} | {etype} | {_usd(mag)} | "
              f"{conf} | {flag}{(notes or '')[:60]} |")
    A("")

    # ---------- 2. 기계적 플로우 현황 ----------
    A("## 2. 기계적 플로우 현황")
    A("")

    # 2a. 편입 매수 (base + 시나리오)
    base = con.execute(
        "SELECT weight_pct, forced_buy_usd, tracking_aum_usd, multiplier "
        "FROM index_flow_estimates WHERE as_of_date=? AND scenario='base'", [today]
    ).fetchone()
    A("### 2a. 나스닥100 편입 매수 추정 (Module A1)")
    if base:
        w, buy, aum, mult = base
        A(f"- **base 시나리오**: 가중치 **{_pct(w,3)}** → 편입 매수 **{_usd(buy)}**  "
          f"(추종 AUM {_usd(aum)}, 멀티플라이어 {mult:g}x)")
        A(f"- _계산은 deterministic, 입력(AUM·지수시총)은 rule_based 추정 — "
          f"§A1 가정 각주 참조._")
        A("")
        A("**민감도 매트릭스** (주가 ±20% / float ±30% / 멀티 1·2·3x):")
        A("")
        A("| 시나리오 | 가중치 | 편입 매수 |")
        A("|---|---|---|")
        scen = con.execute(
            "SELECT scenario, weight_pct, forced_buy_usd FROM index_flow_estimates "
            "WHERE as_of_date=? ORDER BY forced_buy_usd DESC", [today]
        ).fetchall()
        for s, w2, b2 in scen:
            A(f"| {s} | {_pct(w2,3)} | {_usd(b2)} |")
        A("")
        # 펀딩 매도 상위 (C모듈 워치리스트)
        sells = con.execute(
            "SELECT ticker, current_weight, est_sell_usd FROM funded_sell_by_constituent "
            "WHERE as_of_date=? ORDER BY est_sell_usd DESC LIMIT 10", [today]
        ).fetchall()
        if sells:
            A("**펀딩 매도 상위 종목** (편입 재원 마련용 비례 매도 추정 = C모듈 선행매매 워치리스트):")
            A("")
            A("| 종목 | 현재 비중 | 예상 매도 |")
            A("|---|---|---|")
            for t, cw, su in sells:
                A(f"| {t} | {_pct(cw,2)} | {_usd(su)} |")
        A("")
        # 자기강화 루프
        loop = con.execute(
            "SELECT MAX(iteration), MAX(cumulative_buy_usd), MAX(cum_buy_to_float_pct) "
            "FROM reinforcing_loop WHERE as_of_date=?", [today]
        ).fetchone()
        if loop and loop[0]:
            A(f"**자기강화 루프**: {loop[0]}회 반복 수렴 · 누적 패시브 매수 "
              f"{_usd(loop[1])} · float 대비 **{_pct(loop[2])}**  "
              f"(_단순 선형 가격임팩트 가정, inferred 성격_)")
    else:
        A("_편입 계산 결과 없음._")
    A("")

    # 2b. 락업
    A("### 2b. 락업 누적 해제 (Module A2)")
    src_note = ("EDGAR 공시 추출(extracted)" if lockup_mode == "extracted"
                else "**config 보도구조 (원문 미확인 · rule_based · needs_review)**")
    A(f"- 데이터 소스: {src_note}")
    lk = con.execute(
        "SELECT release_date, cumulative_shares, cumulative_usd, pct_of_float, confidence "
        "FROM lockup_calendar ORDER BY release_date"
    ).fetchall()
    if lk:
        A("")
        A("| 해제일 | 누적 해제가능 주식 | 누적 명목 USD | IPO float 대비(overhang) | 신뢰도 |")
        A("|---|---|---|---|---|")
        for rd, cs, cu, pf, cf in lk:
            A(f"| {rd.isoformat()} | {cs/1e6:.1f}M | {_usd(cu)} | {_pct(pf,0)} | {cf} |")
        A("")
        A("> _명목 USD = 해제 '가능' 물량 × 공모가(추정). 예측 매도액이 아님. "
          "overhang = 누적 해제가능 주식 ÷ IPO float(≈556M주)._")
    else:
        A("- _날짜·물량이 확정된 트랜치 없음 (424B4 추출 전까지 누적 시계열 비어 있음). "
          "빈 값을 추정으로 채우지 않음 (§5)._")
    # 트랜치 목록
    tr = con.execute(
        "SELECT tranche_id, holder_group, trigger_type, trigger_ref, resolved_date, "
        "release_shares, confidence, needs_review FROM lockup_tranches ORDER BY tranche_id"
    ).fetchall()
    if tr:
        A("")
        A("**트랜치 (해석된 트리거):**")
        A("")
        A("| 트랜치 | 보유그룹 | 트리거 | 해제일 | 물량 | 신뢰도 | review |")
        A("|---|---|---|---|---|---|---|")
        for tid, hg, tt, tref, rd, rs, cf, nr in tr:
            rd_s = rd.isoformat() if rd else "미정"
            rs_s = f"{rs/1e6:.1f}M" if rs is not None else "미상"
            # 마크다운 표 셀: '|' 이스케이프 + 트리거 표기 정리/절단
            trig = f"{tt}={tref}".replace("|", "+").replace("earnings_relative=", "")
            trig = trig[:38] + ("…" if len(trig) > 38 else "")
            A(f"| {tid} | {hg} | {trig} | {rd_s} | {rs_s} | {cf} | "
              f"{'🔎' if nr else ''} |")
    A("")

    # 2c. 구조적 매도자
    A("### 2c. 보유자 / 구조적 매도 압력 (Module A3)")
    hl = con.execute(
        "SELECT classification, COUNT(*), "
        "SUM(CASE WHEN est_shares IS NOT NULL THEN est_shares ELSE 0 END) "
        "FROM holders GROUP BY classification ORDER BY classification"
    ).fetchall()
    if hl:
        price = cfg["ipo"]["offer_price_usd"]
        A("| 분류 | 보유자 수 | 알려진 보유분(USD) |")
        A("|---|---|---|")
        for cls, cnt, sh in hl:
            A(f"| {cls} | {cnt} | {_usd((sh or 0)*price)} |")
        unknown_forced = con.execute(
            "SELECT COUNT(*) FROM holders WHERE classification='forced_seller' "
            "AND est_shares IS NULL"
        ).fetchone()[0]
        if unknown_forced:
            A("")
            A(f"- ⚠︎ 보유분 미상 forced_seller {unknown_forced}건 — 매도압력 정량화 불가, "
              "needs_review (13F/N-PORT 확인 필요).")
    A("")

    # ---------- 3. 관측 요약 ----------
    A("## 3. 관측 요약 (Module B)")
    A("")
    obs_n = con.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    n_series = con.execute("SELECT COUNT(DISTINCT series) FROM observations").fetchone()[0]
    if obs_n:
        last_obs = con.execute("SELECT MAX(obs_date) FROM observations").fetchone()[0]
        A(f"- 관측 레코드 **{obs_n}건** / 시리즈 {n_series}개 · 최신 관측일 {last_obs}")
        # 대상 종목(SPCX) 핵심 관측 스냅샷
        tk = cfg["ticker"]
        snap = con.execute(
            "SELECT series, value, obs_date FROM observations WHERE series LIKE ? "
            "QUALIFY ROW_NUMBER() OVER (PARTITION BY series ORDER BY obs_date DESC)=1 "
            "ORDER BY series", [f"%:{tk}"]).fetchall()
        if snap:
            A("")
            A(f"**{tk} 핵심 관측 (최신):**")
            A("")
            A("| 시리즈 | 값 | 날짜 |")
            A("|---|---|---|")
            for s, v, d in snap:
                A(f"| {s} | {v:,.4g} | {d.isoformat()} |")
        else:
            A(f"- _{tk} 관측값 없음 (미상장 — 상장 후 close/volume/옵션/공매도 활성화)._")
        # 최신 비정상 플래그
        flags = con.execute(
            "SELECT series, obs_date, zscore FROM anomaly_flags WHERE flagged "
            "ORDER BY obs_date DESC, ABS(zscore) DESC LIMIT 12").fetchall()
        if flags:
            A("")
            A("**신규/최근 비정상 플래그 (|z| ≥ 2):**")
            A("")
            A("| 시리즈 | 날짜 | z-score |")
            A("|---|---|---|")
            for s, d, z in flags:
                A(f"| {s} | {d.isoformat()} | {z:+.2f} |")
        else:
            A("- 비정상 플래그 없음 (충분한 히스토리 누적 후 활성화).")
        # QQQ 보유내역 diff (리밸런스 실집행)
        try:
            from ..ingest.qqq_holdings import holdings_diff
            diff = holdings_diff(con, "QQQ")
        except Exception:  # noqa: BLE001
            diff = []
        if diff:
            A("")
            A("**QQQ 보유내역 변화 상위 (리밸런스 실집행 감지):**")
            A("")
            A("| 종목 | Δ주식수 | Δ% |")
            A("|---|---|---|")
            for r in diff[:8]:
                dp = f"{r['delta_pct']*100:+.1f}%" if r["delta_pct"] is not None else "—"
                A(f"| {r['ticker']} | {r['delta_shares']:+,.0f} | {dp} |")
    else:
        A("- _관측 데이터 없음 (yfinance 등 어댑터 결과 비어있음 — SPCX 미상장 또는 소스 실패)._")
    A("")

    # ---------- 4. 추론 섹션 ----------
    A("## 4. 추론 섹션 (Module C — 모두 `inferred`)")
    A("")

    def _inf(name):
        return con.execute(
            "SELECT value, detail, obs_date FROM inferences WHERE name=? "
            "ORDER BY obs_date DESC LIMIT 1", [name]).fetchone()

    # 4a. 편입 선행 매매
    fr = con.execute(
        "SELECT name, value FROM inferences WHERE name LIKE 'front_run_rel:%' "
        "AND obs_date=(SELECT MAX(obs_date) FROM inferences WHERE name LIKE 'front_run_rel:%') "
        "ORDER BY value ASC LIMIT 8").fetchall()
    A("### 4a. 편입 선행 매매 탐지 (`inferred`)")
    if fr:
        A("- funded_sell 상위 종목의 바스켓 대비 상대수익률 (음수 = 편입 전 선행 매도 추정):")
        A("")
        A("| 종목 | 상대수익률 |")
        A("|---|---|")
        for name, val in fr:
            A(f"| {name.split(':')[1]} | {val*100:+.2f}% |")
    else:
        A("- _구성종목 시세 누적 부족 — 활성화 대기._")
    A("")

    # 4b. 리테일 과열 / DXYZ
    A("### 4b. 리테일 과열 게이지 (`inferred`)")
    for nm, label in (("dxyz_premium", "DXYZ 프리미엄(시장가/NAV)"),
                      ("dxyz_retail_gauge", "DXYZ 게이지(NAV 부재 proxy)"),
                      ("retail_overheating", "리테일 과열 종합")):
        row = _inf(nm)
        if row:
            v, detail, d = row
            unit = "%" if nm == "dxyz_premium" else ""
            shown = f"{v*100:+.2f}%" if nm == "dxyz_premium" else f"{v:+.2f}"
            A(f"- **{label}**: {shown} ({d}) — _{detail}_")
    if not any(_inf(n) for n in ("dxyz_premium", "dxyz_retail_gauge", "retail_overheating")):
        A("- _DXYZ/테마 ETF 데이터 부족 — 활성화 대기._")
    A("")

    # 4c. 크로스에셋 흡수
    A("### 4c. 크로스에셋 흡수 지수 (`inferred`)")
    ca = _inf("cross_asset_absorption")
    if ca:
        v, detail, d = ca
        A(f"- **흡수 지수**: {v:+.2f} ({d}) — _{detail}_ (양수 = 주변 자산서 자금 이탈↑)")
    else:
        A("- _BTC ETF flow / MMF / QQQ 환매 데이터 부족 — 활성화 대기._")
    A("")

    # 4d. 이벤트 스터디
    es = con.execute(
        "SELECT event_id, COUNT(*), MIN(rel_trading_day), "
        "SUM(CASE WHEN rel_trading_day<0 THEN 1 ELSE 0 END) "
        "FROM event_study_hits GROUP BY event_id ORDER BY COUNT(*) DESC LIMIT 8").fetchall()
    A("### 4d. 이벤트 스터디 (이벤트 전후 비정상 귀속, `inferred`)")
    if es:
        A("| 이벤트 | 히트 | 최선행(거래일) | 선행 히트 |")
        A("|---|---|---|---|")
        for eid, hits, earliest, pre in es:
            A(f"| {eid} | {hits} | {earliest} | {pre} |")
    else:
        A("- _이벤트 윈도우 내 비정상 플래그 없음 — 데이터 누적 후 자동 집계._")
    A("")

    # ---------- 5. 데이터 신선도 ----------
    A("## 5. 데이터 신선도")
    A("")
    fr = con.execute(
        "SELECT series, last_updated, known_latency_days, status, notes "
        "FROM data_freshness ORDER BY status, series"
    ).fetchall()
    if fr:
        A("| 시리즈 | 마지막 갱신 | 알려진 지연(일) | 상태 | 비고 |")
        A("|---|---|---|---|---|")
        for s, lu, lat, st, nt in fr:
            lu_s = lu.isoformat() if lu else "—"
            A(f"| {s} | {lu_s} | {lat if lat is not None else '—'} | {st} | {(nt or '')[:50]} |")
    else:
        A("_신선도 레코드 없음._")
    A("")

    # ---------- 미처리 문서 ----------
    A("## 6. inbox 문서 처리 현황 (Claude Code 추출)")
    if inbox_summary:
        A(f"- EDGAR 탐색: {inbox_summary.get('message','')}")
        cps = inbox_summary.get("cached_paths") or []
        if inbox_summary.get("found") and cps:
            if inbox_summary.get("extracted_done"):
                A(f"- ✅ prospectus 추출 완료 (락업 트랜치 적재됨): `{cps[0]}`")
                A("  - _정정/최종 424B4 게시 시 재추출 필요._")
            else:
                A(f"- **신규 prospectus 캐시됨 (추출 대기)**: `{cps[0]}`")
                A("  - ➜ Claude Code 작업: 이 문서의 락업 섹션을 읽고 "
                  "`data/extracted/lockup_<acc>.yaml` (LockupExtraction 스키마) 작성 → "
                  "`python run.py daily` 재실행.")
    undone = con.execute(
        "SELECT doc_id, doc_type, path FROM inbox_docs WHERE processed=FALSE"
    ).fetchall()
    if undone:
        for did, dt, p in undone:
            A(f"- 미처리 [{dt}] {did}: `{p}`")
    if not inbox_summary and not undone:
        A("- _미처리 문서 없음._")
    A("")

    A("---")
    A("_본 리포트는 분석용 정량 추정이며 매매 추천이 아니다 (스펙 §1.5, §7)._")
    return "\n".join(L)
