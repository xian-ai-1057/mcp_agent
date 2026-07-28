"""Bounded translation verification implemented outside the agent core."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from agent.bridge import assistant_message, user_message
from agent.gateway import ToolCall
from agent.policy import PolicyContext, PolicyOutcome
from capabilities.translation.prompts import retranslate_prompt
from contracts.agent import Initiator, ToolCallRecord
from contracts.tools import VerifyResult

logger = logging.getLogger(__name__)

DEFAULT_MAX_RETRANSLATE = 2


@dataclass(frozen=True)
class TranslationSelfCheck:
    """Verify successful glossary-assisted translations and repair misses."""

    lookup_tool: str = "lookup_terms"
    verify_tool: str = "verify_translation"
    max_retranslate: int = DEFAULT_MAX_RETRANSLATE

    def __post_init__(self) -> None:
        if (
            not isinstance(self.max_retranslate, int)
            or isinstance(self.max_retranslate, bool)
            or self.max_retranslate < 0
        ):
            raise ValueError("max_retranslate must be a non-negative integer")

    def applies(self, called: set[str], available: set[str]) -> bool:
        """Whether this policy has one unambiguous pair of usable tools.

        MCP server prefixes are part of the public tool name.  A translation
        server configured with ``tool_prefix=\"translation\"`` therefore
        exposes ``translation__lookup_terms`` rather than ``lookup_terms``.
        Prefer an exact name, otherwise accept a single namespaced match; more
        than one match is intentionally treated as ambiguous instead of making
        an order-dependent dispatch decision.
        """
        resolved = self._resolve_tools(available)
        return resolved is not None and resolved[0] in called

    def _resolve_tools(self, available: set[str]) -> tuple[str, str] | None:
        # Never pair tools from different MCP namespaces: that could route
        # translation content from one capability/server into another.  The
        # unprefixed pair is canonical when both names exist.
        if self.lookup_tool in available and self.verify_tool in available:
            return self.lookup_tool, self.verify_tool

        lookup_suffix = f"__{self.lookup_tool}"
        verify_suffix = f"__{self.verify_tool}"
        lookups = {
            name[: -len(lookup_suffix)]: name
            for name in available
            if name.endswith(lookup_suffix) and len(name) > len(lookup_suffix)
        }
        verifiers = {
            name[: -len(verify_suffix)]: name
            for name in available
            if name.endswith(verify_suffix) and len(name) > len(verify_suffix)
        }
        common_prefixes = sorted(set(lookups) & set(verifiers))
        if len(common_prefixes) != 1:
            return None
        prefix = common_prefixes[0]
        return lookups[prefix], verifiers[prefix]

    async def after_run(self, context: PolicyContext) -> PolicyOutcome:
        successful_model_calls = {
            record.name
            for record in context.records
            if record.ok and record.initiator is Initiator.MODEL
        }
        resolved = self._resolve_tools(context.tools.tool_names)
        if (
            not context.completed
            or not context.output
            or resolved is None
            or resolved[0] not in successful_model_calls
        ):
            return PolicyOutcome(output=context.output)
        _, verify_tool = resolved

        records: list[ToolCallRecord] = []
        output = context.output
        verify, record = await self._verify(
            context, output, context.model_turns, verify_tool
        )
        records.append(record)
        if verify is None:
            return PolicyOutcome(output=output, records=tuple(records))

        conversation = list(context.messages)
        retranslations = 0
        extra_turns = 0
        remaining_turns = max(0, context.max_model_turns - context.model_turns)

        while (
            verify.hit_rate < 1.0
            and retranslations < self.max_retranslate
            and extra_turns < remaining_turns
        ):
            conversation.append(
                user_message(retranslate_prompt(context.user_text, output, verify))
            )
            attempt = await context.gateway.complete(conversation, tools=None)
            extra_turns += 1
            candidate = (attempt.content or "").strip()
            if not candidate:
                break

            retranslations += 1
            conversation.append(assistant_message(attempt))
            candidate_verify, candidate_record = await self._verify(
                context, candidate, context.model_turns + extra_turns, verify_tool
            )
            records.append(candidate_record)
            if candidate_verify is None:
                break
            if candidate_verify.hit_rate >= verify.hit_rate:
                output, verify = candidate, candidate_verify

        return PolicyOutcome(
            output=output,
            records=tuple(records),
            model_turns=extra_turns,
            artifacts={"translation.verify": verify.model_dump(mode="json")},
            metrics={"translation.retranslations": retranslations},
        )

    async def _verify(
        self,
        context: PolicyContext,
        translation: str,
        turn: int,
        verify_tool: str,
    ) -> tuple[VerifyResult | None, ToolCallRecord]:
        call = ToolCall(
            id=f"policy_verify_{turn}",
            name=verify_tool,
            arguments={"source_text": context.user_text, "translation": translation},
        )
        content, record = await context.invoke(call, turn, Initiator.POLICY)
        if not record.ok:
            return None, record
        try:
            return VerifyResult.model_validate(json.loads(content)), record
        except Exception as exc:
            logger.warning("could not read translation verification result: %s", exc)
            failed = record.model_copy(
                update={"ok": False, "error": "invalid verification result"}
            )
            return None, failed
