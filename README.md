# SPCX Mechanical Flow Tracker

기계적으로 움직일 수밖에 없는 자금(패시브 인덱스 편입, 락업 매도, VC/펀드 분배)의 **시점과 규모를 사전 정량화**하고,
그 이벤트 윈도우 주변의 비정상 플로우로 재량적 자금(스마트머니)의 선행 움직임을 추정하는 분석 시스템.

대상: **SpaceX (SPCX)** IPO — 2026-06-11 프라이싱, 2026-06-12 나스닥 상장. (이벤트/티커 파라미터화로 후속 대형 IPO 재사용 가능)

> ⚠️ 분석용 정량 리포트이며 **매매 추천/주문 기능이 아니다** (스펙 §7).

## 빠른 시작

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe run.py daily      # 수집 + 계산 + 리포트 (멱등)
```

- 산출물: `reports/YYYY-MM-DD.md`  ·  저장소: `data/spcx.duckdb`
- 명령: `daily | compute | report | ingest | initdb | notify-test`
- Telegram 알림(선택): `.env.example` → `.env` 복사 후 토큰/챗ID 입력.

## 신뢰도 태그 (핵심 설계)
- **deterministic** — 계산(인덱스 편입 수식, 락업 누적 합산)
- **rule_based** — 규칙/보도/추출(락업 트랜치 구조, AUM 추정 입력)
- **inferred** — 추론(z-score 비정상, 선행 매매 — Phase 2)

## 모듈
| 모듈 | 내용 | Phase |
|---|---|---|
| A1 `mechanical/index_flows.py` | 나스닥100 편입 매수/펀딩 매도 + 민감도 + 자기강화 루프 | 1 ✅ |
| A2 `mechanical/lockup.py` | 락업 트랜치 → 날짜별 누적 해제 캘린더 (EDGAR 추출 입력) | 1 ✅ |
| A3 `mechanical/holders.py` | 보유자 분류 + 구조적 매도 압력 | 1 ✅ |
| D `eventcal/events.py` | 이벤트 캘린더 (확정/추정 단일 테이블) | 1 ✅ |
| E `report/daily.py` | 일일 리포트 (카운트다운/플로우/신선도/inbox) | 1 ✅ |
| ingest `ingest/edgar.py` | EDGAR prospectus 탐색 + inbox 캐시 | 1 ✅ |
| B `ingest/market·qqq_holdings·etf_flows·insiders·nasdaq_monitor` | yfinance OHLCV, QQQ 보유내역 diff, ETF flow, Form4/144 watcher, 편입발표 모니터 | 2 ✅ |
| C `infer/baseline·event_study·signals` | z-score 비정상, 이벤트스터디, 편입 선행매매, DXYZ 게이지, 크로스에셋 흡수 | 2 ✅ |
| Phase3 `ingest/options·finra·form13f` | 옵션 OI/IV/풋콜, FINRA 공매도/ATS, 13F 파서 | 3 ✅ |

## LLM 운영 원칙 (스펙 §5)
파이프라인 코드에는 **LLM API 호출이 없다**. 비정형 문서(424B4/Form4/보도자료) → 구조화는
**Claude Code 운영 세션**이 `data/inbox/` 를 읽고 `data/extracted/*.yaml`(Pydantic 검증)을 작성하는 방식으로 수행한다.
일일 운영 루틴은 `CLAUDE.md` 참조.

## 테스트
```bash
.venv/Scripts/python.exe -m pytest -q
```
