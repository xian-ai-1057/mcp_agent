"""Generic post-run policy hooks for capability-specific behaviour."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from agent.gateway import Gateway, ToolCall
from agent.tooling import ToolRunner
from contracts.agent import Initiator, ToolCallRecord

ToolInvoker = Callable[
    [ToolCall, int, Initiator], Awaitable[tuple[str, ToolCallRecord]]
]


@dataclass(frozen=True)
class PolicyContext:
    """Read-only view of a completed generic agent run."""

    user_text: str
    output: str
    messages: list[dict[str, Any]]
    model_turns: int
    max_model_turns: int
    completed: bool
    records: tuple[ToolCallRecord, ...]
    gateway: Gateway
    tools: ToolRunner
    invoke: ToolInvoker


@dataclass(frozen=True)
class PolicyOutcome:
    """A policy's bounded contribution to the final run result."""

    output: str
    records: tuple[ToolCallRecord, ...] = ()
    model_turns: int = 0
    artifacts: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, int | float | str | bool] = field(default_factory=dict)


class RunPolicy(Protocol):
    """Capability hook invoked only after the normal tool loop stops."""

    async def after_run(self, context: PolicyContext) -> PolicyOutcome: ...
