"""Startup logging configuration and secret/serial redaction.

``configure_logging`` is the application's single logging entry point: it
sets the root logger's level and format, installs a :class:`RedactionFilter`
seeded with every configured secret and device serial, and quiets the
``httpx``/``httpcore`` loggers (their INFO lines carry a device serial in the
request URL) to WARNING. This is the logging half of the "Secrets exposed"
named defect; the full page/API/log contract test lands once the web layer
exists. Nothing here changes what secrets *are* — ``PublicSettings`` remains
the only view safe to hand to a template or JSON response.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from ecoflow_stats.config import Settings

_NOISY_HTTP_LOGGERS = ("httpx", "httpcore")


def mask_serial(serial: str) -> str:
    """Mask a device serial to its last 4 characters, e.g. ``…F0A1``.

    Shared between the logging redaction filter below and the `check`
    command, so a serial is displayed identically wherever it is shown.
    """
    return f"…{serial[-4:]}" if len(serial) > 4 else f"…{serial}"


class RedactionFilter(logging.Filter):
    """Replace configured secret values with ``***`` and device serials with
    ``…<last 4 characters>`` in every formatted log message.

    A message containing none of the configured values passes through
    unchanged — the filter only ever narrows what a log line reveals, it
    never alters unrelated output.
    """

    def __init__(self, secrets: Iterable[str], serials: Iterable[str]) -> None:
        super().__init__()
        self._secrets = tuple(s for s in secrets if s)
        self._serial_map = {s: mask_serial(s) for s in serials if s}

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = self._redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True

    def _redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        for serial, masked in self._serial_map.items():
            text = text.replace(serial, masked)
        return text


def configure_logging(settings: Settings) -> None:
    """Set up root logging with secret/serial redaction for this process.

    Safe to call more than once: any previously installed redaction filter
    is replaced, so the root logger always redacts the settings from the
    most recent call rather than freezing on the first one.
    """
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    root = logging.getLogger()
    for stale in [f for f in root.filters if isinstance(f, RedactionFilter)]:
        root.removeFilter(stale)
    root.addFilter(_build_filter(settings))
    for name in _NOISY_HTTP_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def _build_filter(settings: Settings) -> RedactionFilter:
    secrets = (
        settings.access_key,
        settings.secret_key,
        settings.password,
        settings.ntfy_topic,
        settings.ntfy_token,
    )
    serials = tuple(device.serial for device in settings.devices)
    return RedactionFilter(secrets=secrets, serials=serials)


__all__ = ["RedactionFilter", "configure_logging", "mask_serial"]
