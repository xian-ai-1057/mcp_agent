"""Guards against documentation drifting away from the code.

The diagrams in `docs/architecture.md` name tools and modules. Renaming a tool
without touching the docs leaves a diagram that is confidently wrong, which is
worse than no diagram — so the cheap parts of that claim are checked here.
"""

import re
from pathlib import Path

import pytest

from tools.registry import discover

DOCS = Path("docs/architecture.md")
README = Path("README.md")
MERMAID_BLOCK = re.compile(r"```mermaid\n(.*?)```", re.DOTALL)


@pytest.fixture(scope="module")
def architecture() -> str:
    return DOCS.read_text(encoding="utf-8")


class TestToolNamesStayInSync:
    def test_every_registered_tool_is_documented(self, architecture):
        missing = [name for name in discover() if name not in architecture]
        assert not missing, f"tools missing from {DOCS}: {missing}"

    def test_the_readme_documents_them_too(self):
        text = README.read_text(encoding="utf-8")
        missing = [name for name in discover() if name not in text]
        assert not missing, f"tools missing from {README}: {missing}"


class TestModuleNamesStayInSync:
    @pytest.mark.parametrize(
        "module",
        [
            "loader.py",
            "scanner.py",
            "matcher.py",
            "normalize.py",
            "registry.py",
            "base.py",
            "gateway.py",
            "bridge.py",
            "mcp_client.py",
            "prompts.py",
            "metrics.py",
            "cli.py",
            "server.py",
        ],
    )
    def test_documented_module_exists(self, architecture, module):
        assert module in architecture, f"{module} is not mentioned in {DOCS}"


class TestMermaidBlocks:
    def test_fences_are_balanced(self, architecture):
        opens = architecture.count("```mermaid")
        blocks = MERMAID_BLOCK.findall(architecture)
        assert len(blocks) == opens, "an unclosed ```mermaid fence would break rendering"

    def test_every_block_declares_a_diagram_type(self, architecture):
        for index, block in enumerate(MERMAID_BLOCK.findall(architecture), start=1):
            first = block.strip().splitlines()[0].strip()
            assert first.startswith(("flowchart", "sequenceDiagram", "stateDiagram")), (
                f"diagram {index} starts with {first!r}"
            )

    def test_sequence_diagrams_avoid_reserved_participant_aliases(self, architecture):
        """`loop`, `alt`, `opt`, `par` and `end` are Mermaid keywords.

        Using one as a participant alias parses locally and then fails to render
        on GitHub — this caught `participant Loop` while the docs were written.
        """
        reserved = {"loop", "alt", "opt", "par", "end", "rect", "critical", "break"}
        for block in MERMAID_BLOCK.findall(architecture):
            if not block.strip().startswith("sequenceDiagram"):
                continue
            for alias in re.findall(r"^\s*(?:participant|actor)\s+(\S+)", block, re.MULTILINE):
                assert alias.lower() not in reserved, f"{alias!r} is a Mermaid keyword"
