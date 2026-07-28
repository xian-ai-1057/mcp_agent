"""Multi-turn tool-calling orchestration.

Two things live here and nowhere else: the turn budget, and the decision to try
again. `verify_translation` reports; this module decides. Keeping the decision
out of the tool is what lets the tool stay stateless and independently testable,
and lets the retry policy change without touching an MCP schema.

See `specs/003-agent-client/spec.md` §5.
"""

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from agent.bridge import assistant_message, system_message, tool_result_message, user_message
from agent.gateway import AssistantTurn, Gateway, ToolCall
from agent.mcp_client import ToolInvocationError, ToolRunner
from agent.prompts import SYSTEM_PROMPT, retranslate_prompt
from contracts.agent import Initiator, RunMetrics, RunResult, StopReason, ToolCallRecord
from contracts.tools import VerifyResult

logger = logging.getLogger(__name__)

DEFAULT_MAX_TURNS = 6
DEFAULT_MAX_RETRANSLATE = 2


@dataclass(frozen=True)
class TranslationSelfCheck:
    """When to re-translate, and how many times.

    The policy triggers on **observed tool use, not on the user's wording**: if
    the model called the lookup tool, the run was a translation. No keyword
    sniffing, so the agent stays general — a future tool gets its own policy, or
    none at all.

    Tool names live here rather than in the loop so that `AgentLoop` itself names
    no tool.
    """

    lookup_tool: str = "lookup_terms"
    verify_tool: str = "verify_translation"
    max_retranslate: int = DEFAULT_MAX_RETRANSLATE

    def applies(self, called: set[str], available: set[str]) -> bool:
        return self.lookup_tool in called and self.verify_tool in available


@dataclass
class AgentLoop:
    gateway: Gateway
    tools: ToolRunner
    max_turns: int = DEFAULT_MAX_TURNS
    self_check: TranslationSelfCheck | None = field(default_factory=TranslationSelfCheck)
    system_prompt: str = SYSTEM_PROMPT

    @classmethod
    def from_env(cls, gateway: Gateway, tools: ToolRunner, **overrides: Any) -> "AgentLoop":
        settings: dict[str, Any] = {
            "max_turns": int(os.environ.get("AGENT_MAX_TURNS", DEFAULT_MAX_TURNS)),
            "self_check": TranslationSelfCheck(
                max_retranslate=int(
                    os.environ.get("AGENT_MAX_RETRANSLATE", DEFAULT_MAX_RETRANSLATE)
                )
            ),
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
                stop_reason = StopReason.COMPLETED
                break

            messages.append(assistant_message(turn))
            for call in turn.tool_calls:
                content, record = await self._invoke(call, turns)
                records.append(record)
                messages.append(tool_result_message(call, content))

        output = last_content
        verify: VerifyResult | None = None
        retranslations = 0

        if self.self_check is not None and output:
            called = {r.name for r in records}
            if self.self_check.applies(called, self.tools.tool_names):
                output, verify, retranslations, extra_records, extra_turns = (
                    await self._repair(user_text, output, messages, turns)
                )
                records.extend(extra_records)
                turns += extra_turns

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
            verify=verify,
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

    async def _verify(
        self, source: str, translation: str, turn: int
    ) -> tuple[VerifyResult | None, ToolCallRecord]:
        assert self.self_check is not None
        call = ToolCall(
            id=f"policy_verify_{turn}",
            name=self.self_check.verify_tool,
            arguments={"source_text": source, "translation": translation},
        )
        content, record = await self._invoke(call, turn, initiator=Initiator.POLICY)
        if not record.ok:
            return None, record
        try:
            return VerifyResult.model_validate(json.loads(content)), record
        except Exception as exc:  # malformed tool output must not kill the run
            logger.warning("could not read verify_translation result: %s", exc)
            return None, record

    async def _repair(
        self,
        source: str,
        translation: str,
        messages: list[dict[str, Any]],
        turn_offset: int,
    ) -> tuple[str, VerifyResult | None, int, list[ToolCallRecord], int]:
        """Re-translate while terms are still missing, bounded by the cap.

        The cap is what guarantees termination: a model that keeps returning the
        same flawed translation would otherwise loop forever, so the budget —
        not the model's progress — is the stopping condition (criterion 9).
        """
        assert self.self_check is not None
        records: list[ToolCallRecord] = []
        extra_turns = 0

        verify, record = await self._verify(source, translation, turn_offset)
        records.append(record)
        if verify is None:
            return translation, None, 0, records, extra_turns

        conversation = list(messages)
        retranslations = 0

        while verify.hit_rate < 1.0 and retranslations < self.self_check.max_retranslate:
            conversation.append(user_message(retranslate_prompt(source, translation, verify)))
            extra_turns += 1
            # No tools on a repair turn: the terms are already in the prompt, and
            # what is wanted back is text, not another lookup.
            attempt: AssistantTurn = await self.gateway.complete(conversation, tools=None)
            candidate = (attempt.content or "").strip()
            if not candidate:
                break

            retranslations += 1
            conversation.append(assistant_message(attempt))

            new_verify, new_record = await self._verify(
                source, candidate, turn_offset + extra_turns
            )
            records.append(new_record)
            if new_verify is None:
                break

            # Never accept a worse translation than the one already in hand.
            if new_verify.hit_rate >= verify.hit_rate:
                translation, verify = candidate, new_verify

        return translation, verify, retranslations, records, extra_turns
