"""Configuration for the RAG upload MCP server.

Environment variables are read only when :meth:`RagUploadSettings.from_env` is
called by the server entry point.  Merely importing this package never reads a
``.env`` file or mutates the process environment.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit, urlunsplit

DEFAULT_MAX_FILE_BYTES = 25 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 30.0


def _parse_bool(name: str, value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _validated_base_url(value: str) -> str:
    raw = value.strip()
    if not raw:
        raise ValueError("RAG_UPLOAD_BASE_URL must not be empty")

    try:
        parsed = urlsplit(raw)
        # Accessing ``port`` performs urllib's numeric/range validation.  Merely
        # reading hostname/netloc would otherwise accept values such as ``:abc``
        # and defer an unsafe InvalidURL exception until the tool call.
        port = parsed.port
        if port == 0:
            raise ValueError("port zero cannot identify an upload service")
    except ValueError as exc:
        raise ValueError("RAG_UPLOAD_BASE_URL has an invalid host or port") from exc
    if parsed.username or parsed.password:
        raise ValueError("RAG_UPLOAD_BASE_URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("RAG_UPLOAD_BASE_URL must not contain a query or fragment")
    if not parsed.hostname:
        raise ValueError("RAG_UPLOAD_BASE_URL must include a host")

    is_loopback = parsed.hostname.lower() == "localhost"
    try:
        is_loopback = is_loopback or ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        pass

    if parsed.scheme != "https" and not (parsed.scheme == "http" and is_loopback):
        raise ValueError("RAG_UPLOAD_BASE_URL must use HTTPS (HTTP is allowed only for loopback)")

    # Strip a trailing slash so request paths are deterministic in tests and logs.
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _validated_root(value: str | os.PathLike[str]) -> Path:
    root = Path(os.path.abspath(os.path.expanduser(os.fspath(value))))
    try:
        if root.is_symlink():
            raise ValueError("RAG_UPLOAD_ALLOWED_ROOTS entries must not be symlinks")
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise ValueError("every RAG_UPLOAD_ALLOWED_ROOTS entry must exist") from exc
    if not resolved.is_dir():
        raise ValueError("every RAG_UPLOAD_ALLOWED_ROOTS entry must be a directory")
    return resolved


@dataclass(frozen=True)
class RagUploadSettings:
    """Validated runtime settings for one RAG upload server process."""

    base_url: str
    allowed_roots: tuple[Path, ...]
    allowed_kb_names: frozenset[str] | None = None
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    bearer_token: str | None = None
    allow_archives: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", _validated_base_url(self.base_url))

        roots = tuple(_validated_root(root) for root in self.allowed_roots)
        if not roots:
            raise ValueError("RAG_UPLOAD_ALLOWED_ROOTS must contain at least one directory")
        object.__setattr__(self, "allowed_roots", roots)

        if self.allowed_kb_names is not None:
            names = frozenset(name.strip() for name in self.allowed_kb_names if name.strip())
            if not names:
                raise ValueError("RAG_UPLOAD_ALLOWED_KB_NAMES must not be an empty allowlist")
            object.__setattr__(self, "allowed_kb_names", names)

        if (
            not isinstance(self.max_file_bytes, int)
            or isinstance(self.max_file_bytes, bool)
            or self.max_file_bytes <= 0
        ):
            raise ValueError("RAG_UPLOAD_MAX_BYTES must be a positive integer")
        if not 0 < self.timeout_seconds <= 300:
            raise ValueError("RAG_UPLOAD_TIMEOUT_SECONDS must be between 0 and 300")
        if self.bearer_token is not None:
            token = self.bearer_token.strip()
            if not token or "\n" in token or "\r" in token:
                raise ValueError("RAG_UPLOAD_BEARER_TOKEN is invalid")
            object.__setattr__(self, "bearer_token", token)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "RagUploadSettings":
        """Build settings from an explicit mapping or the current environment."""

        source = os.environ if env is None else env
        base_url = source.get("RAG_UPLOAD_BASE_URL", "")
        raw_roots = source.get("RAG_UPLOAD_ALLOWED_ROOTS", "")
        roots = tuple(Path(item.strip()) for item in raw_roots.split(os.pathsep) if item.strip())

        raw_names = source.get("RAG_UPLOAD_ALLOWED_KB_NAMES", "").strip()
        allowed_names = (
            frozenset(item.strip() for item in raw_names.split(",") if item.strip())
            if raw_names
            else None
        )

        try:
            max_bytes = int(source.get("RAG_UPLOAD_MAX_BYTES", str(DEFAULT_MAX_FILE_BYTES)))
        except ValueError as exc:
            raise ValueError("RAG_UPLOAD_MAX_BYTES must be a positive integer") from exc
        try:
            timeout = float(
                source.get("RAG_UPLOAD_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))
            )
        except ValueError as exc:
            raise ValueError("RAG_UPLOAD_TIMEOUT_SECONDS must be a number") from exc

        allow_archives = _parse_bool(
            "RAG_UPLOAD_ALLOW_ARCHIVES", source.get("RAG_UPLOAD_ALLOW_ARCHIVES", "false")
        )
        token = source.get("RAG_UPLOAD_BEARER_TOKEN", "").strip() or None
        return cls(
            base_url=base_url,
            allowed_roots=roots,
            allowed_kb_names=allowed_names,
            max_file_bytes=max_bytes,
            timeout_seconds=timeout,
            bearer_token=token,
            allow_archives=allow_archives,
        )
