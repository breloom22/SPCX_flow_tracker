"""Notifier 추상 인터페이스 (스펙 §D: 웹훅/텔레그램은 인터페이스로)."""
from __future__ import annotations

import abc


class Notifier(abc.ABC):
    @abc.abstractmethod
    def send(self, title: str, body: str) -> bool:
        """알림 전송. 성공 True. 미설정/실패 시 False (파이프라인 비중단)."""


class NullNotifier(Notifier):
    """미설정 시 no-op."""
    def send(self, title: str, body: str) -> bool:
        return False
