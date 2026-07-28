"""Deterministic gateway doubles.

Acceptance criteria have to be verifiable in CI, where there is no gateway. These
two classes make loop behaviour testable end to end — through the real MCP
server and the real tools — with no model in the picture.

What they cannot do is answer "does the real model pick the right tool". That
question belongs to `evals/`, which requires a configured gateway and is skipped,
never faked, when one is absent. Keeping the two apart is the point: a simulated
routing score would be a number that looks like evidence and isn't.

See `specs/003-agent-client/spec.md` §7.
"""

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from agent.gateway import AssistantTurn, ToolCall

RETRANSLATE_MARKER = "請修正上述術語"
_BLOCK_LINE = re.compile(r"^-\s*(?P<zh>.+?)\s*→\s*(?P<en>.+?)\s*$")
_CORRECTION_LINE = re.compile(r"^-\s*(?P<zh>.+?)\s*必須譯為\s*(?P<en>[^（]+)")
_PREVIOUS_TRANSLATION = re.compile(r"你的譯文：\n(?P<text>.*?)\n\n", re.DOTALL)

TRANSLATE_CUES = ("翻譯", "翻成英文", "譯成英文", "譯為英文", "translate")
TIME_CUES = ("幾點", "日期", "時間", "幾月", "幾號", "today", "time")
WEATHER_CUES = ("天氣", "氣溫", "weather")
GREETING_CUES = ("打招呼", "問候", "說哈囉", "say hello", "greet")

CITY_TIMEZONES = {
    "台北": "Asia/Taipei",
    "臺北": "Asia/Taipei",
    "紐約": "America/New_York",
    "東京": "Asia/Tokyo",
    "倫敦": "Europe/London",
}

INTENT_TOOL = {
    "translate": "lookup_terms",
    "time": "get_time",
    "weather": "get_weather",
    "greet": "say_hello",
}


def classify(text: str) -> str:
    """Intent of a request.

    Translation is tested first on purpose: `請翻譯：服務時間` contains a time cue
    but is unambiguously a translation request, and getting that precedence wrong
    would make the double disagree with the fixtures for the wrong reason.
    """
    if any(cue in text for cue in TRANSLATE_CUES):
        return "translate"
    if any(cue in text for cue in WEATHER_CUES):
        return "weather"
    if any(cue in text for cue in GREETING_CUES):
        return "greet"
    if any(cue in text for cue in TIME_CUES):
        return "time"
    return "chat"


@dataclass
class ScriptedGateway:
    """Replays a fixed list of turns. For loop mechanics, not for behaviour."""

    turns: list[AssistantTurn]
    requests: list[dict[str, Any]] = field(default_factory=list)
    _index: int = 0

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
    ) -> AssistantTurn:
        self.requests.append({"messages": list(messages), "tools": tools})
        if self._index >= len(self.turns):
            # Running off the end means the loop asked for more turns than the
            # test scripted; answer with plain text so the loop terminates and
            # the assertion that failed is the interesting one.
            return AssistantTurn(content="(script exhausted)")
        turn = self.turns[self._index]
        self._index += 1
        return turn


@dataclass
class RuleBasedGateway:
    """Simulates a competent tool-using model.

    It routes on the request, calls the matching tool, and — for translation —
    builds its answer out of the English the tool handed back. It does not
    translate; it assembles a sentence containing the required terms, which is
    exactly what the matcher is checking for and nothing more.

    `glossary_fidelity` is the lever that makes failure reproducible: below 1.0
    the double deliberately drops terms, which is how the re-translation criteria
    get a real failure to repair. `repair_fidelity` of 0.0 models a model that
    never improves, which is how the retry cap gets tested.
    """

    glossary_fidelity: float = 1.0
    repair_fidelity: float = 1.0
    use_tools: bool = True
    requests: list[dict[str, Any]] = field(default_factory=list)

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
    ) -> AssistantTurn:
        self.requests.append({"messages": list(messages), "tools": tools})
        available = {
            tool.get("function", {}).get("name") for tool in (tools or []) if isinstance(tool, dict)
        }
        last = messages[-1]

        if last.get("role") == "tool":
            return self._answer_from_tool(messages)

        text = last.get("content") or ""
        if RETRANSLATE_MARKER in text:
            return AssistantTurn(content=self._repair(text))

        intent = classify(text)
        tool_name = INTENT_TOOL.get(intent)
        if self.use_tools and tool_name and tool_name in available:
            return AssistantTurn(
                tool_calls=(
                    ToolCall(
                        id=f"call_{intent}",
                        name=tool_name,
                        arguments=self._arguments(intent, text),
                    ),
                )
            )
        return AssistantTurn(content=self._without_tools(intent, text))

    # -- request side --------------------------------------------------------

    def _arguments(self, intent: str, text: str) -> dict[str, Any]:
        if intent == "translate":
            return {"text": text}
        if intent == "time":
            for city, zone in CITY_TIMEZONES.items():
                if city in text:
                    return {"timezone": zone}
            return {}
        if intent == "weather":
            return {"city": next((c for c in CITY_TIMEZONES if c in text), "台北")}
        if intent == "greet":
            return {"name": "客戶", "language": "zh"}
        return {}

    def _without_tools(self, intent: str, text: str) -> str:
        if intent == "translate":
            return "The customer request has been processed."
        return "我無法在沒有工具的情況下回答這個問題。"

    # -- answer side ---------------------------------------------------------

    def _answer_from_tool(self, messages: list[dict[str, Any]]) -> AssistantTurn:
        tool_message = messages[-1]
        name = tool_message.get("name") or ""
        payload = _load(tool_message.get("content"))
        source = next(
            (m.get("content", "") for m in messages if m.get("role") == "user"),
            "",
        )

        if name == "lookup_terms":
            return AssistantTurn(content=self._translate(source, payload))
        if name == "get_time":
            return AssistantTurn(
                content=f"現在是 {payload.get('date', '')} {payload.get('time', '')}"
                f"（{payload.get('timezone', '')}）。"
            )
        if name == "get_weather":
            return AssistantTurn(
                content=f"{payload.get('city', '')}目前 {payload.get('temperature_c', '')}°C，"
                f"{payload.get('condition', '')}。"
            )
        if name == "say_hello":
            return AssistantTurn(content=str(payload.get("greeting", "")))
        return AssistantTurn(content=json.dumps(payload, ensure_ascii=False))

    def _translate(self, source: str, payload: dict[str, Any]) -> str:
        terms = _terms_from_block(payload.get("glossary_block", ""))
        kept = _keep(terms, self.glossary_fidelity)
        if not kept:
            return "The customer request has been processed."
        return "Regarding the request: " + ", ".join(kept) + "."

    def _repair(self, prompt: str) -> str:
        required = [
            match.group("en").strip()
            for line in prompt.splitlines()
            if (match := _CORRECTION_LINE.match(line.strip()))
        ]
        previous = ""
        found = _PREVIOUS_TRANSLATION.search(prompt)
        if found:
            previous = found.group("text").strip()

        kept = _keep(required, self.repair_fidelity)
        if not kept:
            return previous or "The customer request has been processed."
        return f"{previous} Specifically: " + ", ".join(kept) + "."


def _load(content: Any) -> dict[str, Any]:
    if isinstance(content, dict):
        return content
    try:
        loaded = json.loads(content or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _terms_from_block(block: str) -> list[str]:
    terms: list[str] = []
    for line in block.splitlines():
        match = _BLOCK_LINE.match(line.strip())
        if match:
            terms.append(match.group("en").strip())
    return terms


def _keep(terms: Iterable[str], fidelity: float) -> list[str]:
    """Deterministically keep the leading `fidelity` share of `terms`."""
    ordered = list(terms)
    if fidelity >= 1.0:
        return ordered
    if fidelity <= 0.0:
        return []
    return ordered[: math.ceil(len(ordered) * fidelity)]
