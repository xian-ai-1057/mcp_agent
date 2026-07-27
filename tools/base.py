"""The tool contract.

Every module in `tools/` exposes exactly one module-level `SPEC`. Name,
description and schema travel with the implementation, which is what lets
`server.py` register tools without knowing any of them by name.

See `specs/002-mcp-tools/spec.md` §2.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Callable

NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


class ToolError(Exception):
    """An expected failure, reported to the model as an error result.

    Use this for conditions the caller could plausibly recover from: an unknown
    timezone, an unreachable weather provider, a term that isn't in the glossary.
    Programming errors should propagate instead — they are not the model's
    problem to route around.
    """


@dataclass(frozen=True)
class ToolSpec:
    """One tool: what it is called, when to call it, what it accepts, what it does."""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], dict[str, Any]]
    tags: tuple[str, ...] = field(default=())

    def __post_init__(self) -> None:
        if not NAME_PATTERN.match(self.name):
            raise ValueError(f"tool name {self.name!r} must match {NAME_PATTERN.pattern}")
        if not self.description.strip():
            raise ValueError(f"tool {self.name!r} has an empty description")
        if self.input_schema.get("type") != "object":
            raise ValueError(f"tool {self.name!r} input_schema root must be type 'object'")
        if not callable(self.handler):
            raise ValueError(f"tool {self.name!r} handler is not callable")

    def run(self, arguments: dict[str, Any] | None) -> dict[str, Any]:
        return self.handler(arguments or {})


def object_schema(
    properties: dict[str, Any],
    required: list[str] | None = None,
) -> dict[str, Any]:
    """Build a strict object schema. `additionalProperties` is always false."""
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }
