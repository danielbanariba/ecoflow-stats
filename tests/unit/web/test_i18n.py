"""Unit tests for the pure half of i18n (design D12; web-ui "Language
Negotiated From the Browser, With English Fallback" and "Manual Language
Override Persists").

``negotiate_lang`` and ``translator`` take every input as a plain
argument (cookie value, header value, catalog mapping) — no cookies, no
HTTP, no disk access — so the negotiation order and the lookup/fallback
rule are provable without a real request or real catalog files. The real
shipped catalogs are proven by ``tests/contract/test_translation_parity.py``,
and the end-to-end browser-negotiation behavior by
``tests/integration/web/test_i18n.py``.
"""

from __future__ import annotations

from ecoflow_stats.web.i18n import negotiate_lang, translator


def test_a_lang_cookie_wins_over_everything_else() -> None:
    """Pass-1: without cookie priority, a manual override would not
    "persist" across requests once the browser's own header disagrees —
    exactly the "Manual Language Override Persists" requirement."""
    assert negotiate_lang(cookie="en", accept_language="es-HN,es;q=0.9", default_lang="es") == "en"


def test_accept_language_is_used_when_no_cookie_is_set() -> None:
    """Scenario "A Spanish browser preference renders Spanish"."""
    assert (
        negotiate_lang(cookie=None, accept_language="es-HN,es;q=0.9,en;q=0.5", default_lang="en")
        == "es"
    )


def test_the_highest_q_value_wins_when_several_languages_are_offered() -> None:
    """Pass-2 target: dropping the q-value sort (e.g. taking the first
    listed tag unconditionally) would pick "fr" here instead of "en"."""
    assert (
        negotiate_lang(cookie=None, accept_language="fr;q=0.9,en;q=0.95", default_lang="es") == "en"
    )


def test_an_unsupported_preference_falls_back_to_the_configured_default() -> None:
    """Scenario "An unsupported preference falls back to English" (with
    the shipped default of "en")."""
    assert negotiate_lang(cookie=None, accept_language="fr-FR,de;q=0.8", default_lang="en") == "en"


def test_no_cookie_and_no_header_uses_the_configured_default() -> None:
    assert negotiate_lang(cookie=None, accept_language=None, default_lang="es") == "es"


def test_an_unsupported_cookie_value_is_ignored_not_trusted() -> None:
    """A stale or tampered cookie naming an unsupported language must not
    crash lookup or silently win over a valid header preference."""
    assert negotiate_lang(cookie="fr", accept_language="es;q=0.9", default_lang="en") == "es"


_CATALOGS = {
    "en": {"nav.overview": "Overview", "only.in.en": "English only"},
    "es": {"nav.overview": "Resumen"},
}


def test_translator_looks_up_the_requested_languages_catalog() -> None:
    t = translator("es", _CATALOGS)
    assert t("nav.overview") == "Resumen"


def test_translator_falls_back_to_english_for_a_key_missing_in_the_requested_catalog() -> None:
    """Pass-2 target: removing this fallback branch would raise or
    return the raw key instead of the English string for a catalog gap
    (which `test_translation_parity.py` guards against ever shipping,
    but the lookup itself must still degrade safely)."""
    t = translator("es", _CATALOGS)
    assert t("only.in.en") == "English only"


def test_translator_returns_the_key_itself_when_missing_from_every_catalog() -> None:
    t = translator("en", _CATALOGS)
    assert t("totally.unknown.key") == "totally.unknown.key"
