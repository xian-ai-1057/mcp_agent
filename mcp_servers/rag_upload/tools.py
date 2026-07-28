"""ToolSpec definitions for the isolated RAG upload MCP server."""

from __future__ import annotations

import re
from typing import Any

import httpx

from mcp_servers.rag_upload.client import RagUploadClient
from mcp_servers.rag_upload.config import RagUploadSettings
from mcp_servers.rag_upload.files import open_upload
from tools.base import ToolError, ToolSpec, object_schema

DESCRIPTION = """\
Submit a local document to the configured RAG knowledge base ingestion queue.

Call this only after the user explicitly asks to upload a file and identifies the
target knowledge base. The result confirms upload acceptance only; it does not
mean that parsing, embedding, or indexing has completed. If a timeout or transport
failure occurs, the outcome is unknown: do not retry automatically; verify upload
status first.\
"""

KB_NAME_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _validated_kb_name(value: Any, settings: RagUploadSettings) -> str:
    if not isinstance(value, str):
        raise ToolError("kb_name must be a string")
    name = value.strip()
    if not 1 <= len(name) <= 64:
        raise ToolError("kb_name must contain between 1 and 64 characters")
    if not KB_NAME_PATTERN.fullmatch(name):
        raise ToolError(
            "kb_name must start with a letter or underscore and contain only "
            "ASCII letters, numbers, and underscores"
        )
    if settings.allowed_kb_names is not None and name not in settings.allowed_kb_names:
        raise ToolError("kb_name is not permitted by RAG_UPLOAD_ALLOWED_KB_NAMES")
    return name


def _validated_expire_at(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError("expire_at must be an integer between 1 and 9999999999")
    if not 1 <= value <= 9_999_999_999:
        raise ToolError("expire_at must be an integer between 1 and 9999999999")
    return value


def build_specs(
    settings: RagUploadSettings,
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, ToolSpec]:
    """Build this server's registry, with an injectable transport for tests."""

    client = RagUploadClient(settings, transport=transport)

    def upload_document(arguments: dict[str, Any]) -> dict[str, Any]:
        file_path = arguments.get("file_path")
        if not isinstance(file_path, str):
            raise ToolError("file_path must be a string")
        kb_name = _validated_kb_name(arguments.get("kb_name"), settings)
        expire_at = _validated_expire_at(arguments.get("expire_at"))

        with open_upload(
            file_path,
            allowed_roots=settings.allowed_roots,
            max_file_bytes=settings.max_file_bytes,
            allow_archives=settings.allow_archives,
        ) as upload:
            return client.upload(upload, kb_name=kb_name, expire_at=expire_at)

    spec = ToolSpec(
        name="upload_document",
        description=DESCRIPTION,
        input_schema=object_schema(
            {
                "file_path": {
                    "type": "string",
                    "description": (
                        "Absolute or working-directory-relative path under an allowed root."
                    ),
                },
                "kb_name": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 64,
                    "pattern": KB_NAME_PATTERN.pattern,
                    "description": "Destination knowledge-base name.",
                },
                "expire_at": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 9_999_999_999,
                    "description": "Optional upstream expiration value from 1 to 9999999999.",
                },
            },
            required=["file_path", "kb_name"],
        ),
        handler=upload_document,
        tags=("rag", "upload", "external", "side-effect"),
    )
    return {spec.name: spec}
