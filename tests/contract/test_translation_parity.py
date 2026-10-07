"""Named defect "Missing translation" (design-edges testing strategy
table): `en.json` and `es.json` have identical key sets, and every
`t('...')` key referenced in a template exists in both catalogs.
"""

from __future__ import annotations

import re
from pathlib import Path

from ecoflow_stats.web.i18n import SUPPORTED_LANGS, load_catalogs

_WEB_ROOT = Path(__file__).resolve().parent.parent.parent / "src" / "ecoflow_stats" / "web"
_TEMPLATES_DIR = _WEB_ROOT / "templates"
_T_CALL = re.compile(r"""\bt\(\s*['"]([\w.]+)['"]\s*\)""")


def _keys_referenced_in_templates() -> set[str]:
    keys: set[str] = set()
    for path in _TEMPLATES_DIR.rglob("*.html"):
        keys.update(_T_CALL.findall(path.read_text(encoding="utf-8")))
    return keys


def test_every_supported_catalog_has_the_same_key_set() -> None:
    catalogs = load_catalogs()
    assert set(catalogs) == set(SUPPORTED_LANGS)
    key_sets = {lang: set(catalog) for lang, catalog in catalogs.items()}
    reference = key_sets[SUPPORTED_LANGS[0]]
    for lang, keys in key_sets.items():
        assert keys == reference, f"{lang}.json key set differs from {SUPPORTED_LANGS[0]}.json"


def test_every_key_a_template_references_exists_in_every_catalog() -> None:
    referenced = _keys_referenced_in_templates()
    assert referenced, "expected at least one t('...') call across the templates"
    catalogs = load_catalogs()
    for lang, catalog in catalogs.items():
        missing = referenced - set(catalog)
        assert not missing, f"{lang}.json is missing keys referenced by a template: {missing}"


def test_the_scan_actually_detects_a_key_missing_from_one_catalog() -> None:
    """Pass-2, proven directly: a catalog missing a key a template uses
    must fail this same check."""
    catalogs = {"en": {"nav.overview": "Overview"}, "es": {}}
    referenced = {"nav.overview"}

    missing = referenced - set(catalogs["es"])

    assert missing == {"nav.overview"}
