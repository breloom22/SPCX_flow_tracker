"""Telegram Notifier — Bot API 직접 호출(httpx, 추가 의존성 없음).

.env:
  TELEGRAM_BOT_TOKEN=123456:ABC...
  TELEGRAM_CHAT_ID=123456789

토큰/챗ID 미설정이면 NullNotifier처럼 no-op (파이프라인 비중단).
"""
from __future__ import annotations

import httpx

from ..config import env
from .base import Notifier


class TelegramNotifier(Notifier):
    def __init__(self, token: str | None = None, chat_id: str | None = None,
                 timeout: float = 15.0):
        self.token = token or env("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or env("TELEGRAM_CHAT_ID")
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, title: str, body: str) -> bool:
        if not self.configured:
            return False
        text = f"*{title}*\n{body}"
        # Telegram 메시지 길이 제한 4096
        text = text[:4000]
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "Markdown"}
        try:
            with httpx.Client(timeout=self.timeout) as c:
                r = c.post(url, json=payload)
                r.raise_for_status()
            return True
        except Exception:  # noqa: BLE001 — 알림 실패가 파이프라인을 죽이면 안 됨
            return False


def get_notifier() -> Notifier:
    """설정되어 있으면 Telegram, 아니면 Null."""
    from .base import NullNotifier
    tn = TelegramNotifier()
    return tn if tn.configured else NullNotifier()
