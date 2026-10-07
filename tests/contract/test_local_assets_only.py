"""Named defect "External asset" (design-edges testing strategy table):
no template or stylesheet references an `http(s)://` host. Every asset
a page loads must come from this application's own origin (web-ui "All
Assets Served Locally").

A static source scan, not a running server: the `static/vendor/*` and
`static/fonts/*` binaries/license texts are intentionally excluded —
those are the vendored files themselves, never loaded as a network
reference from a page, and `static/fonts/OFL.txt` legitimately contains
attribution URLs as plain license text.
"""

from __future__ import annotations

import re
from pathlib import Path

_WEB_ROOT = Path(__file__).resolve().parent.parent.parent / "src" / "ecoflow_stats" / "web"
_EXTERNAL_HOST = re.compile(r"https?://")


def _scanned_files() -> list[Path]:
    files = list((_WEB_ROOT / "templates").rglob("*.html"))
    files += list((_WEB_ROOT / "static").glob("*.css"))
    files += list((_WEB_ROOT / "static").glob("*.js"))
    assert files, "expected at least one template/CSS/JS file to scan"
    return files


def test_no_page_loads_an_external_asset() -> None:
    """Scenario "No page loads an external asset"."""
    offenders = [
        str(path.relative_to(_WEB_ROOT))
        for path in _scanned_files()
        if _EXTERNAL_HOST.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_the_scan_actually_detects_an_external_reference(tmp_path: Path) -> None:
    """Pass-2, proven directly: a template that *does* reference an
    external host must fail this same check, so the first test is not
    vacuously true against an empty file list."""
    offending = tmp_path / "offending.html"
    offending.write_text('<script src="https://cdn.example.com/lib.js"></script>')

    assert _EXTERNAL_HOST.search(offending.read_text(encoding="utf-8")) is not None
