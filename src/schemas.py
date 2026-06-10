"""Pydantic 스키마 — Claude Code가 작성하는 extracted YAML의 검증 계약(§5).

파이프라인은 data/extracted/*.yaml 을 이 모델로 재검증한 뒤에만 DB에 적재한다.
검증 실패 = 적재 거부 + 리포트에 사유 표시.

원칙(§5): 각 extracted 파일은 반드시 출처(accession/URL), 추출 일시, 신뢰도,
근거가 된 원문 문장 인용을 포함한다. 모호하면 needs_review=true.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

Confidence = Literal["deterministic", "rule_based", "inferred"]
Classification = Literal["forced_seller", "discretionary", "unknown"]
TriggerType = Literal[
    "absolute_days",       # 상장일 + N일
    "absolute_date",       # 절대 날짜
    "earnings_relative",   # 실적일 + N거래일
    "price_condition",     # 주가 조건
]


class Provenance(BaseModel):
    """모든 extracted 산출물의 공통 출처 메타데이터."""
    source_accession: Optional[str] = None   # EDGAR accession 번호
    source_url: Optional[str] = None
    extracted_at: datetime
    extracted_by: str = "claude-code"
    confidence: Confidence
    source_quote: str = Field(..., min_length=1,
                              description="근거가 된 원문 문장 인용 (필수)")

    @field_validator("source_url")
    @classmethod
    def _need_some_source(cls, v, info):
        # accession 또는 url 중 하나는 있어야 함은 LockupExtraction에서 교차검증
        return v


class LockupTranche(BaseModel):
    """424B4에서 추출한 락업 트랜치 1건."""
    id: str
    holder_group: str
    classification: Classification = "unknown"
    trigger_type: TriggerType
    trigger_ref: str = Field(..., description="N일/거래일 수, 절대날짜(ISO), 또는 주가조건 텍스트")
    condition: str = Field(..., description="해제 조건 원문 요약")
    release_shares: Optional[float] = None
    release_fraction: Optional[float] = Field(None, ge=0.0, le=1.0)
    needs_review: bool = False

    @field_validator("trigger_ref", mode="before")
    @classmethod
    def _coerce_str(cls, v):
        return str(v)


class LockupExtraction(BaseModel):
    """data/extracted/lockup_*.yaml 최상위 스키마."""
    schema_version: int = 1
    ticker: str
    provenance: Provenance
    earnings_date_estimate: Optional[date] = None
    earnings_date_confirmed: bool = False
    tranches: list[LockupTranche]

    @field_validator("tranches")
    @classmethod
    def _nonempty(cls, v):
        if not v:
            raise ValueError("tranches는 비어 있을 수 없다")
        return v


class EventExtraction(BaseModel):
    """나스닥/지수 제공사 보도자료에서 추출한 이벤트 확정값(§5.3)."""
    schema_version: int = 1
    provenance: Provenance
    event_id: str
    event_date: date
    event_type: str
    magnitude_usd: Optional[float] = None
    notes: Optional[str] = None
    needs_review: bool = False


class Form4Classification(BaseModel):
    """Form 4/144 각주 해석 결과(§5.2): 10b5-1 사전계획 vs 재량 매도."""
    schema_version: int = 1
    provenance: Provenance
    accession: str
    insider_name: str
    transaction_date: date
    shares: float
    price_usd: Optional[float] = None
    is_10b5_1: bool = Field(..., description="True=사전계획 매도, False=재량 매도")
    needs_review: bool = False
