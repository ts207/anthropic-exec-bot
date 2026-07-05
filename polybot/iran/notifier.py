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
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_ID")

    def notify(self, message: str, **fields: Any) -> None:
        # Notification must never take down the polling loop, whatever the
        # delivery failure or field names the caller passed.
        try:
            super().notify(message, **fields)
        except Exception:
            pass
        if not self.token or not self.chat_id:
            return
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": message},
                timeout=15,
            ).raise_for_status()
        except Exception as exc:
            try:
                safe_fields = {f"field_{key}" if key in {"message", "error", "event"} else key: value for key, value in fields.items()}
                log_event("iran_notify_error", message=message, error=self._sanitize_error(exc), **safe_fields)
            except Exception:
                pass

    def _sanitize_error(self, exc: Exception) -> str:
        text = str(exc)
        if self.token:
            text = text.replace(self.token, "<redacted>")
        return text
