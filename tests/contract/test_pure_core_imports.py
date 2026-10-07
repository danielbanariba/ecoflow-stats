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
BANNED_PACKAGE_PREFIXES = frozenset(
    {
        # This app's own I/O-boundary packages ("shell"), reached by their
        # real, fully-qualified dotted name -- the shape every import in
        # this codebase actually uses (`from ecoflow_stats.storage.legacy
        # import LegacyStore`, never a bare `from storage import ...`).
        # `BANNED_MODULES` above only ever catches the bare form.
        "ecoflow_stats.storage",
        "ecoflow_stats.acquisition",
        "ecoflow_stats.web",
        "ecoflow_stats.notifications",
        "ecoflow_stats.history_import",
        "ecoflow_stats.bootstrap",
        "ecoflow_stats.jobs",
        "ecoflow_stats.cli",
    }
)


def _matches_banned_prefix(module: str, prefixes: frozenset[str]) -> bool:
    """True when ``module`` is exactly one of ``prefixes``, or a
    sub-module of one (``ecoflow_stats.storage.legacy`` matches
    ``ecoflow_stats.storage``) -- compared whole dotted segment by
    segment, so ``ecoflow_stats.storageshed`` never falsely matches
    ``ecoflow_stats.storage``."""
    parts = module.split(".")
    return any(parts[: len(prefix.split("."))] == prefix.split(".") for prefix in prefixes)


def find_banned_imports(
    root: Path,
    banned: frozenset[str],
    banned_prefixes: frozenset[str] = frozenset(),
) -> list[str]:
    """Scan every ``.py`` file under ``root`` for an import of a banned
    top-level module, or a banned fully-qualified package prefix, and
    return one ``"<file>: <statement>"`` entry per offending import.

    Catches an absolute import reaching into a forbidden module (``import
    sqlite3``), a relative one from within the package tree (``from ..
    import storage``), and -- via ``banned_prefixes`` -- this codebase's
    own universal fully-qualified style (``from ecoflow_stats.storage.
    legacy import LegacyStore``), regardless of whether the import sits
    inside an ``if TYPE_CHECKING:`` block: ``ast.walk`` descends into
    every branch's body unconditionally, so a guard that only exists for
    static type checkers offers no runtime-import exemption here either.
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
                module = node.module or ""
                module_top = module.split(".")[0]
                if module_top in banned or _matches_banned_prefix(module, banned_prefixes):
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


def test_scanner_detects_a_fully_qualified_banned_package_import(tmp_path: Path) -> None:
    """A real gap this scanner had: `module_top` only ever compared the
    FIRST dotted segment (always `ecoflow_stats` in this codebase's own
    universal import style), so `from ecoflow_stats.storage.legacy import
    LegacyStore` inside a pure-core package was never flagged -- exactly
    the shape of import that let `devices/registry.py` reach into
    `acquisition.ecoflow_client` undetected."""
    offender = tmp_path / "bad.py"
    offender.write_text("from ecoflow_stats.storage.legacy import LegacyStore\n")

    violations = find_banned_imports(tmp_path, BANNED_MODULES, BANNED_PACKAGE_PREFIXES)

    assert violations == [f"{offender}: from ecoflow_stats.storage.legacy import ..."]


def test_scanner_detects_a_fully_qualified_banned_import_inside_type_checking(
    tmp_path: Path,
) -> None:
    """The same gap, guarded by `if TYPE_CHECKING:` -- the exact shape
    every offending `devices/*` import used. `ast.walk` already descends
    into an `if` block's body regardless of the branch condition, so this
    proves the fix catches it there too, not only at module level."""
    offender = tmp_path / "bad.py"
    offender.write_text(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from ecoflow_stats.acquisition.ecoflow_client import DeviceInfo\n"
    )

    violations = find_banned_imports(tmp_path, BANNED_MODULES, BANNED_PACKAGE_PREFIXES)

    assert violations == [f"{offender}: from ecoflow_stats.acquisition.ecoflow_client import ..."]


def test_scanner_does_not_flag_an_allowed_fully_qualified_import(tmp_path: Path) -> None:
    """A pure-core module importing from another pure-core package (or
    from `ports.py`) by its fully-qualified name must stay clean -- the
    new prefix check must not over-match on the shared `ecoflow_stats.`
    root every import in this codebase starts with."""
    clean = tmp_path / "clean.py"
    clean.write_text("from ecoflow_stats.outages.model import Event\n")

    violations = find_banned_imports(clean.parent, BANNED_MODULES, BANNED_PACKAGE_PREFIXES)

    assert violations == []


@pytest.mark.parametrize("package", PURE_CORE_PACKAGES)
def test_pure_core_package_imports_no_io_module(package: str) -> None:
    violations = find_banned_imports(SRC_ROOT / package, BANNED_MODULES, BANNED_PACKAGE_PREFIXES)

    assert violations == []
