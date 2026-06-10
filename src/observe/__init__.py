"""Module B — Flow Observation Layer (관측).

Phase 1: 인터페이스/스텁만. 실제 어댑터(yfinance, QQQ 보유내역 diff, ETF shares,
Form4/144 watcher, FINRA, ICI 등)는 Phase 2에서 src/ingest/ 어댑터로 구현.
유료 소스(IBKR borrow fee 등)는 ingest/base.py:PaidAdapter 로 인터페이스만.
"""
