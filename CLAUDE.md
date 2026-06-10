# CLAUDE.md — SPCX Flow Tracker 운영 가이드

이 저장소는 `SPCX_flow_tracker_spec.md`(=SPEC)의 구현이다. 구현 결정은 `DECISIONS.md`.

## 핵심 원칙 (매 세션 상기)
- **확정(deterministic) / 규칙(rule_based) / 추론(inferred)** 신호를 코드·출력에서 분리한다.
- 포지션이 아니라 **플로우(그림자)** 를 추적한다. 모든 수치에 **출처·시점·지연** 메타데이터.
- **파이프라인 코드에 LLM API 호출 금지.** 비정형 텍스트 → 구조화는 Claude Code(나)가 운영 세션에서 직접 수행한다.
- 원문에 없는 값을 "합리적 추정"으로 채우지 않는다 → **빈 값 + `needs_review: true`** 가 정답.

## 실행 (make 대체: python run.py)
```
python run.py daily        # 전체: EDGAR 탐색 + 관측(B) + 계산(A) + 추론(C) + 리포트 (멱등)
python run.py ingest       # EDGAR prospectus 탐색 + inbox 캐시
python run.py observe      # Module B 관측 어댑터 (yfinance/QQQ/ETF/Form4/나스닥모니터/옵션/FINRA)
python run.py compute      # Module A 계산 (인덱스/락업/보유자/이벤트)
python run.py infer        # Module C 추론 (z-score/이벤트스터디/선행매매/게이지)
python run.py report       # 리포트만 재생성
python run.py form13f      # 13F-HR에서 SPCX 보유자 탐색 (분기·8월 공개 전 0건)
python run.py notify-test  # Telegram 연결 점검
```
daily 실행 순서: ingest → observe(B) → compute(A) → infer(C) → freshness → report.
- venv: `.venv/Scripts/python.exe run.py daily`
- 산출물: `reports/YYYY-MM-DD.md`, DB: `data/spcx.duckdb`

## 일일 운영 루틴 (스펙 §5 — 매 세션 이 순서로)
1. `python run.py daily` 실행 → 수집 + 계산 + 리포트 생성.
2. 리포트의 **"6. 미처리 문서"** 섹션 확인 → `data/inbox/` 의 신규 문서 처리:
   - 문서를 직접 읽고, `src/schemas.py`의 Pydantic 모델에 맞는 YAML을 `data/extracted/` 에 작성.
   - 필수 포함: **출처(accession/URL), 추출 일시, 신뢰도, 근거 원문 문장 인용.**
   - 모호한 조항은 임의 해석 금지 → `needs_review: true` 로 두고 사용자에게 질문.
3. `python run.py daily` 재실행 → 파이프라인이 extracted를 Pydantic 재검증 후 적재 →
   최종 리포트 확인 → 특이사항을 사용자에게 보고.
4. **그날의 해석 작성**: 리포트를 읽고 `reports/<날짜>.interp.md` 에 "오늘의 읽기"(핵심 변화·
   주의점·다음 할 일)를 작성 → `report` 재실행 시 리포트 **§0**에 자동 포함된다(파이프라인 LLM 미호출).
   - 각 섹션 헤더 밑 "📖 읽는 법"은 코드가 붙이는 **정적** 해설(`src/report/daily.py: _HOWTO`).
     그날그날의 **동적** 판단은 이 interp 파일이 담당 — 둘을 혼동하지 말 것.

## Claude Code 담당 추출 작업 (§5)
1. **락업 조항 → 트랜치 스키마** (`data/extracted/lockup_<acc>.yaml`, `LockupExtraction`).
   - ✅ 완료: S-1/A(acc 0001628280-26-040364) → 17 트랜치. **최종 424B4 게시 시 재수행**(프라이싱 후).
   - 이것이 A2(`src/mechanical/lockup.py`)의 입력. extracted가 있으면 config 보도구조보다 우선.
2. **Form 4/144 각주** → 10b5-1 사전계획 vs 재량 매도 분류 (`Form4Classification`, `data/extracted/form4_*.yaml`).
   - `observe`가 신규 Form 4/144를 inbox 캐시 → 각주 읽고 분류. 락업 해제 후(8월~) 핵심.
3. **나스닥/지수 보도자료** → 편입일·실적일 확정값 (`EventExtraction`) → 이벤트 캘린더 갱신.
   - `observe`의 NasdaqInclusionMonitor가 편입 발표 후보를 inbox 캐시 → 추출.
4. **리포트 §0 "오늘의 읽기" 서술** → `reports/<날짜>.interp.md` (위 일일 루틴 4단계). 데이터 나열에
   해석을 입히는 레이어. 정적 "읽는 법"은 `_HOWTO`, 동적 판단은 interp 파일.

## 트레이드오프
- 추출 지연 하한 = 세션 주기(일 1회). **락업 해제 주간(8월)에는 하루 2회 세션을 사용자에게 제안.**

## 캘린더 재계산 트리거 (확정 시 config 갱신 → daily 재실행)
- 첫 분기 실적일 확정 → `config/spcx.yaml: lockup.earnings_date_estimate` + `earnings_date_confirmed: true`.
- 나스닥 공식 편입일 발표 → events.py `inclusion_date_override` 또는 EventExtraction.
- QQQ/QQQM 실제 AUM, 지수 총시총 → `tracking_aum`, `nasdaq100.index_total_mcap_usd` 갱신 (Phase 2 자동화).

## 구조
```
config/spcx.yaml      도메인 사실 + 파라미터 (재사용: 티커/이벤트 파라미터화)
config/sources.yaml   소스별 URL/지연/주기
src/mechanical/       A1 index_flows · A2 lockup · A3 holders
src/ingest/           EDGAR 어댑터 (+ base: Adapter/PaidAdapter)
src/eventcal/         Module D 이벤트 캘린더
src/report/daily.py   Module E 리포트
src/notify/           Telegram + Notifier 인터페이스
src/observe/ infer/   Module B/C — Phase 2 스텁
data/inbox/           미처리 비정형 문서 (Claude 추출 대기)
data/extracted/       Claude가 작성한 검증 대상 YAML
```

## 비목표 (만들지 마라 — §7)
주문 집행 / 매매 추천 / 백테스트 전략 / 실시간 tick / ML(Phase3 전) / 파이프라인 내 LLM 호출.
