"""Per-request derived values shared by every page route: the negotiated
language and the selected device. Kept as plain functions over plain
values (request cookies/headers already extracted by the caller) so the
device-selection rule is unit-testable without a real `Request`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ecoflow_stats.web.i18n import negotiate_lang

if TYPE_CHECKING:
    from collections.abc import Sequence

LANG_COOKIE = "lang"
DEVICE_COOKIE = "device"
_HTML_LANG = {"en": "en", "es": "es-419"}


def html_lang(lang: str) -> str:
    """The `<html lang>` value: Spanish uses `es-419` (Latin American)
    so client-side `Intl` formatting matches the server's number
    symbols (design D12)."""
    return _HTML_LANG.get(lang, "en")


def negotiate_request_lang(
    *, cookie: str | None, accept_language: str | None, default_lang: str
) -> str:
    return negotiate_lang(cookie=cookie, accept_language=accept_language, default_lang=default_lang)


def select_device_id(
    device_ids: Sequence[int], *, requested: int | None, cookie_value: str | None
) -> int:
    """Pick which configured device a page shows (web-ui "One Device at
    a Time, With a Selector"): an explicitly requested id wins when
    valid, else the remembered cookie when valid, else the first
    configured device -- never an empty or invalid selection."""
    if requested is not None and requested in device_ids:
        return requested
    if cookie_value is not None:
        try:
            cookie_id = int(cookie_value)
        except ValueError:
            cookie_id = None
        else:
            if cookie_id in device_ids:
                return cookie_id
    return device_ids[0]


__all__ = [
    "DEVICE_COOKIE",
    "LANG_COOKIE",
    "html_lang",
    "negotiate_request_lang",
    "select_device_id",
]
