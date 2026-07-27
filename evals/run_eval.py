"""Run an evaluation suite end to end and print the report.

    python -m evals.run_eval --suite all
    python -m evals.run_eval --suite routing --gateway fake
    python -m evals.run_eval --suite glossary --out evals/reports/glossary.json

Suites map onto the plan's acceptance criteria:

| Suite         | Criteria | Question |
|---------------|----------|----------|
| `routing`     | 5, 11    | Does the model pick the right tool? |
| `translation` | 8, 12    | Does the self-check loop raise the hit rate? |
| `glossary`    | 10       | One request per glossary term — what is the end-to-end hit rate? |

`--gateway fake` exercises the harness itself. It does not evaluate a model, and
the report says so in `gateway`.
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.gateway import Gateway, GatewayError, HTTPGateway
from agent.loop import AgentLoop
from agent.mcp_client import MCPToolClient
from agent.metrics import Report, RunRecord, format_report, summarize
from agent.testing import RuleBasedGateway
from glossary.runtime import get_glossary

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "fixtures"
REPORTS = REPO_ROOT / "evals" / "reports"

SUITES = ("routing", "translation", "glossary")


def _load(name: str) -> list[dict[str, Any]]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))["cases"]


def routing_cases() -> list[dict[str, Any]]:
    return [
        {"text": case["text"], "expected_tool": case["expect_tool"]}
        for case in _load("routing_cases.json")
    ]


def translation_cases() -> list[dict[str, Any]]:
    return [
        {"text": case["text"], "expected_tool": "lookup_terms"}
        for case in _load("retranslate_cases.json")
    ]


def glossary_cases() -> list[dict[str, Any]]:
    """One translation request per glossary term.

    This is the plan's criterion-10 suite. It reads the glossary rather than a
    fixture file, so pointing `GLOSSARY_CSV` at the production 379-term asset
    scales the suite with no code change.
    """
    return [
        {"text": f"請翻譯：{entry.zh}", "expected_tool": "lookup_terms"}
        for entry in get_glossary().entries
    ]


CASE_BUILDERS = {
    "routing": routing_cases,
    "translation": translation_cases,
    "glossary": glossary_cases,
}


async def run_suite(loop: AgentLoop, cases: list[dict[str, Any]]) -> Report:
    records: list[RunRecord] = []
    for index, case in enumerate(cases, start=1):
        result = await loop.run(case["text"])
        records.append(
            RunRecord(text=case["text"], result=result, expected_tool=case.get("expected_tool"))
        )
        print(f"  [{index}/{len(cases)}] {case['text'][:40]}", file=sys.stderr)
    return summarize(records)


def _make_gateway(choice: str) -> Gateway:
    if choice == "fake":
        return RuleBasedGateway()
    if not HTTPGateway.configured():
        raise GatewayError(
            "GATEWAY_BASE_URL is not set — no model to evaluate. "
            "Configure it (see .env.example), or pass --gateway fake to exercise the harness itself."
        )
    return HTTPGateway.from_env()


async def amain(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run_eval")
    parser.add_argument("--suite", choices=(*SUITES, "all"), default="all")
    parser.add_argument("--gateway", choices=("http", "fake"), default="http")
    parser.add_argument("--out", type=Path, default=None, help="Write the JSON report here.")
    parser.add_argument("--limit", type=int, default=None, help="Cap cases per suite.")
    args = parser.parse_args(argv)

    try:
        gateway = _make_gateway(args.gateway)
    except GatewayError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    suites = SUITES if args.suite == "all" else (args.suite,)
    model = os.environ.get("GATEWAY_MODEL", "-") if args.gateway == "http" else "rule-based-double"
    payload: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "gateway": args.gateway,
        "model": model,
        "glossary_terms": len(get_glossary()),
        "suites": {},
    }

    server_log = open(os.devnull, "w")
    try:
        async with MCPToolClient(errlog=server_log) as tools:
            loop = AgentLoop.from_env(gateway, tools)
            payload["tools"] = sorted(tools.tool_names)

            for name in suites:
                cases = CASE_BUILDERS[name]()
                if args.limit:
                    cases = cases[: args.limit]
                print(f"\n== {name} ({len(cases)} cases) ==", file=sys.stderr)
                report = await run_suite(loop, cases)
                payload["suites"][name] = report.to_dict()
                print(f"\n== {name} ==")
                print(format_report(report))
    finally:
        aclose = getattr(gateway, "aclose", None)
        if aclose is not None:
            await aclose()
        server_log.close()

    destination = args.out or REPORTS / f"eval-{args.suite}-{args.gateway}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nreport written to {destination}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(amain(argv))


if __name__ == "__main__":
    sys.exit(main())
