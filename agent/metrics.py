"""Aggregate runs into the report acceptance criterion 12 asks for.

`tool_call_rate` is the headline. It is not a diagnostic — it is the single
statistic that separates a 98% system from a 43% one, because a model that skips
the glossary produces a fluent, plausible, wrong translation that nothing
downstream will flag.

See `specs/003-agent-client/spec.md` §6.
"""

from dataclasses import dataclass, field
from statistics import mean
from typing import Any

from contracts.agent import Initiator, RunResult, StopReason
from contracts.tools import Verdict


@dataclass
class RunRecord:
    """One evaluated run: what was asked, what was expected, what happened."""

    text: str
    result: RunResult
    expected_tool: str | None = None


@dataclass
class Report:
    runs: int = 0
    tool_call_rate: float = 0.0
    tool_selection_accuracy: float | None = None
    glossary_hit_rate: float | None = None
    mean_turns: float = 0.0
    retranslation_rate: float = 0.0
    max_turns_hit_rate: float = 0.0
    terms_total: int = 0
    terms_hit: int = 0
    tool_usage: dict[str, int] = field(default_factory=dict)
    routing_errors: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "runs": self.runs,
            "tool_call_rate": self.tool_call_rate,
            "tool_selection_accuracy": self.tool_selection_accuracy,
            "glossary_hit_rate": self.glossary_hit_rate,
            "mean_turns": self.mean_turns,
            "retranslation_rate": self.retranslation_rate,
            "max_turns_hit_rate": self.max_turns_hit_rate,
            "terms_total": self.terms_total,
            "terms_hit": self.terms_hit,
            "tool_usage": self.tool_usage,
            "routing_errors": self.routing_errors,
        }


def first_model_tool(result: RunResult) -> str | None:
    """The first tool the *model* chose. Policy-initiated calls do not count."""
    for record in result.tool_calls:
        if record.initiator is Initiator.MODEL:
            return record.name
    return None


def summarize(records: list[RunRecord]) -> Report:
    if not records:
        return Report()

    report = Report(runs=len(records))
    routed_total = 0
    routed_ok = 0

    for record in records:
        result = record.result
        if result.metrics.called_any_tool:
            report.tool_call_rate += 1
        if result.metrics.retranslations:
            report.retranslation_rate += 1
        if result.metrics.stop_reason is StopReason.MAX_TURNS:
            report.max_turns_hit_rate += 1

        for name in result.metrics.tool_names:
            report.tool_usage[name] = report.tool_usage.get(name, 0) + 1

        if record.expected_tool:
            routed_total += 1
            chosen = first_model_tool(result)
            if chosen == record.expected_tool:
                routed_ok += 1
            else:
                report.routing_errors.append(
                    {"text": record.text, "expected": record.expected_tool, "got": chosen}
                )

        if result.verify is not None:
            report.terms_total += len(result.verify.results)
            report.terms_hit += sum(
                1 for v in result.verify.results if v.verdict is Verdict.HIT
            )

    report.tool_call_rate /= report.runs
    report.retranslation_rate /= report.runs
    report.max_turns_hit_rate /= report.runs
    report.mean_turns = mean(r.result.metrics.turns for r in records)
    if routed_total:
        report.tool_selection_accuracy = routed_ok / routed_total
    if report.terms_total:
        report.glossary_hit_rate = report.terms_hit / report.terms_total
    return report


def format_report(report: Report) -> str:
    """Human-readable summary for the CLI and the eval harness."""

    def pct(value: float | None) -> str:
        return "n/a" if value is None else f"{value * 100:.1f}%"

    lines = [
        f"runs                     {report.runs}",
        f"tool call rate           {pct(report.tool_call_rate)}",
        f"tool selection accuracy  {pct(report.tool_selection_accuracy)}",
        f"glossary hit rate        {pct(report.glossary_hit_rate)}"
        + (f"  ({report.terms_hit}/{report.terms_total} terms)" if report.terms_total else ""),
        f"mean turns               {report.mean_turns:.2f}",
        f"retranslation rate       {pct(report.retranslation_rate)}",
        f"max-turns hit rate       {pct(report.max_turns_hit_rate)}",
    ]
    if report.tool_usage:
        usage = ", ".join(f"{name}={count}" for name, count in sorted(report.tool_usage.items()))
        lines.append(f"tool usage               {usage}")
    if report.routing_errors:
        lines.append("routing errors:")
        lines.extend(
            f"  - {e['text']!r} expected {e['expected']}, got {e['got']}"
            for e in report.routing_errors
        )
    return "\n".join(lines)
