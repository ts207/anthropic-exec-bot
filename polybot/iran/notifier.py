from __future__ import annotations

import os
from typing import Any

import requests

from polybot.log import log_event


class Notifier:
    def notify(self, message: str, **fields: Any) -> None:
        log_event("iran_notify", message=message, **fields)


class TelegramNotifier(Notifier):
    def __init__(self) -> None:
        self.token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID")

    def notify(self, message: str, **fields: Any) -> None:
        super().notify(message, **fields)
        if not self.token or not self.chat_id:
            return
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": message},
                timeout=15,
            ).raise_for_status()
        except requests.RequestException as exc:
            log_event("iran_notify_error", message=message, error=str(exc), **fields)
