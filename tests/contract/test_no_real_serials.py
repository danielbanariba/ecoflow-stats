"""Contract test: no fixture file contains what looks like a real EcoFlow
device serial. Introduced here because fixtures first exist in this unit
(Named Defect: real serial committed).
"""

from __future__ import annotations

import re
from pathlib import Path

_SERIAL_PATTERN = re.compile(r"[A-Z]\d{3}[A-Z0-9]{8,}")
_FIXTURES_ROOT = Path(__file__).resolve().parents[1] / "fixtures"


def _scan(root: Path) -> list[str]:
    return sorted(
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file() and _SERIAL_PATTERN.search(path.read_text(encoding="utf-8"))
    )


def test_no_fixture_file_contains_a_real_looking_device_serial() -> None:
    assert _scan(_FIXTURES_ROOT) == []


def test_scanner_detects_a_real_looking_serial_when_present(tmp_path: Path) -> None:
    """Triangulation: the scanner must actually catch a real-looking
    serial, proven against a throwaway directory -- so the assertion above
    cannot be passing merely because nothing was scanned."""
    (tmp_path / "suspicious.json").write_text('{"sn": "X123ABCDEFGH"}', encoding="utf-8")
    assert _scan(tmp_path) == ["suspicious.json"]
