# DECISIONS.md — 구현 결정 로그

> SPEC.md(=`SPCX_flow_tracker_spec.md`)의 구현 과정에서 내린 결정과 그 근거를 시간순으로 기록한다.

## 2026-06-10 — Phase 1 시작 시 확정한 가정 (사용자 확인 완료)

| 항목 | 결정 | 근거 |
|---|---|---|
| 태스크 러너 | `python run.py <cmd>` (make 비의존) | Windows 환경에 `make` 부재 가능. 단일 진입점으로 통일. `make daily` ≡ `python run.py daily`. |
| 데이터 계정 | 무료 소스만 사용 | IBKR/Polygon/유료 옵션 미보유. 유료 어댑터는 `observe/base.py` 인터페이스만 구현하고 NotImplemented 처리. |
| 알림 채널 | Telegram 실제 연결 | `.env`의 `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` 사용. httpx로 Bot API 직접 호출(추가 의존성 없음). 미설정 시 no-op. |
| Python | 3.14.4 그대로 | 머신 설치 버전. 스펙의 3.12는 권고치였음. 주요 의존성(duckdb/pandas/pydantic) 3.14 지원 확인. |
| 저장소 | DuckDB 단일 파일 `data/spcx.duckdb` | 스펙 지정. |
| 시세 라이브러리 | Phase 1에서는 yfinance 미사용 | 상장 전이라 SPCX 가격이 없음 → A1은 config의 목표가($135) 사용. yfinance는 Phase 2. |

## 2026-06-10 — 데이터 공백에 대한 처리 원칙

- **SPCX 424B4(최종 prospectus)는 프라이싱(6/11) 이후 EDGAR에 게시되므로 6/10 시점에는 부재할 수 있다.**
  EDGAR 어댑터는 풀텍스트 검색으로 SPCX 관련 공시를 조회하고, 발견 시 `data/inbox/`에 원문 캐시한다.
  발견 실패는 정상 상태로 처리(파이프라인 중단 없음)하고 리포트에 "미처리/대기" 표시.
- **락업 캘린더(A2)는 두 단계로 동작한다.**
  1) `data/extracted/lockup_*.yaml`(Claude Code가 424B4에서 추출한 검증된 트랜치)이 있으면 그것을 적재.
  2) 없으면 config의 *보도 기준* staggered 구조(머스크 366일, 내부자 실적+2거래일)를 `confidence: rule_based`, `needs_review: true`로 잠정 적재. **원문 미확인이므로 추정임을 모든 출력에 명시.**
  → 스펙 §5 원칙 준수: 원문에 없는 값을 임의 추정으로 채우지 않는다. 보도된 구조는 출처를 "press_report"로 명시.
- **나스닥100 추종 AUM·지수 총시총은 config의 추정 placeholder**로 시작(일별 수집은 Phase 2). 모든 A1 출력에 가정 각주를 단다.

## 2026-06-10 — A2 락업 추출 완료 (실제 공시 확보)

- **EDGAR 어댑터가 실제 SpaceX 공시를 발견.** ticker SPCX → CIK 1181412 = "SPACE EXPLORATION TECHNOLOGIES CORP".
  최신 prospectus 계열은 **S-1/A (2026-06-03, acc 0001628280-26-040364)**. 424B4는 프라이싱(6/11) 이후 게시 예정.
- **Claude Code가 S-1/A "Sales of Restricted Shares" 표에서 락업 트랜치 17건을 직접 추출** →
  `data/extracted/lockup_0001628280-26-040364.yaml` (LockupExtraction 검증 통과, A2 입력).
  - 구조 요약: 180일 락업분(7%씩 분할 + First Earnings·Q3 실적 트리거) + 연장 락업분(머스크 제외, Q4~Q2'27 실적·일자 트리거 20%/10%) + **머스크 366일 6.4B주 전량, 조기해제 없음**.
  - 주가 조건부 트랜치(Additional Release Shares 455.8M)는 `price_condition` → 날짜 미정(needs_review), 캘린더 제외.
  - 1년 초과 락업 = 약 7.8B주(머스크 100% 포함, 발행주식의 ~60%). Rule 144/등록권 ~12.2B주.
- **검증된 사실로 config 갱신**: `shares` (Class A 7,380,196,910 + Class B 5,695,668,265 = 13,075,865,175), 출처 S-1/A.
- **핵심 인사이트**: 락업 누적 해제 '가능' 물량이 IPO float(~556M주) 대비 최종 **~22x(2,229%) overhang**.
  명목가 기준 누적 ~$1.67T. (해제 '가능'이며 예측 매도액 아님 — 리포트에 명시.)
- Module D 락업 이벤트는 A2 캘린더/트랜치에서 **파생**(하드코딩 제거) → D와 A2 날짜 일관성 확보.

## 2026-06-10 — Phase 2 + Phase 3 구현 완료

**Module B 관측 어댑터 (`src/ingest/`)** — fetch→normalize→upsert, 실패 시 stale, raw 캐시:
- `market.py` (yfinance OHLCV): 유니버스 ~30종목(SPCX+QQQ/QQQM+우주테마 UFO·XOVR·ARKX+DXYZ+BTC IBIT·FBTC+나스닥100 상위) → 24k+ 관측. **실데이터 동작.**
- `qqq_holdings.py` (Invesco CSV): 보유내역 diff + AUM. **Invesco가 봇 차단(403)** → stale 처리 + `data/raw/qqq_holdings_*.csv` 수동 드롭 폴백 지원.
- `etf_flows.py` (yfinance shares_full): 설정/환매 프록시. 대부분 티커 None 반환 → 가용분만(DXYZ 등).
- `insiders.py` (EDGAR Form 4/144 watcher): 신규 공시 inbox 캐시(§5.2 분류 대기). 상장 전 0건.
- `nasdaq_monitor.py`: 편입 발표 폴링 → 키워드 매칭 시 inbox 캐시(§5.3 EventExtraction 대기).

**Module C 추론 (`src/infer/`)** — 모두 `inferred`, ML 없음(해석가능성 우선):
- `baseline.py`: 롤링 z-score(60일, |z|≥2). close는 수익률로 변환. → anomaly_flags.
- `event_study.py`: 이벤트 [T-15,T+5] 윈도우에 비정상 귀속 + "며칠 전부터 신호" 자동 집계.
- `signals.py`: 편입 선행매매(바스켓 상대수익률), DXYZ 게이지(NAV 부재→proxy), 크로스에셋 흡수지수, 리테일 과열.

**Phase 3 (`src/ingest/`)**:
- `options.py` (yfinance options): 콜/풋 OI, 풋콜비, ATM IV.
- `finra.py` (ShortInterest·Ats): 표준 포맷 방어적 파서. **무료 직접 다운로드 차단/인증** → fetcher 주입형, 기본 interface_only.
- `form13f.py`: 13F-HR 정보테이블 파서 + EDGAR 풀텍스트 SPCX 보유자 탐색(`run.py form13f`). 6/30 포지션은 8월 공개.

**관측된 실제 신호(2026-06-10 데이터)**: 우주/테마 ETF(ARKX·XOVR) 및 QQQM 거래량이 상장 직전 z=2.6~4.5 급등(리테일 선행 관심); GOOGL·NVDA가 나스닥100 바스켓 대비 상대약세(편입 선행매도 의심, inferred). cross_asset은 BTC ETF flow 무료 데이터 부재로 degraded.

**스키마 마이그레이션**: db.py에 파생 테이블 컬럼 드리프트 감지→DROP 재생성(`_migrate`). 파생 데이터는 재계산되므로 안전.

**비목표 준수**: 파이프라인 내 LLM 호출 없음, 매매추천 없음, ML 없음(§7). 유료(IBKR borrow 등) 인터페이스만.

## 미해결/추후 (needs_review)
- **최종 424B4** 게시 시(프라이싱 6/11 이후) 재추출 → 공모가 확정·정정 반영. (현재는 S-1/A + 공모가 추정 $135)
- 주가 조건부 Additional Release Shares(455.8M): 조건 충족 시 First Earnings+2거래일 해제 → 실시간 주가 모니터링 필요(Phase 2).
- 첫 분기 실적일 및 분기 실적 스케줄(현재 추정). 확정 시 `config: lockup.earnings_schedule` 갱신 → 캘린더 자동 재계산.
- 나스닥 공식 편입일/발표 (상장+15거래일 추정 → 발표값으로 교체).
- QQQ/QQQM 실제 AUM·나스닥100 지수 총시총 일별 수집(Phase 2) → A1 입력의 rule_based 추정 제거.
- A3 forced_seller 보유분 미상(VC 성장펀드 등) → 13F/N-PORT로 정량화(Phase 2~3).
