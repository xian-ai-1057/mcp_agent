"""Small HTTP adapter for the RAG service's upload contract."""

from __future__ import annotations

from typing import Any

import httpx

from mcp_servers.rag_upload.config import RagUploadSettings
from mcp_servers.rag_upload.files import OpenUpload
from tools.base import ToolError

UPLOAD_PATH = "/datacenter/v1/file"
SAFE_RECEIPT_FIELDS = ("created_at", "update_at", "expire_at")


class RagUploadError(ToolError):
    """A safe, caller-actionable RAG upload failure."""


class RagUploadClient:
    """Submit validated files without importing any RAG implementation code."""

    def __init__(
        self,
        settings: RagUploadSettings,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport

    def upload(self, upload: OpenUpload, *, kb_name: str, expire_at: int | None) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        if self._settings.bearer_token:
            headers["Authorization"] = f"Bearer {self._settings.bearer_token}"

        form = {"kb_name": kb_name}
        if expire_at is not None:
            # Multipart form values are strings; the upstream contract parses
            # this decimal representation as its bounded integer field.
            form["expire_at"] = str(expire_at)

        try:
            with httpx.Client(
                headers=headers,
                timeout=self._settings.timeout_seconds,
                transport=self._transport,
                follow_redirects=False,
            ) as client:
                response = client.post(
                    f"{self._settings.base_url}{UPLOAD_PATH}",
                    data=form,
                    files={
                        "file": (upload.filename, upload.stream, upload.content_type),
                    },
                )
        except httpx.InvalidURL:
            raise RagUploadError("RAG upload service configuration is invalid") from None
        except httpx.TimeoutException:
            raise RagUploadError(
                "RAG upload timed out; the outcome is unknown. "
                "Do not retry automatically; verify upload status first."
            ) from None
        except httpx.RequestError:
            raise RagUploadError(
                "RAG upload transport failed; the outcome is unknown. "
                "Do not retry automatically; verify upload status first."
            ) from None

        if not 200 <= response.status_code < 300:
            request_id = _safe_request_id(response)
            suffix = f"; request_id={request_id}" if request_id else ""
            raise RagUploadError(f"RAG upload was rejected (HTTP {response.status_code}{suffix})")

        try:
            payload = response.json()
        except ValueError:
            raise RagUploadError("RAG upload service returned an invalid receipt") from None

        receipt = payload.get("data", payload) if isinstance(payload, dict) else None
        if not isinstance(receipt, dict):
            raise RagUploadError("RAG upload service returned an invalid receipt")

        file_id = receipt.get("file_id")
        status = receipt.get("status")
        if not isinstance(file_id, str) or not file_id.strip():
            raise RagUploadError("RAG upload receipt is missing file_id")
        if not isinstance(status, str) or not status.strip():
            raise RagUploadError("RAG upload receipt is missing status")

        job: dict[str, Any] = {
            "file_id": file_id.strip(),
            "status": status.strip(),
            "kb_name": kb_name,
            "filename": upload.filename,
            "size_bytes": upload.size_bytes,
        }
        for key in SAFE_RECEIPT_FIELDS:
            value = receipt.get(key)
            if isinstance(value, (str, int, float, bool)) or value is None:
                if key in receipt:
                    job[key] = value

        return {
            "accepted": True,
            "job": job,
            "message": (
                "Upload accepted; indexing completes asynchronously and is not confirmed here."
            ),
        }


def _safe_request_id(response: httpx.Response) -> str:
    value = response.headers.get("x-request-id", "")
    if not value or len(value) > 128:
        return ""
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return ""
    return value
