"""Command-line entry point.

    mcp-agent "請幫我翻譯：客戶申請提高臨時額度"
    mcp-agent --interactive
    mcp-agent --json --gateway fake "現在幾點"

`--gateway fake` runs the rule-based double end to end through the real MCP
server and the real tools, which is what makes the demo runnable with no gateway
credentials. It proves the plumbing, not the model.

Configuration comes from `.env` (loaded here, at the entry point) or from
exported variables, which win over the file.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from agent.config import describe_env_source, load_env_file
from agent.gateway import Gateway, GatewayError, HTTPGateway
from agent.loop import AgentLoop
from agent.mcp_client import MCPToolPool, ToolInvocationError
from agent.mcp_config import (
    MCPConfigError,
    default_mcp_server_configs,
    load_mcp_server_configs,
)
from agent.prompts import SYSTEM_PROMPT
from agent.testing import RuleBasedGateway
from capabilities.translation.policy import TranslationSelfCheck
from capabilities.translation.prompts import TRANSLATION_RULES
from contracts.agent import RunResult


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-agent",
        description="Configurable general-purpose agent backed by isolated MCP servers.",
    )
    parser.add_argument("text", nargs="?", help="The request. Omit with --interactive.")
    parser.add_argument(
        "--gateway",
        choices=("http", "fake"),
        default="http",
        help=(
            "'http' uses GATEWAY_BASE_URL; 'fake' uses the deterministic double "
            "(no credentials)."
        ),
    )
    parser.add_argument(
        "--interactive", "-i", action="store_true", help="Read requests from stdin."
    )
    parser.add_argument("--json", action="store_true", help="Emit the full RunResult as JSON.")
    parser.add_argument("--max-turns", type=int, default=None, help="Override the turn budget.")
    parser.add_argument(
        "--profile",
        choices=("generic", "translation"),
        default=None,
        help="Enable capability-specific prompt/policies (default: AGENT_PROFILE or generic).",
    )
    parser.add_argument(
        "--no-self-check",
        action="store_true",
        help="Disable the translation profile's terminology self-check.",
    )
    parser.add_argument(
        "--mcp-config",
        type=Path,
        default=None,
        help=(
            "JSON file describing stdio MCP servers; defaults to split utility "
            "+ translation servers."
        ),
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Log tool calls to stderr.")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="Env file to load. Defaults to .env in the repository root.",
    )
    return parser


def _make_gateway(choice: str, env_source: Path | None) -> Gateway:
    if choice == "fake":
        return RuleBasedGateway()
    if not HTTPGateway.configured():
        raise GatewayError(
            "GATEWAY_BASE_URL is not set —— "
            f"{describe_env_source(env_source)}。\n"
            "  設定方式：把 GATEWAY_BASE_URL 寫進 .env（見 .env.example），"
            "或直接 export，或改用 --gateway fake（不需要憑證）。"
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
            lines.append(
                f"  {verdict.verdict.value:<5} {verdict.zh} → "
                f"{verdict.expected_en}{detail}"
            )
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

    # Before gateway/profile/server config reads os.environ. MCP child processes
    # receive only the variables each MCPServerConfig explicitly allowlists.
    env_source = load_env_file(args.env_file)

    profile = args.profile or os.environ.get("AGENT_PROFILE", "generic").strip().lower()
    if profile not in {"generic", "translation"}:
        print("error: AGENT_PROFILE must be 'generic' or 'translation'", file=sys.stderr)
        return 2

    try:
        server_configs = (
            load_mcp_server_configs(args.mcp_config)
            if args.mcp_config is not None
            else default_mcp_server_configs()
        )
    except MCPConfigError as exc:
        print(f"MCP config error: {exc}", file=sys.stderr)
        return 2

    try:
        gateway = _make_gateway(args.gateway, env_source)
    except (GatewayError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    server_log = sys.stderr if args.verbose else open(os.devnull, "w")
    try:
        async with MCPToolPool(server_configs, errlog=server_log) as tools:
            overrides = {}
            if args.max_turns is not None:
                overrides["max_turns"] = args.max_turns
            if profile == "translation":
                overrides["system_prompt"] = f"{SYSTEM_PROMPT}\n\n{TRANSLATION_RULES}"
                if not args.no_self_check:
                    overrides["self_check"] = TranslationSelfCheck(
                        max_retranslate=int(os.environ.get("AGENT_MAX_RETRANSLATE", "2"))
                    )
            loop = AgentLoop.from_env(gateway, tools, **overrides)

            if args.interactive:
                await run_interactive(loop, args.json)
            else:
                await run_once(loop, args.text, args.json)
    except GatewayError as exc:
        print(f"gateway error: {exc}", file=sys.stderr)
        return 1
    except (MCPConfigError, ToolInvocationError) as exc:
        print(f"MCP error: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
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
