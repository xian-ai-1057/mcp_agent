"""Capability-neutral model → tool → model orchestration."""

import logging
import os
from dataclasses import dataclass, field
from typing import Any

from agent.bridge import assistant_message, system_message, tool_result_message, user_message
from agent.gateway import Gateway, ToolCall
from agent.policy import PolicyContext, RunPolicy
from agent.prompts import SYSTEM_PROMPT
from agent.tooling import ToolInvocationError, ToolRunner
from contracts.agent import Initiator, RunMetrics, RunResult, StopReason, ToolCallRecord

logger = logging.getLogger(__name__)

DEFAULT_MAX_TURNS = 6


@dataclass
class AgentLoop:
    gateway: Gateway
    tools: ToolRunner
    max_turns: int = DEFAULT_MAX_TURNS
    policies: tuple[RunPolicy, ...] = field(default_factory=tuple)
    # Kept for source compatibility with 0.3.x callers. New code should pass a
    # capability policy in ``policies``; the generic default enables none.
    self_check: RunPolicy | None = None
    system_prompt: str = SYSTEM_PROMPT

    def __post_init__(self) -> None:
        if (
            not isinstance(self.max_turns, int)
            or isinstance(self.max_turns, bool)
            or self.max_turns < 1
        ):
            raise ValueError("max_turns must be a positive integer")

    @classmethod
    def from_env(cls, gateway: Gateway, tools: ToolRunner, **overrides: Any) -> "AgentLoop":
        settings: dict[str, Any] = {
            "max_turns": int(os.environ.get("AGENT_MAX_TURNS", DEFAULT_MAX_TURNS)),
        }
        settings.update(overrides)
        return cls(gateway=gateway, tools=tools, **settings)

    async def run(self, user_text: str) -> RunResult:
        messages: list[dict[str, Any]] = [
            system_message(self.system_prompt),
            user_message(user_text),
        ]
        openai_tools = self.tools.openai_tools
        records: list[ToolCallRecord] = []
        turns = 0
        last_content = ""
        stop_reason = StopReason.MAX_TURNS

        for _ in range(self.max_turns):
            turns += 1
            turn = await self.gateway.complete(messages, tools=openai_tools)

            if turn.content:
                last_content = turn.content

            if not turn.wants_tools:
                # A model that answers without calling anything is a normal,
                # recorded outcome — not an error. The plan refuses to assume
                # tool use, and an assumption you refuse to make is one you have
                # to measure (see `RunMetrics.called_any_tool`).
                # The current terminal turn is authoritative, including an
                # intentionally empty response. Do not leak an earlier tool-call
                # preamble into the final answer.
                last_content = turn.refusal or turn.content or ""
                if turn.refusal:
                    stop_reason = StopReason.REFUSED
                elif turn.finish_reason == "length":
                    stop_reason = StopReason.LENGTH_LIMIT
                elif turn.finish_reason == "content_filter":
                    stop_reason = StopReason.CONTENT_FILTER
                else:
                    stop_reason = StopReason.COMPLETED
                break

            messages.append(assistant_message(turn))
            for call in turn.tool_calls:
                content, record = await self._invoke(call, turns)
                records.append(record)
                messages.append(tool_result_message(call, content))

        output = last_content
        artifacts: dict[str, Any] = {}
        policy_metrics: dict[str, int | float | str | bool] = {}
        active_policies = self.policies + (
            (self.self_check,) if self.self_check is not None else ()
        )
        for policy in active_policies:
            outcome = await policy.after_run(
                PolicyContext(
                    user_text=user_text,
                    output=output,
                    messages=list(messages),
                    model_turns=turns,
                    max_model_turns=self.max_turns,
                    completed=stop_reason is StopReason.COMPLETED,
                    records=tuple(records),
                    gateway=self.gateway,
                    tools=self.tools,
                    invoke=self._invoke,
                )
            )
            output = outcome.output
            records.extend(outcome.records)
            turns += outcome.model_turns
            artifacts.update(outcome.artifacts)
            policy_metrics.update(outcome.metrics)

        retranslations = int(policy_metrics.get("translation.retranslations", 0))

        return RunResult(
            output=output,
            metrics=RunMetrics(
                turns=turns,
                tool_calls=sum(1 for r in records if r.initiator is Initiator.MODEL),
                tool_names=[r.name for r in records if r.initiator is Initiator.MODEL],
                called_any_tool=any(r.initiator is Initiator.MODEL for r in records),
                retranslations=retranslations,
                stop_reason=stop_reason,
            ),
            tool_calls=records,
            verify=artifacts.get("translation.verify"),
            artifacts=artifacts,
            policy_metrics=policy_metrics,
        )

    async def _invoke(
        self,
        call: ToolCall,
        turn: int,
        initiator: Initiator = Initiator.MODEL,
    ) -> tuple[str, ToolCallRecord]:
        """Run one tool call. Failures come back as text for the model to read."""
        if call.parse_error:
            message = f"Error: {call.parse_error}. Raw arguments: {call.raw_arguments!r}"
            return message, ToolCallRecord(
                name=call.name,
                arguments={},
                ok=False,
                error=call.parse_error,
                turn=turn,
                initiator=initiator,
            )

        try:
            content = await self.tools.call(call.name, call.arguments)
        except ToolInvocationError as exc:
            logger.info("tool %s failed: %s", call.name, exc)
            return f"Error: {exc}", ToolCallRecord(
                name=call.name,
                arguments=call.arguments,
                ok=False,
                error=str(exc),
                turn=turn,
                initiator=initiator,
            )

        return content, ToolCallRecord(
            name=call.name,
            arguments=call.arguments,
            ok=True,
            turn=turn,
            initiator=initiator,
        )
