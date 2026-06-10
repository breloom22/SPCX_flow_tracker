"""DuckDB 연결 + 스키마 초기화.

설계:
- 단일 파일 data/spcx.duckdb.
- 멱등(idempotent): CREATE TABLE IF NOT EXISTS + upsert(DELETE-then-INSERT by key)로 같은 날 두 번 돌려도 중복 없음.
- 모든 수치 테이블에 confidence / source / 시점 메타데이터 컬럼을 둔다 (스펙 §1.3).
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

import duckdb

from .config import DB_PATH, ensure_dirs

# confidence 허용값 (스펙 §1.1)
CONFIDENCE_VALUES = ("deterministic", "rule_based", "inferred")

SCHEMA_SQL = """
-- 이벤트 캘린더 (Module D): 확정/추정 이벤트 단일 테이블
CREATE TABLE IF NOT EXISTS events (
    event_id      VARCHAR PRIMARY KEY,
    event_date    DATE,
    event_type    VARCHAR,
    magnitude_usd DOUBLE,
    confidence    VARCHAR,          -- deterministic | rule_based | inferred
    source        VARCHAR,
    notes         VARCHAR,
    is_estimate   BOOLEAN,
    needs_review  BOOLEAN DEFAULT FALSE,
    created_at    TIMESTAMP
);

-- 보유자/분배 트래커 (Module A3)
CREATE TABLE IF NOT EXISTS holders (
    holder_name             VARCHAR PRIMARY KEY,
    holder_group            VARCHAR,
    classification          VARCHAR,   -- forced_seller | discretionary | unknown
    est_pct                 DOUBLE,
    est_shares              DOUBLE,
    has_redemption_obligation BOOLEAN,
    is_dxyz                 BOOLEAN DEFAULT FALSE,
    source                  VARCHAR,
    notes                   VARCHAR,
    updated_at              TIMESTAMP
);

-- A1 인덱스 편입 계산 결과 (시나리오 매트릭스 포함)
CREATE TABLE IF NOT EXISTS index_flow_estimates (
    as_of_date         DATE,
    scenario           VARCHAR,        -- base | price_-20 | float_+30 | mult_1x ...
    price_usd          DOUBLE,
    float_shares       DOUBLE,
    float_mcap_usd     DOUBLE,
    multiplier         DOUBLE,
    weighting_mcap_usd DOUBLE,
    weight_pct         DOUBLE,
    tracking_aum_usd   DOUBLE,
    forced_buy_usd     DOUBLE,
    confidence         VARCHAR,
    source             VARCHAR,
    created_at         TIMESTAMP,
    PRIMARY KEY (as_of_date, scenario)
);

-- A1 편입 시 기존 나스닥100 종목별 예상 매도 (C모듈 워치리스트)
CREATE TABLE IF NOT EXISTS funded_sell_by_constituent (
    as_of_date      DATE,
    ticker          VARCHAR,
    current_weight  DOUBLE,
    est_sell_usd    DOUBLE,
    confidence      VARCHAR,
    created_at      TIMESTAMP,
    PRIMARY KEY (as_of_date, ticker)
);

-- A1 자기강화 루프 시뮬레이션 결과
CREATE TABLE IF NOT EXISTS reinforcing_loop (
    as_of_date            DATE,
    iteration             INTEGER,
    price_usd             DOUBLE,
    float_mcap_usd        DOUBLE,
    weight_pct            DOUBLE,
    cumulative_buy_usd    DOUBLE,
    cum_buy_to_float_pct  DOUBLE,
    created_at            TIMESTAMP,
    PRIMARY KEY (as_of_date, iteration)
);

-- A2 락업 트랜치 (extracted YAML 또는 config 보도구조에서 적재)
CREATE TABLE IF NOT EXISTS lockup_tranches (
    tranche_id      VARCHAR PRIMARY KEY,
    holder_group    VARCHAR,
    classification  VARCHAR,
    trigger_type    VARCHAR,    -- absolute_days | absolute_date | earnings_relative | price_condition
    trigger_ref     VARCHAR,
    condition       VARCHAR,
    release_shares  DOUBLE,
    release_fraction DOUBLE,
    est_usd         DOUBLE,
    resolved_date   DATE,       -- 트리거 해석 결과 (실적일 등 적용 후)
    confidence      VARCHAR,
    needs_review    BOOLEAN,
    source          VARCHAR,
    created_at      TIMESTAMP
);

-- A2 날짜별 누적 락업 해제 시계열
CREATE TABLE IF NOT EXISTS lockup_calendar (
    release_date        DATE PRIMARY KEY,
    cumulative_shares   DOUBLE,
    cumulative_usd      DOUBLE,
    pct_of_float        DOUBLE,
    confidence          VARCHAR,
    created_at          TIMESTAMP
);

-- Module B 관측 시리즈 (Phase 2 적재; 스키마는 지금 생성)
CREATE TABLE IF NOT EXISTS observations (
    series        VARCHAR,
    obs_date      DATE,
    value         DOUBLE,
    source        VARCHAR,
    fetched_at    TIMESTAMP,
    latency_days  INTEGER,
    PRIMARY KEY (series, obs_date)
);

-- Module C 비정상 플래그 (롤링 z-score)
CREATE TABLE IF NOT EXISTS anomaly_flags (
    series      VARCHAR,
    obs_date    DATE,
    value       DOUBLE,
    zscore      DOUBLE,
    flagged     BOOLEAN,
    confidence  VARCHAR,
    created_at  TIMESTAMP,
    PRIMARY KEY (series, obs_date)
);

-- ETF 보유내역 스냅샷 (QQQ 등) — diff로 리밸런스 실집행 감지
CREATE TABLE IF NOT EXISTS etf_holdings (
    etf           VARCHAR,
    obs_date      DATE,
    ticker        VARCHAR,
    weight        DOUBLE,
    shares        DOUBLE,
    market_value  DOUBLE,
    source        VARCHAR,
    fetched_at    TIMESTAMP,
    PRIMARY KEY (etf, obs_date, ticker)
);

-- Module C 추론 산출물 (게이지/지수). 모두 confidence='inferred'.
CREATE TABLE IF NOT EXISTS inferences (
    name        VARCHAR,
    obs_date    DATE,
    value       DOUBLE,
    confidence  VARCHAR,
    detail      VARCHAR,
    created_at  TIMESTAMP,
    PRIMARY KEY (name, obs_date)
);

-- 이벤트 스터디: 비정상 플래그를 이벤트 윈도우에 귀속
CREATE TABLE IF NOT EXISTS event_study_hits (
    event_id        VARCHAR,
    series          VARCHAR,
    obs_date        DATE,
    rel_trading_day INTEGER,    -- 이벤트일 기준 거래일 오프셋 (음수=이전)
    zscore          DOUBLE,
    confidence      VARCHAR,
    created_at      TIMESTAMP,
    PRIMARY KEY (event_id, series, obs_date)
);

-- 데이터 신선도 (Module E 신선도 테이블)
CREATE TABLE IF NOT EXISTS data_freshness (
    series            VARCHAR PRIMARY KEY,
    last_updated      DATE,
    known_latency_days INTEGER,
    status            VARCHAR,    -- active | stale | interface_only | pending
    notes             VARCHAR,
    updated_at        TIMESTAMP
);

-- 미처리 비정형 문서 (inbox) 트래킹 — 스펙 §5 인수인계 프로토콜
CREATE TABLE IF NOT EXISTS inbox_docs (
    doc_id        VARCHAR PRIMARY KEY,
    doc_type      VARCHAR,
    path          VARCHAR,
    discovered_at TIMESTAMP,
    processed     BOOLEAN DEFAULT FALSE,
    notes         VARCHAR
);
"""


def connect(path: Path | str = DB_PATH) -> duckdb.DuckDBPyConnection:
    ensure_dirs()
    return duckdb.connect(str(path))


# 파생(재계산 가능) 테이블: 스키마 드리프트 시 DROP 후 재생성해도 안전.
# 필수 컬럼 일부가 없으면 구 스키마로 판단하고 드롭한다.
_DERIVED_REQUIRED_COLS = {
    "anomaly_flags": {"value", "zscore", "flagged"},
    "etf_holdings": {"weight", "shares", "market_value"},
    "inferences": {"name", "confidence", "detail"},
    "event_study_hits": {"rel_trading_day", "zscore"},
}


def _migrate(con: duckdb.DuckDBPyConnection) -> None:
    """구 스키마의 파생 테이블을 드롭(데이터는 재계산되므로 안전)."""
    existing = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    for table, required in _DERIVED_REQUIRED_COLS.items():
        if table not in existing:
            continue
        cols = {r[1] for r in con.execute(f"PRAGMA table_info('{table}')").fetchall()}
        if not required.issubset(cols):
            con.execute(f"DROP TABLE IF EXISTS {table}")


def init_schema(con: duckdb.DuckDBPyConnection) -> None:
    _migrate(con)
    con.execute(SCHEMA_SQL)


def upsert(con: duckdb.DuckDBPyConnection, table: str, rows: list[dict],
           key_cols: Iterable[str]) -> int:
    """멱등 upsert: 주어진 key로 기존 행 삭제 후 삽입.

    DuckDB의 ON CONFLICT는 컬럼 순서/제약 의존성이 있어, 멱등성을 명시적으로
    DELETE-then-INSERT로 보장한다 (스펙 §6: 같은 날 두 번 돌려도 중복 없음).
    """
    if not rows:
        return 0
    key_cols = list(key_cols)
    cols = list(rows[0].keys())
    # 1) 기존 키 삭제
    for r in rows:
        where = " AND ".join(f"{k} = ?" for k in key_cols)
        con.execute(f"DELETE FROM {table} WHERE {where}", [r[k] for k in key_cols])
    # 2) 삽입
    placeholders = ", ".join("?" for _ in cols)
    col_list = ", ".join(cols)
    con.executemany(
        f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})",
        [[r[c] for c in cols] for r in rows],
    )
    return len(rows)
