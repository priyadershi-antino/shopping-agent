import json
import logging

from shopping_agent.logging import JsonFormatter, redact


def test_redacts_secret_keys_recursively() -> None:
    data = {
        "api_key": "abc",
        "nested": {"Authorization": "Bearer x", "ok": 1},
        "list": [{"otp": 1}],
    }
    assert redact(data) == {
        "api_key": "[REDACTED]",
        "nested": {"Authorization": "[REDACTED]", "ok": 1},
        "list": [{"otp": "[REDACTED]"}],
    }


def test_redacts_card_numbers_and_url_credentials() -> None:
    assert "4111" not in redact("card 4111 1111 1111 1111 used")
    assert redact("postgresql+asyncpg://user:s3cret@localhost/db") == (
        "postgresql+asyncpg://user:[REDACTED]@localhost/db"
    )


def test_prices_are_not_redacted() -> None:
    assert redact("price 74999 and mrp 99,999") == "price 74999 and mrp 99,999"


def test_json_formatter_includes_extra_and_redacts() -> None:
    record = logging.makeLogRecord(
        {"msg": "tool call", "levelname": "INFO", "name": "t", "tool": "search", "token": "x"}
    )
    payload = json.loads(JsonFormatter().format(record))
    assert payload["msg"] == "tool call"
    assert payload["tool"] == "search"
    assert payload["token"] == "[REDACTED]"
