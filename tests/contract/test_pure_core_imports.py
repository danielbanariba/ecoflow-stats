"""Architecture guard: the pure core never imports an I/O boundary.

Named defect "Core doing I/O": `devices`, `outages`, `energy`, `battery` and
`grid` hold pure domain logic with no side effects, so they stay trivially
unit-testable and so a detector bug is a re-run, never a permanent error
(design D3/D4). This test scans their real source for a forbidden import and
passes trivially today, since those packages hold no code yet — it keeps
guarding the same promise once Phases 3, 10, 17-20 fill them in, without
being re-authored.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC_ROOT = Path(__file__).resolve().parent.parent.parent / "src" / "ecoflow_stats"
PURE_CORE_PACKAGES = ("devices", "outages", "energy", "battery", "grid")
BANNED_MODULES = frozenset({"sqlite3", "httpx", "fastapi", "starlette", "jinja2", "storage", "web"})


def find_banned_imports(root: Path, banned: frozenset[str]) -> list[str]:
    """Scan every ``.py`` file under ``root`` for an import of a banned
    top-level module and return one ``"<file>: <statement>"`` entry per
    offending import.

    Catches both an absolute import reaching into a forbidden module
    (``import sqlite3``, ``from ecoflow_stats.storage import X``) and a
    relative one from within the package tree (``from .. import storage``).
    """
    violations: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top_level = alias.name.split(".")[0]
                    if top_level in banned:
                        violations.append(f"{path}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                module_top = (node.module or "").split(".")[0]
                if module_top in banned:
                    violations.append(f"{path}: from {node.module} import ...")
                for alias in node.names:
                    if alias.name in banned:
                        violations.append(f"{path}: from ... import {alias.name}")
    return violations


def test_scanner_detects_a_banned_absolute_import(tmp_path: Path) -> None:
    offender = tmp_path / "bad.py"
    offender.write_text("import sqlite3\n")

    violations = find_banned_imports(tmp_path, BANNED_MODULES)

    assert violations == [f"{offender}: import sqlite3"]


def test_scanner_detects_a_banned_relative_from_import(tmp_path: Path) -> None:
    offender = tmp_path / "bad.py"
    offender.write_text("from .. import storage\n")

    violations = find_banned_imports(tmp_path, BANNED_MODULES)

    assert violations == [f"{offender}: from ... import storage"]


def test_scanner_ignores_an_unrelated_import(tmp_path: Path) -> None:
    clean = tmp_path / "clean.py"
    clean.write_text("import json\nfrom dataclasses import dataclass\n")

    violations = find_banned_imports(tmp_path, BANNED_MODULES)

    assert violations == []


@pytest.mark.parametrize("package", PURE_CORE_PACKAGES)
def test_pure_core_package_imports_no_io_module(package: str) -> None:
    violations = find_banned_imports(SRC_ROOT / package, BANNED_MODULES)

    assert violations == []
