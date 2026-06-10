"""수집 어댑터 추상 베이스.

원칙(스펙 §B):
- fetch() → normalize() → upsert() 3단계 통일.
- 외부 소스 실패 시 전체 파이프라인이 죽지 않음 → run()이 예외를 잡아 stale 표시하고 진행.
- 스크래핑이 깨지기 쉬운 소스는 raw 응답을 data/raw/ 에 날짜별 캐시(재파싱 가능).
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import date
from typing import Any


@dataclass
class IngestResult:
    series: str
    ok: bool
    rows_upserted: int = 0
    status: str = "active"        # active | stale | interface_only | pending
    error: str | None = None
    notes: str | None = None
    artifacts: list[str] = field(default_factory=list)  # 캐시된 raw 경로 등


class Adapter(abc.ABC):
    """모든 수집 어댑터의 베이스."""
    series: str = "unnamed"
    latency_days: int = 0

    @abc.abstractmethod
    def fetch(self) -> Any:
        """원격 소스에서 raw 데이터를 가져온다 (필요 시 data/raw 캐시)."""

    @abc.abstractmethod
    def normalize(self, raw: Any) -> list[dict]:
        """raw → DB 적재용 dict 목록."""

    @abc.abstractmethod
    def upsert(self, con, rows: list[dict]) -> int:
        """DB 적재. 반환: upsert 행 수."""

    def run(self, con) -> IngestResult:
        """fetch→normalize→upsert. 실패는 IngestResult로 캡슐화(파이프라인 비중단)."""
        try:
            raw = self.fetch()
            rows = self.normalize(raw)
            n = self.upsert(con, rows)
            return IngestResult(self.series, ok=True, rows_upserted=n, status="active")
        except NotImplementedError as e:
            return IngestResult(self.series, ok=False, status="interface_only",
                                error=str(e) or "interface only")
        except Exception as e:  # noqa: BLE001 — 의도적으로 모든 실패를 stale 처리
            return IngestResult(self.series, ok=False, status="stale", error=repr(e))


class PaidAdapter(Adapter):
    """유료 소스용 스텁. 계정 없음 → 인터페이스만 (스펙 §1.4)."""
    def fetch(self):
        raise NotImplementedError(f"{self.series}: 유료 소스, 계정 미보유 (인터페이스만)")

    def normalize(self, raw):
        raise NotImplementedError

    def upsert(self, con, rows):
        raise NotImplementedError
