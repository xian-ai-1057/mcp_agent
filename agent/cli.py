"""Command-line entry point.

    mcp-agent "請幫我翻譯：客戶申請提高臨時額度"
    mcp-agent --interactive
    mcp-agent --json --gateway fake "現在幾點"

`--gateway fake` runs the rule-based double end to end through the real MCP
server and the real tools, which is what makes the demo runnable with no gateway
credentials. It proves the plumbing, not the model.
"""

import argparse
import asyncio
import json
import logging
import os
import sys

from agent.gateway import Gateway, GatewayError, HTTPGateway
from agent.loop import AgentLoop
from agent.mcp_client import MCPToolClient
from agent.testing import RuleBasedGateway
from contracts.agent import RunResult


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-agent",
        description="General-purpose tool-calling agent with a glossary-backed translation MCP server.",
    )
    parser.add_argument("text", nargs="?", help="The request. Omit with --interactive.")
    parser.add_argument(
        "--gateway",
        choices=("http", "fake"),
        default="http",
        help="'http' uses GATEWAY_BASE_URL; 'fake' uses the deterministic double (no credentials).",
    )
    parser.add_argument("--interactive", "-i", action="store_true", help="Read requests from stdin.")
    parser.add_argument("--json", action="store_true", help="Emit the full RunResult as JSON.")
    parser.add_argument("--max-turns", type=int, default=None, help="Override the turn budget.")
    parser.add_argument("--no-self-check", action="store_true", help="Disable re-translation.")
    parser.add_argument("--verbose", "-v", action="store_true", help="Log tool calls to stderr.")
    return parser


def _make_gateway(choice: str) -> Gateway:
    if choice == "fake":
        return RuleBasedGateway()
    if not HTTPGateway.configured():
        raise GatewayError(
            "GATEWAY_BASE_URL is not set. Configure it (see .env.example), or run with --gateway fake."
        )
    return HTTPGateway.from_env()


def render(result: RunResult, as_json: bool) -> str:
    if as_json:
        return json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2)

    lines = [result.output]
    metrics = result.metrics
    summary = (
        f"[turns={metrics.turns} tool_calls={metrics.tool_calls} "
        f"tools={','.join(metrics.tool_names) or '-'} "
        f"retranslations={metrics.retranslations} stop={metrics.stop_reason.value}]"
    )
    if not metrics.called_any_tool:
        summary += " [no tool was called]"
    lines.append(summary)

    if result.verify is not None:
        lines.append(f"[glossary hit rate {result.verify.hit_rate * 100:.0f}%]")
        for verdict in result.verify.results:
            detail = f" (found: {verdict.found})" if verdict.found else ""
            lines.append(f"  {verdict.verdict.value:<5} {verdict.zh} → {verdict.expected_en}{detail}")
    return "\n".join(lines)


async def run_once(loop: AgentLoop, text: str, as_json: bool) -> None:
    result = await loop.run(text)
    print(render(result, as_json))


async def run_interactive(loop: AgentLoop, as_json: bool) -> None:
    print("Type a request, or Ctrl-D to quit.", file=sys.stderr)
    for line in sys.stdin:
        text = line.strip()
        if not text:
            continue
        await run_once(loop, text, as_json)


async def amain(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.text and not args.interactive:
        build_parser().error("give a request, or pass --interactive")

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        gateway = _make_gateway(args.gateway)
    except GatewayError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    server_log = sys.stderr if args.verbose else open(os.devnull, "w")
    try:
        async with MCPToolClient(errlog=server_log) as tools:
            overrides = {}
            if args.max_turns is not None:
                overrides["max_turns"] = args.max_turns
            if args.no_self_check:
                overrides["self_check"] = None
            loop = AgentLoop.from_env(gateway, tools, **overrides)

            if args.interactive:
                await run_interactive(loop, args.json)
            else:
                await run_once(loop, args.text, args.json)
    except GatewayError as exc:
        print(f"gateway error: {exc}", file=sys.stderr)
        return 1
    finally:
        aclose = getattr(gateway, "aclose", None)
        if aclose is not None:
            await aclose()
        if server_log is not sys.stderr:
            server_log.close()

    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(amain(argv))


if __name__ == "__main__":
    sys.exit(main())
