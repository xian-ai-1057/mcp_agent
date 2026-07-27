"""Tool discovery.

Adding a tool is adding a file. This module is the reason that is true, and
acceptance criterion 6 is the reason it stays true.

See `specs/002-mcp-tools/spec.md` §3.
"""

import importlib
import logging
import pkgutil
from types import ModuleType

import tools
from tools.base import ToolSpec

logger = logging.getLogger(__name__)

# Infrastructure, not tools. Everything else in the package is fair game.
EXCLUDED = {"base", "registry"}


def _candidate_modules(package: ModuleType) -> list[str]:
    return sorted(
        name
        for _, name, is_pkg in pkgutil.iter_modules(package.__path__)
        if not is_pkg and not name.startswith("_") and name not in EXCLUDED
    )


def discover(package: ModuleType = tools) -> dict[str, ToolSpec]:
    """Import every tool module and collect its `SPEC`, keyed by tool name.

    A module without `SPEC` is skipped with a warning rather than failing the
    load — a shared helper living in the package is not an error. A *duplicate
    tool name* does fail the load, loudly: silently shadowing one tool with
    another would be near-impossible to diagnose from the model's behaviour.
    """
    specs: dict[str, ToolSpec] = {}
    owners: dict[str, str] = {}

    for module_name in _candidate_modules(package):
        qualified = f"{package.__name__}.{module_name}"
        module = importlib.import_module(qualified)
        spec = getattr(module, "SPEC", None)

        if spec is None:
            logger.warning("%s defines no SPEC; skipping", qualified)
            continue
        if not isinstance(spec, ToolSpec):
            raise TypeError(f"{qualified}.SPEC must be a ToolSpec, got {type(spec).__name__}")
        if spec.name in specs:
            raise ValueError(
                f"duplicate tool name {spec.name!r}: defined in both "
                f"{owners[spec.name]} and {qualified}"
            )

        specs[spec.name] = spec
        owners[spec.name] = qualified

    logger.info("discovered %d tools: %s", len(specs), ", ".join(sorted(specs)))
    return specs
