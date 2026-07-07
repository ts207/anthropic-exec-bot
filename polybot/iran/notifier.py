from __future__ import annotations

import os
import re
from typing import Any

import requests

from polybot.log import log_event

# Best-effort emoji marker per message-type, purely cosmetic (falls back to
# no marker for anything unrecognized, including arbitrary article titles).
_EMOJI_BY_PREFIX: list[tuple[str, str]] = [
    ("Location protection execution blocked", "\U0001F6AB"),  # 🚫
    ("Location protection alert only", "\U0001F515"),  # 🔕
    ("Location protection classifier budget blocked", "⏳"),  # ⏳
    ("Location classifier unavailable", "⚠️"),  # ⚠️
    ("Location protection feed summary skipped", "⚠️"),  # ⚠️
    ("Location protection polling cycle failed", "⚠️"),  # ⚠️
    ("Location price band crossed", "\U0001F4C8"),  # 📈
    ("Location market verification failed", "\U0001F6D1"),  # 🛑
    ("Location market verification recovered", "✅"),  # ✅
    ("Location protection heartbeat", "\U0001F493"),  # 💓
]
_CHUNK_TAG_RE = re.compile(r"^\[\d+/\d+\]\n")


def _emoji_for(message: str) -> str:
    for prefix, emoji in _EMOJI_BY_PREFIX:
        if message.startswith(prefix):
            return emoji
    if _CHUNK_TAG_RE.match(message):
        return "\U0001F4F0"  # 📰 -- full-text article/liveblog push
    return ""


def _format_field_value(value: Any) -> str:
    if isinstance(value, float):
        rounded = round(value, 4)
        return str(int(rounded)) if rounded == int(rounded) else str(rounded)
    return str(value)


def render_telegram_message(message: str, fields: dict[str, Any]) -> str:
    """Builds the actual outgoing Telegram text from a bare message + kwargs.

    Historically notify()'s **fields (level, reason, url, error, price,
    threshold, ...) were only ever passed to the local log_event call --
    Telegram itself only ever received the bare `message` string, so alerts
    like "alert only, no trade" or "price band crossed" arrived with none of
    the actual diagnostic detail, which was only visible by SSHing into
    logs. This renders them into the message body instead.

    A field is skipped if its value is already visible verbatim in the
    message text (e.g. the full-text article push already embeds title and
    "Source: <url>", so re-appending title/url as fields would just be
    noise); this also naturally skips None/empty values.
    """
    emoji = _emoji_for(message)
    header = f"{emoji} {message}" if emoji else message
    lines = [header]
    detail_lines = []
    for key, value in fields.items():
        if value is None or value == "":
            continue
        text_value = _format_field_value(value)
        if not text_value or text_value in message:
            continue
        label = key.replace("_", " ").capitalize()
        detail_lines.append(f"{label}: {text_value}")
    if detail_lines:
        lines.append("")
        lines.extend(detail_lines)
    return "\n".join(lines)


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
            text = render_telegram_message(message, fields)
        except Exception:
            text = message
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": text},
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
