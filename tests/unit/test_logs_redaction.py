"""Unit tests for ecoflow_stats.logs: secret and serial redaction.

These guard the "Secrets Redacted From Startup Output" requirement and the
"Secrets exposed" named defect (logging half) — every log line must come out
with configured secret values replaced and device serials truncated, never
printed whole, and the noisy httpx/httpcore loggers must not leak a serial
through their own INFO-level request-URL lines.
"""

from __future__ import annotations

import logging

from ecoflow_stats.config import load_settings
from ecoflow_stats.logs import RedactionFilter, configure_logging

VALID_ENV = {
    "ECOFLOW_ACCESS_KEY": "top-secret-access",
    "ECOFLOW_SECRET_KEY": "top-secret-secret",
    "ECOFLOW_DEVICES": "TESTDEV0001",
}

OTHER_ENV = {
    "ECOFLOW_ACCESS_KEY": "a-different-access-key",
    "ECOFLOW_SECRET_KEY": "a-different-secret-key",
    "ECOFLOW_DEVICES": "TESTDEV0002",
}


def _make_record(message: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


def test_filter_redacts_a_configured_secret_value() -> None:
    filt = RedactionFilter(secrets=["top-secret-access"], serials=[])
    record = _make_record("connecting with key=top-secret-access")

    filt.filter(record)

    assert "top-secret-access" not in record.getMessage()
    assert "***" in record.getMessage()


def test_filter_truncates_a_device_serial_to_its_last_four_characters() -> None:
    filt = RedactionFilter(secrets=[], serials=["TESTDEV0001"])
    record = _make_record("collected sample for device TESTDEV0001")

    filt.filter(record)

    assert "TESTDEV0001" not in record.getMessage()
    assert record.getMessage() == "collected sample for device …0001"


def test_filter_leaves_an_unrelated_message_unchanged() -> None:
    """Triangulates against a filter that redacts unconditionally: a message
    containing none of the configured secrets or serials must pass through
    byte-for-byte."""
    filt = RedactionFilter(secrets=["top-secret-access"], serials=["TESTDEV0001"])
    record = _make_record("tick completed in 42ms")

    filt.filter(record)

    assert record.getMessage() == "tick completed in 42ms"


def test_configure_logging_sets_httpx_and_httpcore_loggers_to_warning() -> None:
    settings = load_settings(VALID_ENV)

    configure_logging(settings)

    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def test_configure_logging_installs_a_redaction_filter_on_the_root_logger() -> None:
    settings = load_settings(VALID_ENV)

    configure_logging(settings)

    assert any(isinstance(f, RedactionFilter) for f in logging.getLogger().filters)


def test_configure_logging_redacts_the_configured_access_key_end_to_end() -> None:
    """Exercises the real path: Settings -> configure_logging -> the filter it
    builds, proving the two are actually wired together, not just unit-level
    compatible."""
    settings = load_settings(VALID_ENV)
    configure_logging(settings)
    installed = next(f for f in logging.getLogger().filters if isinstance(f, RedactionFilter))
    record = _make_record("startup access_key=top-secret-access")

    installed.filter(record)

    assert "top-secret-access" not in record.getMessage()


def test_configure_logging_called_again_redacts_the_newest_settings() -> None:
    """Regression: a first call must not freeze the filter on its own
    secrets forever. If `configure_logging` is ever called a second time
    (for example, a future reload path) with different settings, the
    installed filter must redact the *current* secret, not a stale one from
    the first call."""
    configure_logging(load_settings(VALID_ENV))
    configure_logging(load_settings(OTHER_ENV))
    record = _make_record("startup access_key=a-different-access-key")

    for filt in logging.getLogger().filters:
        filt.filter(record)

    assert "a-different-access-key" not in record.getMessage()
