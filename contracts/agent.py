"""Client-layer contracts: what one agent run produced and how it behaved.

`RunMetrics.called_any_tool` exists because the offline experiment measured a
49-55 point quality gap between plain and glossary-assisted translation. A run
where the model skipped the tool is not an error, but it must be *countable* —
see spec 003.
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from contracts.tools import VerifyResult


class StopReason(StrEnum):
    COMPLETED = "completed"
    MAX_TURNS = "max_turns"


class Initiator(StrEnum):
    """Who asked for the call.

    The self-check policy calls `verify_translation` on the agent's behalf. That
    call is real and must be recorded, but it is not evidence that the *model*
    chose a tool — so tool-call counts and routing accuracy only ever look at
    `MODEL` records.
    """

    MODEL = "model"
    POLICY = "policy"


class ToolCallRecord(BaseModel):
    """One tool invocation attempted during a run."""

    model_config = ConfigDict(frozen=True)

    name: str
    arguments: dict = Field(default_factory=dict)
    ok: bool = True
    error: str | None = None
    turn: int = 0
    initiator: Initiator = Initiator.MODEL


class RunMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    turns: int = 0
    tool_calls: int = 0
    tool_names: list[str] = Field(default_factory=list)
    called_any_tool: bool = False
    retranslations: int = 0
    stop_reason: StopReason = StopReason.COMPLETED


class RunResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    output: str = ""
    metrics: RunMetrics = Field(default_factory=RunMetrics)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    verify: VerifyResult | None = None
