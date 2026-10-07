"""JSON-catalog i18n without Babel (design D12).

Negotiation order is the ``lang`` cookie, then ``Accept-Language``
q-values, then the configured default language (web-ui "Language
Negotiated From the Browser, With English Fallback"; "Manual Language
Override Persists"). Both ``negotiate_lang`` and ``translator`` are pure
over their inputs — no cookies, headers, or catalog files are read here;
``load_catalogs`` is the one disk-reading boundary, used by the real
routes and proven against the real shipped catalogs by
``tests/contract/test_translation_parity.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

SUPPORTED_LANGS = ("en", "es")
_FALLBACK_LANG = "en"
CATALOG_DIR = Path(__file__).resolve().parent / "i18n"


def _parse_accept_language(header: str | None) -> list[str]:
    """Return primary language subtags from an `Accept-Language` header,
    highest `q` first (e.g. `es-HN,es;q=0.9,en;q=0.5` -> `["es", "es", "en"]`).
    A tag with no explicit `q` defaults to `1.0`, matching RFC 9110."""
    if not header:
        return []
    weighted: list[tuple[float, str]] = []
    for part in header.split(","):
        part = part.strip()
        if not part:
            continue
        tag, _, params = part.partition(";")
        weight = 1.0
        params = params.strip()
        if params.startswith("q="):
            try:
                weight = float(params[2:])
            except ValueError:
                weight = 1.0
        primary = tag.strip().split("-")[0].lower()
        if primary:
            weighted.append((weight, primary))
    weighted.sort(key=lambda entry: entry[0], reverse=True)
    return [primary for _, primary in weighted]


def negotiate_lang(*, cookie: str | None, accept_language: str | None, default_lang: str) -> str:
    """Pick the effective language for one request."""
    if cookie in SUPPORTED_LANGS:
        return cookie
    for primary in _parse_accept_language(accept_language):
        if primary in SUPPORTED_LANGS:
            return primary
    return default_lang if default_lang in SUPPORTED_LANGS else _FALLBACK_LANG


def load_catalogs(catalog_dir: Path | None = None) -> dict[str, dict[str, str]]:
    """Read every supported language's flat dotted-key JSON catalog."""
    directory = catalog_dir if catalog_dir is not None else CATALOG_DIR
    return {
        lang: json.loads((directory / f"{lang}.json").read_text(encoding="utf-8"))
        for lang in SUPPORTED_LANGS
    }


def translator(lang: str, catalogs: Mapping[str, Mapping[str, str]]) -> Callable[..., str]:
    """Return a `t(key)` lookup bound to `lang`. A key missing from
    `lang`'s own catalog falls back to English, then to the key itself,
    so a page never crashes rendering a translation.

    `t(key, count=n)` looks up a pluralized variant instead: `key +
    ".one"` for an exact count of 1, `key + ".other"` for every other
    count (English and Spanish both only distinguish singular/plural,
    unlike languages with more plural forms) -- the same fallback-to-
    English-then-key-itself behavior applies to the suffixed key, so a
    missing plural variant degrades the same way a missing plain key
    already does, rather than crashing. A one-off `"{count} gaps
    awaiting review"` string hard-coded for every count (Named Defect
    "Missing translation" cousin: wrong for a count of exactly one) is
    exactly what this `count` seam exists to let a caller avoid."""
    fallback = catalogs[_FALLBACK_LANG]
    catalog = catalogs.get(lang, fallback)

    def t(key: str, count: int | None = None) -> str:
        lookup_key = key if count is None else f"{key}.{'one' if count == 1 else 'other'}"
        return catalog.get(lookup_key, fallback.get(lookup_key, lookup_key))

    return t


__all__ = ["CATALOG_DIR", "SUPPORTED_LANGS", "load_catalogs", "negotiate_lang", "translator"]
