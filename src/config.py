"""설정 로더. config/*.yaml 을 읽어 dict로 제공한다.

모든 도메인 사실은 config에 있고 코드는 파라미터화되어 있어야 한다 (SPCX 외 IPO 재사용).
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

# 프로젝트 루트 = 이 파일의 두 단계 상위 (src/config.py → repo root)
ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
INBOX_DIR = DATA_DIR / "inbox"
EXTRACTED_DIR = DATA_DIR / "extracted"
RAW_DIR = DATA_DIR / "raw"
REPORTS_DIR = ROOT / "reports"
DB_PATH = DATA_DIR / "spcx.duckdb"


def _read_yaml(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@lru_cache(maxsize=None)
def load_spcx() -> dict[str, Any]:
    """config/spcx.yaml — 도메인 사실 + 이벤트 파라미터."""
    return _read_yaml(CONFIG_DIR / "spcx.yaml")


@lru_cache(maxsize=None)
def load_sources() -> dict[str, Any]:
    """config/sources.yaml — 데이터 소스별 URL/지연/주기."""
    return _read_yaml(CONFIG_DIR / "sources.yaml")


def ensure_dirs() -> None:
    """파이프라인이 쓰는 디렉토리들을 보장."""
    for d in (DATA_DIR, INBOX_DIR, EXTRACTED_DIR, RAW_DIR, REPORTS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def env(key: str, default: str | None = None) -> str | None:
    """환경변수 (.env는 run.py에서 로드)."""
    return os.environ.get(key, default)
