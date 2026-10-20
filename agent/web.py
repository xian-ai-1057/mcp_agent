"""FastAPI adapter and local HTML test bench for the MCP agent.

The browser and external callers talk to this module, never directly to the
model gateway. Gateway credentials stay server-side, and one long-lived MCP
tool pool is owned by the FastAPI lifespan.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import ipaddress
import logging
import math
import os
import re
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from typing import Any, AsyncIterator, Callable
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agent.config import load_env_file
from agent.gateway import Gateway, GatewayError, HTTPGateway
from agent.loop import AgentLoop
from agent.mcp_client import MCPToolPool
from agent.mcp_config import (
    MCPConfigError,
    default_mcp_server_configs,
    load_mcp_server_configs,
)
from agent.prompts import SYSTEM_PROMPT
from agent.testing import RuleBasedGateway
from agent.tooling import ToolInvocationError, ToolRunner
from capabilities.translation.policy import TranslationSelfCheck
from capabilities.translation.prompts import TRANSLATION_RULES
from contracts.api import (
    AgentProfile,
    AgentRunRequest,
    AgentRunResponse,
    APIErrorResponse,
    CapabilitiesResponse,
    GatewayMode,
    HealthResponse,
)

logger = logging.getLogger(__name__)

API_VERSION = "v1"
APP_VERSION = "0.4.0"
DEFAULT_RUN_TIMEOUT_SECONDS = 120.0
DEFAULT_QUEUE_TIMEOUT_SECONDS = 2.0
MAX_BODY_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class WebSettings:
    """Process-level settings supplied by the CLI or an ASGI import."""

    mcp_config: Path | None = None
    verbose: bool = False
    cors_origins: tuple[str, ...] = ()
    trusted_hosts: tuple[str, ...] = ("127.0.0.1", "localhost", "[::1]", "testserver")


class AgentAPIError(Exception):
    """A safe, structured error that may be returned to an API caller."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


GatewayFactory = Callable[[GatewayMode], Gateway]


class RequestBodyLimitMiddleware:
    """Buffer at most ``max_bytes`` so chunked bodies cannot bypass the limit."""

    def __init__(self, app: ASGIApp, max_bytes: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {name.lower(): value for name, value in scope.get("headers", [])}
        raw_length = headers.get(b"content-length")
        if raw_length is not None:
            try:
                declared_length = int(raw_length)
            except ValueError:
                await _error_response(
                    400, "invalid_content_length", "Content-Length is invalid"
                )(scope, receive, send)
                return
            if declared_length < 0:
                await _error_response(
                    400, "invalid_content_length", "Content-Length is invalid"
                )(scope, receive, send)
                return
            if declared_length > self.max_bytes:
                await _error_response(413, "body_too_large", "Request body is too large")(
                    scope, receive, send
                )
                return

        buffered: list[Message] = []
        received_bytes = 0
        while True:
            message = await receive()
            buffered.append(message)
            if message["type"] == "http.disconnect":
                break
            if message["type"] != "http.request":
                continue
            received_bytes += len(message.get("body", b""))
            if received_bytes > self.max_bytes:
                await _error_response(413, "body_too_large", "Request body is too large")(
                    scope, receive, send
                )
                return
            if not message.get("more_body", False):
                break

        index = 0

        async def replay_receive() -> Message:
            nonlocal index
            if index < len(buffered):
                message = buffered[index]
                index += 1
                return message
            return {"type": "http.request", "body": b"", "more_body": False}

        await self.app(scope, replay_receive, send)


class AgentService:
    """Build and execute isolated AgentLoop runs over a shared tool pool."""

    def __init__(
        self,
        tools: ToolRunner,
        *,
        run_timeout_seconds: float = DEFAULT_RUN_TIMEOUT_SECONDS,
        queue_timeout_seconds: float = DEFAULT_QUEUE_TIMEOUT_SECONDS,
        gateway_factory: GatewayFactory | None = None,
    ) -> None:
        if not math.isfinite(run_timeout_seconds) or run_timeout_seconds <= 0:
            raise ValueError("run_timeout_seconds must be positive")
        if not math.isfinite(queue_timeout_seconds) or queue_timeout_seconds <= 0:
            raise ValueError("queue_timeout_seconds must be positive")
        self.tools = tools
        self.run_timeout_seconds = run_timeout_seconds
        self.queue_timeout_seconds = queue_timeout_seconds
        self._gateway_factory = gateway_factory or self._default_gateway
        # The MCP pool's concurrent-call contract is intentionally narrow.
        # Serialising local runs also keeps tool traces deterministic for tests.
        self._run_lock = asyncio.Lock()

    def capabilities(self) -> CapabilitiesResponse:
        return CapabilitiesResponse(
            http_gateway_configured=HTTPGateway.configured(),
            gateways=list(GatewayMode),
            profiles=list(AgentProfile),
            tools=sorted(self.tools.tool_names),
        )

    async def run(self, request: AgentRunRequest) -> AgentRunResponse:
        overrides: dict[str, Any] = {"max_turns": request.max_turns}
        if request.profile is AgentProfile.TRANSLATION:
            overrides["system_prompt"] = f"{SYSTEM_PROMPT}\n\n{TRANSLATION_RULES}"
            try:
                max_retranslate = int(os.environ.get("AGENT_MAX_RETRANSLATE", "2"))
            except ValueError as exc:
                raise AgentAPIError(
                    500,
                    "configuration_error",
                    "The translation profile is not configured correctly",
                ) from exc
            try:
                overrides["self_check"] = TranslationSelfCheck(
                    max_retranslate=max_retranslate
                )
            except ValueError as exc:
                raise AgentAPIError(
                    500,
                    "configuration_error",
                    "The translation profile is not configured correctly",
                ) from exc

        run_id = uuid4()
        created_at = datetime.now(timezone.utc)
        started = time.perf_counter()
        gateway: Gateway | None = None
        acquired = False
        try:
            try:
                await asyncio.wait_for(
                    self._run_lock.acquire(), timeout=self.queue_timeout_seconds
                )
                acquired = True
            except TimeoutError as exc:
                raise AgentAPIError(
                    429,
                    "agent_busy",
                    "The agent is busy; retry this request shortly",
                ) from exc

            gateway = self._gateway_factory(request.gateway)
            loop = AgentLoop.from_env(gateway, self.tools, **overrides)
            async with asyncio.timeout(self.run_timeout_seconds):
                result = await loop.run(request.text)
        except TimeoutError as exc:
            raise AgentAPIError(
                504,
                "run_timeout",
                "The agent did not finish before the configured timeout",
            ) from exc
        except GatewayError as exc:
            logger.warning("model gateway failed during API run: %s", exc)
            raise AgentAPIError(
                502,
                "gateway_error",
                "The configured model gateway could not complete the request",
            ) from exc
        except ToolInvocationError as exc:
            logger.warning("MCP pool failed during API run: %s", exc)
            raise AgentAPIError(
                502,
                "mcp_error",
                "An MCP tool service could not complete the request",
            ) from exc
        finally:
            if gateway is not None:
                aclose = getattr(gateway, "aclose", None)
                if aclose is not None:
                    try:
                        await aclose()
                    except Exception as exc:
                        logger.warning("could not close model gateway cleanly: %s", exc)
            if acquired:
                self._run_lock.release()

        duration_ms = max(0, round((time.perf_counter() - started) * 1000))
        return AgentRunResponse(
            run_id=run_id,
            created_at=created_at,
            duration_ms=duration_ms,
            result=result,
        )

    @staticmethod
    def _default_gateway(choice: GatewayMode) -> Gateway:
        if choice is GatewayMode.FAKE:
            return RuleBasedGateway()
        if not HTTPGateway.present():
            raise AgentAPIError(
                503,
                "gateway_not_configured",
                "HTTP gateway is unavailable; set GATEWAY_BASE_URL on the server",
            )
        try:
            return HTTPGateway.from_env()
        except (GatewayError, ValueError) as exc:
            raise AgentAPIError(
                500,
                "configuration_error",
                "The HTTP gateway is not configured correctly",
            ) from exc


def _positive_float_from_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise MCPConfigError(f"{name} must be a positive number") from exc
    if not math.isfinite(value) or value <= 0:
        raise MCPConfigError(f"{name} must be a positive number")
    return value


def _origins_from_env() -> tuple[str, ...]:
    raw = os.environ.get("AGENT_CORS_ORIGINS", "")
    return tuple(origin.strip() for origin in raw.split(",") if origin.strip())


def _trusted_hosts_from_env() -> tuple[str, ...]:
    raw = os.environ.get("AGENT_TRUSTED_HOSTS", "")
    configured = tuple(host.strip() for host in raw.split(",") if host.strip())
    return configured or ("127.0.0.1", "localhost", "[::1]", "testserver")


def create_app(
    settings: WebSettings | None = None,
    *,
    service: AgentService | None = None,
) -> FastAPI:
    """Create the ASGI application.

    ``service`` is an explicit test/integration seam. Production callers leave
    it unset so the lifespan owns exactly one MCPToolPool.
    """

    config = settings or WebSettings(
        cors_origins=_origins_from_env(),
        trusted_hosts=_trusted_hosts_from_env(),
    )
    # Each page carries its own inline style/script, so each needs its own CSP
    # hashes. `_inline_content_hash` matches the *first* inline block of a tag,
    # which is why a page must never contain a second <style> or <script>.
    def read_page(name: str) -> str:
        return files("agent").joinpath("static", name).read_text(encoding="utf-8")

    index_html = read_page("index.html")
    flow_html = read_page("flow.html")
    pages = {
        "/": (index_html, _csp_for(index_html)),
        "/flow": (flow_html, _csp_for(flow_html)),
    }

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        if service is not None:
            application.state.agent_service = service
            yield
            return

        server_configs = (
            load_mcp_server_configs(config.mcp_config)
            if config.mcp_config is not None
            else default_mcp_server_configs()
        )
        server_log = sys.stderr if config.verbose else open(os.devnull, "w")
        try:
            async with MCPToolPool(server_configs, errlog=server_log) as tools:
                application.state.agent_service = AgentService(
                    tools,
                    run_timeout_seconds=_positive_float_from_env(
                        "AGENT_RUN_TIMEOUT", DEFAULT_RUN_TIMEOUT_SECONDS
                    ),
                    queue_timeout_seconds=_positive_float_from_env(
                        "AGENT_QUEUE_TIMEOUT", DEFAULT_QUEUE_TIMEOUT_SECONDS
                    ),
                )
                yield
        finally:
            if server_log is not sys.stderr:
                server_log.close()

    application = FastAPI(
        title="MCP Agent API",
        summary="Run a configurable tool-calling agent over isolated MCP servers.",
        description=(
            "Each request is an independent agent run. Use `gateway=fake` for a "
            "deterministic wiring test, or `gateway=http` for the server-configured "
            "OpenAI-compatible model gateway."
        ),
        version=APP_VERSION,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    if config.cors_origins:
        if "*" in config.cors_origins:
            raise ValueError("AGENT_CORS_ORIGINS must list explicit origins, not '*'")
        application.add_middleware(
            CORSMiddleware,
            allow_origins=list(config.cors_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type"],
            expose_headers=["X-Request-ID"],
        )
    application.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=list(config.trusted_hosts),
    )
    application.add_middleware(RequestBodyLimitMiddleware, max_bytes=MAX_BODY_BYTES)

    @application.middleware("http")
    async def request_boundaries(request: Request, call_next: Callable[..., Any]) -> Response:
        if request.method == "POST" and request.url.path == f"/api/{API_VERSION}/runs":
            content_type = request.headers.get("content-type", "").split(";", 1)[0].strip()
            if content_type.lower() != "application/json":
                response = _error_response(
                    415,
                    "unsupported_media_type",
                    "Content-Type must be application/json",
                )
            else:
                response = await call_next(request)
        else:
            response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Request-ID"] = uuid4().hex
        page = pages.get(request.url.path)
        if page is not None or request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        if page is not None:
            response.headers["Content-Security-Policy"] = page[1]
        return response

    @application.exception_handler(AgentAPIError)
    async def agent_api_error_handler(_request: Request, exc: AgentAPIError) -> JSONResponse:
        return _error_response(exc.status_code, exc.code, exc.message)

    @application.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> Response:
        malformed_json = any(error.get("type") == "json_invalid" for error in exc.errors())
        if malformed_json:
            return _error_response(400, "invalid_json", "Request body is not valid JSON")
        return _error_response(422, "validation_error", "Request validation failed")

    @application.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException) -> Response:
        if request.url.path == f"/api/{API_VERSION}/runs" and exc.status_code == 400:
            return _error_response(400, "invalid_json", "Request body is not valid JSON")
        return await http_exception_handler(request, exc)

    @application.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception) -> Response:
        logger.error(
            "unexpected FastAPI request failure",
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        if request.url.path.startswith("/api/") or request.url.path == "/healthz":
            return _error_response(
                500,
                "internal_error",
                "The request failed unexpectedly; check the server log",
            )
        return _error_response(500, "internal_error", "The request failed unexpectedly")

    @application.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index() -> HTMLResponse:
        return HTMLResponse(index_html)

    @application.get("/flow", response_class=HTMLResponse, include_in_schema=False)
    async def flow() -> HTMLResponse:
        return HTMLResponse(flow_html)

    @application.get(
        "/healthz",
        response_model=HealthResponse,
        responses={503: {"model": APIErrorResponse, "description": "Agent is not ready"}},
        tags=["operations"],
        summary="Readiness check",
    )
    async def health(request: Request) -> HealthResponse:
        _service_from(request)
        return HealthResponse(version=APP_VERSION)

    @application.get(
        f"/api/{API_VERSION}/capabilities",
        response_model=CapabilitiesResponse,
        tags=["agent"],
        summary="List the current agent capabilities",
    )
    async def capabilities(request: Request) -> CapabilitiesResponse:
        return _service_from(request).capabilities()

    error_responses = {
        400: {"model": APIErrorResponse, "description": "Malformed JSON"},
        415: {"model": APIErrorResponse, "description": "Content-Type is not JSON"},
        422: {"model": APIErrorResponse, "description": "Request validation failed"},
        429: {"model": APIErrorResponse, "description": "Agent execution capacity is busy"},
        413: {"model": APIErrorResponse, "description": "Request body is too large"},
        500: {"model": APIErrorResponse, "description": "Server configuration error"},
        502: {"model": APIErrorResponse, "description": "Gateway or MCP service failure"},
        503: {"model": APIErrorResponse, "description": "Requested gateway is unavailable"},
        504: {"model": APIErrorResponse, "description": "Agent run timed out"},
    }

    @application.post(
        f"/api/{API_VERSION}/runs",
        response_model=AgentRunResponse,
        responses=error_responses,
        tags=["agent"],
        summary="Execute one isolated agent run",
    )
    async def run_agent(payload: AgentRunRequest, request: Request) -> AgentRunResponse:
        response = await _service_from(request).run(payload)
        return response

    return application


def _service_from(request: Request) -> AgentService:
    service = getattr(request.app.state, "agent_service", None)
    if not isinstance(service, AgentService):
        raise AgentAPIError(503, "not_ready", "The agent service is not ready")
    return service


def _error_response(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message}},
    )


def _csp_for(html: str) -> str:
    """Build the strict CSP for one inline-only page."""
    script_hash = _inline_content_hash(html, "script")
    style_hash = _inline_content_hash(html, "style")
    return (
        f"default-src 'self'; script-src 'self' '{script_hash}'; "
        f"style-src 'self' '{style_hash}'; connect-src 'self'; "
        "img-src 'self' data:; object-src 'none'; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'"
    )


def _inline_content_hash(html: str, tag: str) -> str:
    match = re.search(rf"<{tag}>(.*?)</{tag}>", html, re.DOTALL)
    if match is None:
        raise ValueError(f"test bench HTML has no inline {tag} block")
    digest = hashlib.sha256(match.group(1).encode("utf-8")).digest()
    return "sha256-" + base64.b64encode(digest).decode("ascii")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-agent-web",
        description="FastAPI service and local HTML test bench for the MCP agent.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: localhost).")
    parser.add_argument("--port", type=int, default=8000, help="Bind port (default: 8000).")
    parser.add_argument("--mcp-config", type=Path, default=None, help="Optional MCP JSON config.")
    parser.add_argument("--env-file", type=Path, default=None, help="Optional .env path.")
    parser.add_argument(
        "--cors-origin",
        action="append",
        default=[],
        help="Allowed browser origin; repeat for multiple origins.",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Show access and MCP logs.")
    return parser


def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.port <= 65_535:
        print("error: port must be between 1 and 65535", file=sys.stderr)
        return 2
    if not _is_loopback_host(args.host):
        print(
            "error: this unauthenticated test service may only bind to a loopback address",
            file=sys.stderr,
        )
        return 2

    # Web is an entry point, so loading .env here preserves the repository rule
    # that importing modules never mutates os.environ.
    load_env_file(args.env_file)
    origins = tuple(args.cors_origin) or _origins_from_env()
    try:
        application = create_app(
            WebSettings(
                mcp_config=args.mcp_config,
                verbose=args.verbose,
                cors_origins=origins,
                trusted_hosts=_trusted_hosts_from_env(),
            )
        )
    except ValueError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    uvicorn.run(
        application,
        host=args.host,
        port=args.port,
        log_level="info" if args.verbose else "warning",
        access_log=args.verbose,
    )
    return 0


# ASGI import target. For direct Uvicorn imports, export settings or pass
# `--env-file .env`. The recommended `python -m agent.web` path loads .env
# itself, and importing this module remains side-effect free.
app = create_app(
    WebSettings(
        cors_origins=_origins_from_env(),
        trusted_hosts=_trusted_hosts_from_env(),
    )
)


if __name__ == "__main__":
    sys.exit(main())
