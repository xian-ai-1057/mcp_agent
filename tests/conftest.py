"""Shared test fixtures."""

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "fixtures"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from glossary.loader import GlossaryLoader  # noqa: E402
from glossary.runtime import get_glossary, set_loader  # noqa: E402
from tools.registry import discover  # noqa: E402

# The plan's Phase 1 tool set: one translation tool and one contrasting tool.
# Criterion 2 is stated against exactly this pair, so it gets its own fixture
# rather than being measured against whatever the registry has grown to.
PHASE1_TOOLS = ("lookup_terms", "get_time")


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture
def load_fixture():
    def _load(name: str) -> dict:
        return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))

    return _load


@pytest.fixture
def glossary():
    return get_glossary()


@pytest.fixture
def all_specs():
    return discover()


@pytest.fixture
def phase1_specs(all_specs):
    return {name: all_specs[name] for name in PHASE1_TOOLS}


@pytest.fixture
def csv_factory(tmp_path):
    """Write a glossary CSV and return its path."""
    counter = {"n": 0}

    def _write(rows: list[tuple[str, str, str, str]], name: str | None = None) -> Path:
        counter["n"] += 1
        path = tmp_path / (name or f"glossary_{counter['n']}.csv")
        lines = ["zh,en,aliases,category"]
        lines.extend(",".join(row) for row in rows)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    return _write


@pytest.fixture
def installed_glossary(csv_factory):
    """Install a temporary glossary as the process-wide one, then restore."""
    created: list[Path] = []

    def _install(rows: list[tuple[str, str, str, str]]) -> tuple[Path, GlossaryLoader]:
        path = csv_factory(rows)
        loader = GlossaryLoader(path)
        set_loader(loader)
        created.append(path)
        return path, loader

    yield _install
    set_loader(None)
