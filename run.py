#!/usr/bin/env python
"""SPCX Flow Tracker — 단일 진입점 (make 대체).

사용법:
    python run.py daily        # 전체 파이프라인: 수집 + 계산 + 리포트 (멱등)
    python run.py initdb       # DB 스키마만 생성
    python run.py ingest       # EDGAR 탐색 + inbox 캐시만
    python run.py compute      # A1/A2/A3/D 계산만 (DB 적재)
    python run.py report       # 최신 DB로 리포트만 재생성
    python run.py notify-test  # Telegram 설정 점검 (테스트 메시지)

설계: 멱등(같은 날 두 번 돌려도 중복 없음), 외부 소스 실패 시 해당 시리즈만 stale.
"""
from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def _load_dotenv() -> None:
    """.env 단순 파서 (python-dotenv 의존성 회피)."""
    import os
    f = ROOT / ".env"
    if not f.exists():
        return
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _today() -> date:
    return datetime.now().date()


# ---------------------------------------------------------------- commands

def cmd_initdb(con, cfg, sources):
    from src import db
    db.init_schema(con)
    print("[initdb] 스키마 생성 완료.")


def cmd_ingest(con, cfg, sources) -> dict:
    """EDGAR 탐색 + inbox 캐시. 반환: 요약 dict."""
    from src.ingest.edgar import discover_and_cache
    print("[ingest] EDGAR SPCX prospectus 탐색 ...")
    summary = discover_and_cache(cfg["ticker"], cfg["company_name"])
    print(f"[ingest] {summary['message']}")
    # inbox_docs 기록 (발견 시). 대응 extracted 파일이 있으면 처리 완료로 표시.
    if summary.get("found"):
        from src import db
        from src.config import EXTRACTED_DIR
        acc = summary.get("accession", "unknown")
        extracted_done = (EXTRACTED_DIR / f"lockup_{acc}.yaml").exists()
        summary["extracted_done"] = extracted_done
        now = datetime.now()
        db.upsert(con, "inbox_docs", [{
            "doc_id": acc,
            "doc_type": summary.get("form", "prospectus"),
            "path": summary["cached_paths"][0],
            "discovered_at": now,
            "processed": extracted_done,
            "notes": ("추출 완료 → data/extracted/lockup_%s.yaml" % acc)
                     if extracted_done else summary["message"],
        }], ["doc_id"])
    return summary


def cmd_observe(con, cfg, sources) -> list:
    """Module B 관측 어댑터 실행 (yfinance/QQQ/ETF flows/Form4 watcher)."""
    from src.ingest.market import MarketDataAdapter
    from src.ingest.qqq_holdings import QQQHoldingsAdapter
    from src.ingest.etf_flows import EtfFlowAdapter
    from src.ingest.insiders import InsiderWatcher
    from src.ingest.nasdaq_monitor import NasdaqInclusionMonitor
    from src.ingest.options import OptionsAdapter
    from src.ingest.finra import ShortInterestAdapter, AtsAdapter
    results = []
    for adapter in (MarketDataAdapter(cfg), QQQHoldingsAdapter(),
                    EtfFlowAdapter(cfg), InsiderWatcher(cfg=cfg),
                    NasdaqInclusionMonitor(cfg), OptionsAdapter(cfg),
                    ShortInterestAdapter(cfg), AtsAdapter(cfg)):
        res = adapter.run(con)
        results.append(res)
        print(f"[observe] {res.series:16s} ok={res.ok} rows={res.rows_upserted} "
              f"status={res.status}" + (f" err={res.error[:60]}" if res.error else ""))
    return results


def cmd_infer(con, cfg, sources) -> dict:
    """Module C 추론 (z-score/이벤트스터디/선행매매/게이지). 모두 inferred."""
    from src.infer import baseline, event_study, signals
    n_anom = baseline.compute_anomalies(con, cfg)
    n_es = event_study.build_event_study(con, cfg)
    fr = signals.front_running(con, cfg)
    dz = signals.dxyz_retail_gauge(con, cfg)
    ca = signals.cross_asset_absorption(con, cfg)
    ro = signals.retail_overheating(con, cfg)
    print(f"[infer] z-score {n_anom}행 · 이벤트스터디 {n_es}히트 · "
          f"선행매매 {fr['status']} · DXYZ {dz['status']} · "
          f"크로스에셋 {ca['status']} · 리테일과열 {ro['status']}")
    return {"front_running": fr, "dxyz": dz, "cross_asset": ca, "retail": ro,
            "n_anomalies": n_anom, "n_event_hits": n_es}


def cmd_compute(con, cfg, sources) -> dict:
    """A1/A2/A3 + Module D 계산 → DB 적재. 반환: 핵심 수치 dict."""
    from src import db
    from src.mechanical import index_flows as a1
    from src.mechanical import lockup as a2
    from src.mechanical import holders as a3
    from src.eventcal import events as moduleD

    today = _today()
    now = datetime.now()

    # --- A1 ---
    base = a1.compute_inclusion(cfg)
    matrix = a1.sensitivity_matrix(cfg)
    rows = a1.to_db_rows(matrix, today, "deterministic",
                         "calc; inputs=rule_based(AUM,index_mcap)", now)
    db.upsert(con, "index_flow_estimates", rows, ["as_of_date", "scenario"])

    sells = a1.funded_sell_by_constituent(cfg, base.forced_buy_usd)
    sell_rows = [{
        "as_of_date": today, "ticker": s["ticker"],
        "current_weight": s["current_weight"], "est_sell_usd": s["est_sell_usd"],
        "confidence": "rule_based", "created_at": now,
    } for s in sells]
    db.upsert(con, "funded_sell_by_constituent", sell_rows, ["as_of_date", "ticker"])

    loop = a1.reinforcing_loop(cfg)
    loop_rows = [{**r, "as_of_date": today, "created_at": now} for r in loop]
    # 수렴 반복수가 바뀔 수 있어 오늘 행을 비우고 재적재 (멱등)
    con.execute("DELETE FROM reinforcing_loop WHERE as_of_date = ?", [today])
    db.upsert(con, "reinforcing_loop", loop_rows, ["as_of_date", "iteration"])

    # --- A2 ---
    tranches, lockup_mode = a2.build_tranches(cfg, now)
    # 멱등: 기존 트랜치 비우고 새로 적재 (소스 모드가 바뀔 수 있음)
    con.execute("DELETE FROM lockup_tranches")
    db.upsert(con, "lockup_tranches", tranches, ["tranche_id"])
    cal = a2.build_calendar(tranches, cfg, now)
    con.execute("DELETE FROM lockup_calendar")
    db.upsert(con, "lockup_calendar", cal, ["release_date"])

    # --- A3 ---
    hrows = a3.seed_holder_rows(cfg, now)
    db.upsert(con, "holders", hrows, ["holder_name"])
    pressure = a3.structural_sell_pressure(hrows, cfg)

    # --- Module D events ---
    evrows = moduleD.build_events(
        cfg, now,
        inclusion_buy_usd=base.forced_buy_usd,
        lockup_calendar=cal,
        lockup_tranches=tranches,
    )
    # 이벤트 id 집합이 바뀔 수 있어(extracted↔fallback) 전체 비우고 재적재 (멱등)
    con.execute("DELETE FROM events")
    db.upsert(con, "events", evrows, ["event_id"])

    print(f"[compute] A1 base 편입매수=${base.forced_buy_usd/1e9:.2f}B "
          f"(가중치 {base.weight_pct*100:.3f}%) · 시나리오 {len(matrix)}개")
    print(f"[compute] A2 트랜치 {len(tranches)}건 (소스={lockup_mode}), "
          f"캘린더 {len(cal)}개 날짜")
    print(f"[compute] A3 보유자 {len(hrows)}명, forced_seller 미상 "
          f"{pressure['forced_unknown_count']}건")
    print(f"[compute] D 이벤트 {len(evrows)}건")
    return {"lockup_mode": lockup_mode, "base": base, "pressure": pressure}


def _update_freshness(con, cfg, sources, ingest_summary, observe_results=None):
    from src import db
    today = _today()
    now = datetime.now()
    rows = []
    # 기계적 모듈(계산)은 오늘 갱신
    for s in ("index_flows_A1", "lockup_A2", "holders_A3", "events_D",
              "inference_C"):
        rows.append({"series": s, "last_updated": today, "known_latency_days": 0,
                     "status": "active", "notes": "결정적 계산/추론", "updated_at": now})
    # EDGAR
    edgar_status = "active" if (ingest_summary and ingest_summary.get("found")) else "pending"
    edgar_note = (ingest_summary or {}).get("message", "")[:120]
    rows.append({"series": "edgar_prospectus", "last_updated": today,
                 "known_latency_days": 0, "status": edgar_status,
                 "notes": edgar_note, "updated_at": now})
    # Module B 관측 어댑터 실행 결과 (실데이터 적재 여부 반영)
    observed = {}
    for r in (observe_results or []):
        # 어댑터 series명 → sources.yaml 키 매핑
        observed[r.series] = r
    adapter_to_source = {
        "market_ohlcv": "ohlcv", "qqq_holdings": "qqq_holdings",
        "etf_flows": "etf_creation_redemption", "insider_form4_144": "insider_form4_144",
        "options": "options", "short_interest": "short_interest",
        "ats_darkpool": "ats_darkpool",
    }
    src_to_adapter = {v: k for k, v in adapter_to_source.items()}
    # sources.yaml의 관측 시리즈
    for name, meta in sources.get("series", {}).items():
        status = meta.get("status", "interface_only")
        last = None
        notes = f"{meta.get('source','')} ({meta.get('cadence','')})"
        adp = src_to_adapter.get(name)
        if adp and adp in observed:
            res = observed[adp]
            status = res.status  # active | stale | interface_only (IngestResult)
            last = today if res.rows_upserted > 0 else None
            if res.error:
                notes = (notes + " | " + res.error)[:120]
        rows.append({
            "series": name, "last_updated": last,
            "known_latency_days": meta.get("latency_days"),
            "status": status, "notes": notes[:120], "updated_at": now,
        })
    db.upsert(con, "data_freshness", rows, ["series"])


def cmd_report(con, cfg, sources, ingest_summary=None, lockup_mode=None):
    from src.report.daily import build_report
    from src.config import REPORTS_DIR
    today = _today()
    # 단독 실행 시 DB의 트랜치 source로 모드 자동 감지
    if lockup_mode is None:
        row = con.execute(
            "SELECT COUNT(*) FROM lockup_tranches WHERE source <> 'config_reported'"
        ).fetchone()
        lockup_mode = "extracted" if row and row[0] > 0 else "config_reported"
    md = build_report(con, cfg, today, inbox_summary=ingest_summary, lockup_mode=lockup_mode)
    out = REPORTS_DIR / f"{today.isoformat()}.md"
    out.write_text(md, encoding="utf-8")
    print(f"[report] 생성: {out}")
    return out, md


def cmd_form13f(con, cfg, sources):
    """13F-HR에서 SPCX 보유 제출자 탐색 (분기·45일 지연; 8월 공개 전엔 0건)."""
    from src.ingest.form13f import search_spcx_13f_holders
    res = search_spcx_13f_holders(cfg)
    print(f"[13f] {res['message']}")
    for f in res["filings"][:10]:
        print(f"   - {f['date']} {f['filer']} ({f['accession']})")
    return res


def cmd_notify_test(con, cfg, sources):
    from src.notify.telegram import get_notifier, TelegramNotifier
    tn = TelegramNotifier()
    if not tn.configured:
        print("[notify] Telegram 미설정 (.env의 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID).")
        return
    ok = tn.send("SPCX Tracker 테스트", "Telegram 연결 정상 ✅")
    print(f"[notify] 전송 {'성공' if ok else '실패'}.")


def cmd_daily(con, cfg, sources):
    print(f"=== SPCX daily {_today().isoformat()} ===")
    ingest_summary = cmd_ingest(con, cfg, sources)
    observe_results = cmd_observe(con, cfg, sources)   # Module B
    comp = cmd_compute(con, cfg, sources)              # Module A (+ events for C)
    infer_summary = cmd_infer(con, cfg, sources)       # Module C
    _update_freshness(con, cfg, sources, ingest_summary, observe_results)
    out, md = cmd_report(con, cfg, sources, ingest_summary, comp["lockup_mode"])

    # Telegram 요약 알림 (설정 시)
    from src.notify.telegram import get_notifier
    notifier = get_notifier()
    base = comp["base"]
    summary = (f"편입매수(base) ${base.forced_buy_usd/1e9:.2f}B · "
               f"락업소스 {comp['lockup_mode']} · 리포트 {out.name}")
    sent = notifier.send(f"SPCX daily {_today().isoformat()}", summary)
    print(f"[notify] Telegram {'전송' if sent else 'skip(미설정)'}.")
    print("=== done ===")


COMMANDS = {
    "initdb": cmd_initdb,
    "ingest": cmd_ingest,
    "observe": cmd_observe,
    "compute": cmd_compute,
    "infer": cmd_infer,
    "form13f": cmd_form13f,
    "report": cmd_report,
    "notify-test": cmd_notify_test,
    "daily": cmd_daily,
}


def main(argv: list[str]) -> int:
    _load_dotenv()
    from src import db
    from src.config import load_spcx, load_sources, ensure_dirs

    cmd = argv[1] if len(argv) > 1 else "daily"
    if cmd not in COMMANDS:
        print(f"알 수 없는 명령: {cmd}\n사용 가능: {', '.join(COMMANDS)}")
        return 2

    ensure_dirs()
    cfg = load_spcx()
    sources = load_sources()
    con = db.connect()
    db.init_schema(con)
    try:
        COMMANDS[cmd](con, cfg, sources)
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
