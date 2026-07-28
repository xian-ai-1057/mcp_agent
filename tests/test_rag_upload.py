"""Hermetic security and HTTP-contract tests for the RAG upload MCP adapter."""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from mcp_servers.rag_upload.client import UPLOAD_PATH, RagUploadError
from mcp_servers.rag_upload.config import RagUploadSettings
from mcp_servers.rag_upload.tools import build_specs
from tools.base import ToolError


def settings(root: Path, **overrides) -> RagUploadSettings:
    values = {
        "base_url": "https://rag.example.test",
        "allowed_roots": (root,),
        "max_file_bytes": 1024,
        "timeout_seconds": 5.0,
    }
    values.update(overrides)
    return RagUploadSettings(**values)


def test_base_url_requires_https_except_for_loopback(tmp_path):
    with pytest.raises(ValueError, match="HTTPS"):
        settings(tmp_path, base_url="http://rag.example.test")

    assert settings(tmp_path, base_url="http://127.0.0.1:8080").base_url.startswith("http://")
    assert settings(tmp_path, base_url="http://localhost:8080").base_url.startswith("http://")


@pytest.mark.parametrize(
    "base_url",
    [
        "https://rag.example.test:not-a-port",
        "https://rag.example.test:0",
        "https://rag.example.test:65536",
    ],
)
def test_base_url_rejects_invalid_ports_during_configuration(tmp_path, base_url):
    with pytest.raises(ValueError, match="invalid host or port"):
        settings(tmp_path, base_url=base_url)


def test_environment_requires_an_explicit_allowed_root(tmp_path):
    configured = RagUploadSettings.from_env(
        {
            "RAG_UPLOAD_BASE_URL": "https://rag.example.test/",
            "RAG_UPLOAD_ALLOWED_ROOTS": str(tmp_path),
            "RAG_UPLOAD_ALLOWED_KB_NAMES": "support,manuals",
        }
    )
    assert configured.base_url == "https://rag.example.test"
    assert configured.allowed_kb_names == frozenset({"support", "manuals"})
    assert configured.bearer_token is None

    with pytest.raises(ValueError, match="at least one"):
        RagUploadSettings.from_env({"RAG_UPLOAD_BASE_URL": "https://rag.example.test"})


def test_rejects_outside_root_symlinks_and_non_regular_files(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = allowed / "link.txt"
    link.symlink_to(outside)
    spec = build_specs(settings(allowed))["upload_document"]

    with pytest.raises(ToolError, match="outside"):
        spec.run({"file_path": str(outside), "kb_name": "manuals"})
    with pytest.raises(ToolError, match="symbolic links"):
        spec.run({"file_path": str(link), "kb_name": "manuals"})
    with pytest.raises(ToolError, match="regular file"):
        spec.run({"file_path": str(allowed), "kb_name": "manuals"})


@pytest.mark.parametrize(
    ("filename", "content", "limit", "message"),
    [
        ("empty.txt", b"", 1024, "empty"),
        ("large.txt", b"12345", 4, "configured 4-byte"),
        ("bundle.zip", b"plain text", 1024, "archives are disabled"),
        ("disguised.txt", b"PK\x03\x04contents", 1024, "archives are disabled"),
        ("bundle.7z", b"plain text", 1024, "archives are disabled"),
        ("disguised.bin", b"7z\xbc\xaf\x27\x1ccontents", 1024, "archives are disabled"),
    ],
)
def test_rejects_unsafe_file_shapes(tmp_path, filename, content, limit, message):
    candidate = tmp_path / filename
    candidate.write_bytes(content)
    spec = build_specs(settings(tmp_path, max_file_bytes=limit))["upload_document"]

    with pytest.raises(ToolError, match=message):
        spec.run({"file_path": str(candidate), "kb_name": "manuals"})


def test_archives_can_be_enabled_explicitly(tmp_path):
    archive = tmp_path / "bundle.zip"
    archive.write_bytes(b"PK\x03\x04contents")

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, json={"file_id": "file-1", "status": "pending"})

    spec = build_specs(
        settings(tmp_path, max_file_bytes=1024, allow_archives=True),
        transport=httpx.MockTransport(respond),
    )["upload_document"]
    assert spec.run({"file_path": str(archive), "kb_name": "manuals"})["accepted"] is True


def test_validates_kb_name_allowlist_and_expiration_before_network(tmp_path):
    candidate = tmp_path / "guide.txt"
    candidate.write_text("hello", encoding="utf-8")
    spec = build_specs(
        settings(tmp_path, allowed_kb_names=frozenset({"customer_support", "manuals"}))
    )["upload_document"]

    with pytest.raises(ToolError, match="must start"):
        spec.run({"file_path": str(candidate), "kb_name": "../admin"})
    with pytest.raises(ToolError, match="must start"):
        spec.run({"file_path": str(candidate), "kb_name": "客戶支援"})
    with pytest.raises(ToolError, match="not permitted"):
        spec.run({"file_path": str(candidate), "kb_name": "other"})
    with pytest.raises(ToolError, match="between 1 and 9999999999"):
        spec.run(
            {"file_path": str(candidate), "kb_name": "manuals", "expire_at": 10_000_000_000}
        )
    with pytest.raises(ToolError, match="integer"):
        spec.run({"file_path": str(candidate), "kb_name": "manuals", "expire_at": True})


def test_posts_expected_multipart_and_returns_only_an_accepted_job_receipt(tmp_path):
    document = tmp_path / "guide.txt"
    document.write_bytes(b"safe document")
    captured: dict[str, object] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["authorization"] = request.headers.get("authorization")
        captured["content_type"] = request.headers["content-type"]
        captured["body"] = request.read()
        return httpx.Response(
            202,
            json={
                "data": {
                    "file_id": "file-123",
                    "status": "pending",
                    "created_at": "2026-07-28T10:00:00Z",
                    "update_at": "2026-07-28T10:00:01Z",
                    "internal_path": "/do/not/expose",
                }
            },
        )

    spec = build_specs(
        settings(tmp_path, bearer_token="test-token"),
        transport=httpx.MockTransport(respond),
    )["upload_document"]
    result = spec.run(
        {
            "file_path": str(document),
            "kb_name": "manuals",
            "expire_at": 2_000_000_000,
        }
    )

    assert captured["method"] == "POST"
    assert captured["path"] == UPLOAD_PATH
    assert captured["authorization"] == "Bearer test-token"
    assert str(captured["content_type"]).startswith("multipart/form-data; boundary=")
    body = captured["body"]
    assert isinstance(body, bytes)
    assert b'name="kb_name"' in body and b"manuals" in body
    assert b'name="expire_at"' in body and b"2000000000" in body
    assert b'name="file"; filename="guide.txt"' in body and b"safe document" in body

    assert result == {
        "accepted": True,
        "job": {
            "file_id": "file-123",
            "status": "pending",
            "kb_name": "manuals",
            "filename": "guide.txt",
            "size_bytes": 13,
            "created_at": "2026-07-28T10:00:00Z",
            "update_at": "2026-07-28T10:00:01Z",
        },
        "message": "Upload accepted; indexing completes asynchronously and is not confirmed here.",
    }
    assert "internal_path" not in repr(result)
    assert "indexed" not in result


def test_upload_stream_is_an_immutable_bounded_snapshot(tmp_path):
    original = b"validated document"
    changed = b"changed after validation" * 100
    document = tmp_path / "guide.txt"
    document.write_bytes(original)
    captured: dict[str, bytes] = {}

    class MutatingTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            # This runs after open_upload has yielded to the HTTP client but
            # before the transport consumes the multipart stream.
            document.write_bytes(changed)
            captured["body"] = b"".join(request.stream)
            return httpx.Response(200, json={"file_id": "file-1", "status": "pending"})

    spec = build_specs(
        settings(tmp_path, max_file_bytes=len(original)),
        transport=MutatingTransport(),
    )["upload_document"]
    result = spec.run({"file_path": str(document), "kb_name": "manuals"})

    assert result["job"]["size_bytes"] == len(original)
    assert len(changed) > len(original)
    assert original in captured["body"]
    assert changed not in captured["body"]


def test_base_url_path_prefix_is_preserved(tmp_path):
    document = tmp_path / "guide.txt"
    document.write_text("hello", encoding="utf-8")
    seen = {}

    def respond(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json={"file_id": "file-1", "status": "pending"})

    spec = build_specs(
        settings(tmp_path, base_url="https://rag.example.test/manager"),
        transport=httpx.MockTransport(respond),
    )["upload_document"]
    spec.run({"file_path": str(document), "kb_name": "manuals"})
    assert seen["path"] == f"/manager{UPLOAD_PATH}"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"file_id": "file-1"},
        {"status": "pending"},
        {"file_id": 123, "status": "pending"},
        {"file_id": "file-1", "status": None},
        ["file-1", "pending"],
    ],
)
def test_requires_file_id_and_status_in_success_receipt(tmp_path, payload):
    document = tmp_path / "guide.txt"
    document.write_text("hello", encoding="utf-8")
    spec = build_specs(
        settings(tmp_path),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
    )["upload_document"]

    with pytest.raises(RagUploadError, match="receipt"):
        spec.run({"file_path": str(document), "kb_name": "manuals"})


def test_http_failure_masks_response_body_token_and_local_path(tmp_path):
    document = tmp_path / "private-name.txt"
    document.write_text("hello", encoding="utf-8")

    def reject(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            headers={"x-request-id": "req-safe"},
            text="backend-secret test-token private-name.txt",
        )

    spec = build_specs(
        settings(tmp_path, bearer_token="test-token"),
        transport=httpx.MockTransport(reject),
    )["upload_document"]
    with pytest.raises(RagUploadError) as captured:
        spec.run({"file_path": str(document), "kb_name": "manuals"})

    message = str(captured.value)
    assert "HTTP 500" in message and "req-safe" in message
    assert "backend-secret" not in message
    assert "test-token" not in message
    assert "private-name.txt" not in message


def test_timeout_is_reported_without_request_details(tmp_path):
    document = tmp_path / "guide.txt"
    document.write_text("hello", encoding="utf-8")

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("secret internal URL", request=request)

    spec = build_specs(
        settings(tmp_path), transport=httpx.MockTransport(timeout)
    )["upload_document"]
    with pytest.raises(RagUploadError, match="timed out") as captured:
        spec.run({"file_path": str(document), "kb_name": "manuals"})
    message = str(captured.value)
    assert "outcome is unknown" in message
    assert "Do not retry automatically" in message
    assert "secret internal URL" not in message


def test_transport_failure_reports_unknown_outcome_without_request_details(tmp_path):
    document = tmp_path / "guide.txt"
    document.write_text("hello", encoding="utf-8")

    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("secret internal URL", request=request)

    spec = build_specs(
        settings(tmp_path), transport=httpx.MockTransport(fail)
    )["upload_document"]
    with pytest.raises(RagUploadError, match="transport failed") as captured:
        spec.run({"file_path": str(document), "kb_name": "manuals"})

    message = str(captured.value)
    assert "outcome is unknown" in message
    assert "Do not retry automatically" in message
    assert "secret internal URL" not in message


def test_invalid_url_from_http_client_is_safely_normalized(tmp_path):
    document = tmp_path / "guide.txt"
    document.write_text("hello", encoding="utf-8")

    def invalid_url(request: httpx.Request) -> httpx.Response:
        raise httpx.InvalidURL("secret malformed endpoint")

    spec = build_specs(
        settings(tmp_path), transport=httpx.MockTransport(invalid_url)
    )["upload_document"]
    with pytest.raises(RagUploadError, match="configuration is invalid") as captured:
        spec.run({"file_path": str(document), "kb_name": "manuals"})
    assert "secret malformed endpoint" not in str(captured.value)


def test_tool_schema_is_strict_and_describes_acceptance_semantics(tmp_path):
    spec = build_specs(settings(tmp_path))["upload_document"]
    assert spec.input_schema["additionalProperties"] is False
    assert spec.input_schema["required"] == ["file_path", "kb_name"]
    assert spec.input_schema["properties"]["kb_name"]["pattern"] == (
        r"^[a-zA-Z_][a-zA-Z0-9_]*$"
    )
    assert spec.input_schema["properties"]["expire_at"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 9_999_999_999,
        "description": "Optional upstream expiration value from 1 to 9999999999.",
    }
    assert "Call this" in spec.description
    assert "does not" in spec.description
    assert "outcome is unknown" in spec.description
    assert "do not retry automatically" in spec.description


async def test_stdio_server_advertises_only_the_rag_upload_tool(tmp_path):
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_servers.rag_upload"],
        env={
            "RAG_UPLOAD_BASE_URL": "http://127.0.0.1:8080",
            "RAG_UPLOAD_ALLOWED_ROOTS": str(tmp_path),
        },
    )
    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            listed = await session.list_tools()

    assert {tool.name for tool in listed.tools} == {"upload_document"}
