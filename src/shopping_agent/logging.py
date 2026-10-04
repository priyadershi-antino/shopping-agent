"""Structured logging with secret redaction.

Logs are JSON lines by default so agent runs and tool calls can be traced.
Anything that looks like a secret is redacted before it is written.
"""

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

_SECRET_KEYS = re.compile(
    r"^(.*[_-])?(api[_-]?key|token|secret|password|passwd|authorization|cvv|otp|card[_-]?number)$",
    re.IGNORECASE,
)
# Long digit runs (card numbers), credentials embedded in URLs, and secret query params.
_CARD_NUMBER = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_URL_CREDENTIALS = re.compile(r"(://[^:/@\s]+:)[^@\s]+@")
_SECRET_PARAMS = re.compile(
    r"([?&](?:api_key|apikey|key|token|access_token|page_token)=)[^&\s\"']+", re.IGNORECASE
)

_STANDARD_ATTRS = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


def redact(value: Any) -> Any:
    """Recursively redact secret-looking keys and values."""
    if isinstance(value, dict):
        return {
            k: "[REDACTED]" if _SECRET_KEYS.search(str(k)) else redact(v) for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(v) for v in value]
    if isinstance(value, str):
        value = _URL_CREDENTIALS.sub(r"\1[REDACTED]@", value)
        value = _SECRET_PARAMS.sub(r"\1[REDACTED]", value)
        return _CARD_NUMBER.sub("[REDACTED]", value)
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = {k: v for k, v in record.__dict__.items() if k not in _STANDARD_ATTRS}
        if extra:
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(redact(payload), default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return str(redact(super().format(record)))


def configure_logging(level: str = "INFO", json_logs: bool = True) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        JsonFormatter() if json_logs else TextFormatter("%(levelname)s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # httpx logs full request URLs at INFO (SerpAPI puts the key in the query string).
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
