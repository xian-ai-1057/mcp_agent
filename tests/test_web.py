"""FastAPI adapter tests: contracts, safety boundaries, and agent wiring."""

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import agent.web as web_module
from agent.gateway import AssistantTurn, GatewayError
from agent.mcp_client import LocalToolRunner
from agent.web import (
    MAX_BODY_BYTES,
    AgentAPIError,
    AgentService,
    WebSettings,
    create_app,
)
from contracts.api import AgentRunRequest
from tools.registry import discover


@pytest.fixture
def runner():
    return LocalToolRunner(discover())


@pytest.fixture
def service(runner):
    return AgentService(runner, run_timeout_seconds=1, queue_timeout_seconds=0.05)


@pytest.fixture
def client(service):
    application = create_app(WebSettings(), service=service)
    with TestClient(application, raise_server_exceptions=False) as test_client:
        yield test_client


class TestHTMLAndOperations:
    def test_home_serves_the_packaged_test_bench(self, client):
        response = client.get("/")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert "MCP Agent Lab" in response.text
        assert "/api/v1/runs" in response.text
        assert "default-src 'self'" in response.headers["content-security-policy"]
        assert "sha256-" in response.headers["content-security-policy"]
        assert "unsafe-inline" not in response.headers["content-security-policy"]
        assert "object-src 'none'" in response.headers["content-security-policy"]
        assert response.headers["cache-control"] == "no-store"

    def test_flow_page_is_served_with_its_own_csp(self, client):
        home = client.get("/")
        response = client.get("/flow")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert "MCP Agent 流程動畫" in response.text
        assert "/api/v1/runs" in response.text
        assert response.headers["cache-control"] == "no-store"

        policy = response.headers["content-security-policy"]
        assert "default-src 'self'" in policy
        assert "sha256-" in policy
        assert "unsafe-inline" not in policy
        assert "object-src 'none'" in policy
        # Each page hashes its own inline blocks. `_inline_content_hash` only
        # matches the first <style>/<script> in a document, so sharing one
        # policy across pages would silently break whichever page it was not
        # computed from.
        assert policy != home.headers["content-security-policy"]

    def test_each_page_has_exactly_one_inline_style_and_script(self):
        for name in ("index.html", "flow.html"):
            html = (Path(web_module.__file__).parent / "static" / name).read_text(encoding="utf-8")
            assert html.count("<style>") == 1, name
            assert html.count("<script>") == 1, name
            # An attribute on the tag would make the CSP regex miss the block.
            assert "<style " not in html, name
            assert "<script " not in html, name

    def test_swagger_and_openapi_are_available(self, client):
        docs = client.get("/docs")
        schema = client.get("/openapi.json")

        assert docs.status_code == 200
        assert "Swagger UI" in docs.text
        assert "content-security-policy" not in docs.headers
        assert schema.status_code == 200
        assert "/api/v1/runs" in schema.json()["paths"]
        assert schema.json()["info"]["title"] == "MCP Agent API"

    def test_health_and_capabilities(self, client):
        health = client.get("/healthz")
        capabilities = client.get("/api/v1/capabilities")

        assert health.json() == {"status": "ok", "version": "0.4.0"}
        assert capabilities.status_code == 200
        body = capabilities.json()
        assert body["http_gateway_configured"] is False
        assert set(body["profiles"]) == {"generic", "translation"}
        assert {"get_time", "get_weather", "say_hello"} <= set(body["tools"])

    def test_untrusted_host_is_rejected(self, client):
        response = client.get("/healthz", headers={"host": "attacker.example"})
        assert response.status_code == 400

    def test_explicit_cors_origin_is_allowed(self, service):
        application = create_app(
            WebSettings(cors_origins=("https://app.example.com",)),
            service=service,
        )
        with TestClient(application) as client:
            response = client.get(
                "/api/v1/capabilities",
                headers={"origin": "https://app.example.com"},
            )

        assert response.headers["access-control-allow-origin"] == "https://app.example.com"

    def test_wildcard_cors_origin_is_rejected(self, service):
        with pytest.raises(ValueError, match="explicit origins"):
            create_app(
                WebSettings(cors_origins=("*",)),
                service=service,
            )

    async def test_health_is_not_ready_when_lifespan_did_not_start(self):
        application = create_app(WebSettings())
        transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/healthz")

        assert response.status_code == 503
        assert response.json()["error"]["code"] == "not_ready"

    def test_production_lifespan_owns_one_tool_pool(self, monkeypatch):
        observed = {"entered": 0, "exited": 0}

        class FakePool:
            def __init__(self, configs, **kwargs):
                self.configs = configs
                self.tool_names = {"fake_tool"}
                self.openai_tools = []

            async def __aenter__(self):
                observed["entered"] += 1
                return self

            async def __aexit__(self, *exc_info):
                observed["exited"] += 1

            async def call(self, name, arguments):
                return "{}"

        monkeypatch.setattr(web_module, "MCPToolPool", FakePool)
        application = create_app(WebSettings())

        with TestClient(application) as client:
            assert client.get("/healthz").status_code == 200
            assert client.get("/api/v1/capabilities").json()["tools"] == ["fake_tool"]
            assert observed == {"entered": 1, "exited": 0}

        assert observed == {"entered": 1, "exited": 1}


class TestRunAPI:
    def test_fake_run_returns_a_versioned_envelope(self, client):
        response = client.post(
            "/api/v1/runs",
            json={
                "text": "現在台北幾點？",
                "gateway": "fake",
                "profile": "generic",
                "max_turns": 6,
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["run_id"]
        assert body["created_at"].endswith("Z")
        assert body["duration_ms"] >= 0
        assert body["result"]["metrics"]["tool_names"] == ["get_time"]
        assert body["result"]["metrics"]["stop_reason"] == "completed"
        assert "Asia/Taipei" in body["result"]["output"]
        assert response.headers["x-request-id"]

    def test_translation_profile_includes_terminology_verification(self, client):
        response = client.post(
            "/api/v1/runs",
            json={
                "text": "請幫我翻譯：客戶申請提高臨時額度",
                "gateway": "fake",
                "profile": "translation",
                "max_turns": 6,
            },
        )

        assert response.status_code == 200
        result = response.json()["result"]
        assert result["verify"]["hit_rate"] == 1.0
        assert result["tool_calls"][0]["name"] == "lookup_terms"

    @pytest.mark.parametrize(
        "payload",
        [
            {"text": "   "},
            {"text": 123},
            {"text": "hello", "max_turns": True},
            {"text": "hello", "max_turns": 21},
            {"text": "hello", "gateway": "unknown"},
            {"text": "hello", "profile": "unknown"},
            {"text": "hello", "allow_insecure_http": True},
            {"text": "hello", "unexpected": "field"},
        ],
    )
    def test_invalid_contract_is_a_stable_422(self, client, payload):
        response = client.post("/api/v1/runs", json=payload)
        assert response.status_code == 422
        assert response.json() == {
            "error": {"code": "validation_error", "message": "Request validation failed"}
        }

    def test_malformed_json_is_400(self, client):
        response = client.post(
            "/api/v1/runs",
            content=b"{not-json",
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_json"

    def test_non_utf8_json_is_the_same_stable_400(self, client):
        response = client.post(
            "/api/v1/runs",
            content=b"\xff",
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 400
        assert response.json() == {
            "error": {"code": "invalid_json", "message": "Request body is not valid JSON"}
        }

    def test_non_json_content_type_is_415(self, client):
        response = client.post(
            "/api/v1/runs",
            content="text=hello",
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        assert response.status_code == 415
        assert response.json()["error"]["code"] == "unsupported_media_type"

    def test_declared_oversized_body_is_413(self, client):
        response = client.post(
            "/api/v1/runs",
            content=b"{}",
            headers={
                "content-type": "application/json",
                "content-length": str(MAX_BODY_BYTES + 1),
            },
        )
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "body_too_large"

    def test_chunked_oversized_body_is_also_413(self, client):
        def chunks():
            yield b'{"text":"'
            yield b"x" * (MAX_BODY_BYTES + 1)
            yield b'"}'

        response = client.post(
            "/api/v1/runs",
            content=chunks(),
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "body_too_large"

    def test_http_gateway_must_be_configured(self, client, monkeypatch):
        monkeypatch.delenv("GATEWAY_BASE_URL", raising=False)
        response = client.post(
            "/api/v1/runs",
            json={"text": "hello", "gateway": "http"},
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "gateway_not_configured"

    def test_invalid_http_gateway_is_not_advertised(self, client, monkeypatch):
        monkeypatch.setenv("GATEWAY_BASE_URL", "http://gateway.example/v1")
        monkeypatch.delenv("GATEWAY_ALLOW_INSECURE_HTTP", raising=False)

        capabilities = client.get("/api/v1/capabilities")
        response = client.post(
            "/api/v1/runs",
            json={"text": "hello", "gateway": "http"},
        )

        assert capabilities.json()["http_gateway_configured"] is False
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "configuration_error"

    def test_insecure_http_gateway_opt_in_is_shared_by_the_web_api(
        self, client, monkeypatch
    ):
        monkeypatch.setenv("GATEWAY_BASE_URL", "http://gateway.example/v1")
        monkeypatch.setenv("GATEWAY_ALLOW_INSECURE_HTTP", "true")

        async def complete(gateway, messages, tools=None, tool_choice="auto"):
            return AssistantTurn(content="HTTP gateway reached")

        monkeypatch.setattr(web_module.HTTPGateway, "complete", complete)

        capabilities = client.get("/api/v1/capabilities")
        response = client.post(
            "/api/v1/runs",
            json={"text": "hello", "gateway": "http"},
        )

        assert capabilities.json()["http_gateway_configured"] is True
        assert response.status_code == 200
        assert response.json()["result"]["output"] == "HTTP gateway reached"


class BrokenGateway:
    async def complete(self, messages, tools=None, tool_choice="auto"):
        raise GatewayError("secret upstream response")


class CrashingGateway:
    async def complete(self, messages, tools=None, tool_choice="auto"):
        raise RuntimeError("private implementation detail")


class SlowGateway:
    def __init__(self, started=None, release=None):
        self.started = started
        self.release = release
        self.closed = False

    async def complete(self, messages, tools=None, tool_choice="auto"):
        if self.started is not None:
            self.started.set()
        if self.release is None:
            await asyncio.sleep(1)
        else:
            await self.release.wait()
        return AssistantTurn(content="done")

    async def aclose(self):
        self.closed = True


class TestFailureAndCapacityBoundaries:
    def test_gateway_errors_are_sanitised(self, runner):
        service = AgentService(runner, gateway_factory=lambda _choice: BrokenGateway())
        application = create_app(WebSettings(), service=service)

        with TestClient(application, raise_server_exceptions=False) as client:
            response = client.post("/api/v1/runs", json={"text": "hello"})

        assert response.status_code == 502
        assert response.json()["error"]["code"] == "gateway_error"
        assert "secret" not in response.text

    def test_unexpected_errors_use_a_sanitised_json_envelope(self, runner):
        service = AgentService(runner, gateway_factory=lambda _choice: CrashingGateway())
        application = create_app(WebSettings(), service=service)

        with TestClient(application, raise_server_exceptions=False) as client:
            response = client.post("/api/v1/runs", json={"text": "hello"})

        assert response.status_code == 500
        assert response.json()["error"]["code"] == "internal_error"
        assert "private implementation detail" not in response.text

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_timeouts_are_rejected(self, runner, value):
        with pytest.raises(ValueError, match="positive"):
            AgentService(runner, run_timeout_seconds=value)
        with pytest.raises(ValueError, match="positive"):
            AgentService(runner, queue_timeout_seconds=value)

    async def test_timeout_closes_the_gateway(self, runner):
        gateway = SlowGateway()
        service = AgentService(
            runner,
            run_timeout_seconds=0.01,
            gateway_factory=lambda _choice: gateway,
        )

        with pytest.raises(AgentAPIError) as caught:
            await service.run(AgentRunRequest(text="hello"))

        assert caught.value.status_code == 504
        assert caught.value.code == "run_timeout"
        assert gateway.closed is True

    async def test_waiting_runs_are_bounded(self, runner):
        started = asyncio.Event()
        release = asyncio.Event()
        service = AgentService(
            runner,
            run_timeout_seconds=1,
            queue_timeout_seconds=0.01,
            gateway_factory=lambda _choice: SlowGateway(started, release),
        )
        request = AgentRunRequest(text="hello")
        first = asyncio.create_task(service.run(request))
        await started.wait()

        with pytest.raises(AgentAPIError) as caught:
            await service.run(request)

        assert caught.value.status_code == 429
        assert caught.value.code == "agent_busy"
        release.set()
        assert (await first).result.output == "done"
