"""Filesystem boundary for upload candidates.

Validation and opening happen together so the HTTP client never receives an
unchecked path.  The final file descriptor is opened with ``O_NOFOLLOW`` where
the platform provides it, then compared with the inode that was validated.
"""

from __future__ import annotations

import mimetypes
import os
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator

from tools.base import ToolError

ARCHIVE_SUFFIXES = {".zip", ".7z"}
ZIP_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
SEVEN_Z_SIGNATURE = b"7z\xbc\xaf\x27\x1c"
SNAPSHOT_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class OpenUpload:
    """An already validated descriptor, held open for the multipart request."""

    stream: BinaryIO
    filename: str
    size_bytes: int
    content_type: str


def _lexical_absolute(raw_path: str) -> Path:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ToolError("file_path must not be empty")
    if "\x00" in raw_path:
        raise ToolError("file_path is invalid")
    return Path(os.path.abspath(os.path.expanduser(raw_path)))


def _reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            mode = current.lstat().st_mode
        except OSError as exc:
            raise ToolError("file is not accessible") from exc
        if stat.S_ISLNK(mode):
            raise ToolError("symbolic links are not allowed for uploads")


def _within_allowed_root(path: Path, allowed_roots: tuple[Path, ...]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in allowed_roots)


def _validate_filename(filename: str) -> None:
    if not filename or len(filename) > 255:
        raise ToolError("upload filename is invalid")
    if any(ord(character) < 32 or ord(character) == 127 for character in filename):
        raise ToolError("upload filename is invalid")


def _looks_like_blocked_archive(filename: str, signature: bytes) -> bool:
    return (
        Path(filename).suffix.lower() in ARCHIVE_SUFFIXES
        or signature.startswith(ZIP_SIGNATURES)
        or signature.startswith(SEVEN_Z_SIGNATURE)
    )


def _snapshot_source(
    source: BinaryIO,
    snapshot: BinaryIO,
    *,
    max_file_bytes: int,
) -> int:
    """Copy at most the configured limit plus one byte into a private snapshot."""

    total = 0
    while True:
        remaining_with_sentinel = max_file_bytes - total + 1
        chunk = source.read(min(SNAPSHOT_CHUNK_BYTES, remaining_with_sentinel))
        if not chunk:
            break
        total += len(chunk)
        if total > max_file_bytes:
            raise ToolError(f"file exceeds the configured {max_file_bytes}-byte upload limit")
        snapshot.write(chunk)

    if total == 0:
        raise ToolError("empty files cannot be uploaded")
    snapshot.seek(0)
    return total


@contextmanager
def open_upload(
    raw_path: str,
    *,
    allowed_roots: tuple[Path, ...],
    max_file_bytes: int,
    allow_archives: bool,
) -> Iterator[OpenUpload]:
    """Validate and open a local upload without exposing its path downstream."""

    lexical = _lexical_absolute(raw_path)
    _reject_symlink_components(lexical)
    try:
        resolved = lexical.resolve(strict=True)
        expected = resolved.stat(follow_symlinks=False)
    except OSError as exc:
        raise ToolError("file is not accessible") from exc

    if not _within_allowed_root(resolved, allowed_roots):
        raise ToolError("file_path is outside RAG_UPLOAD_ALLOWED_ROOTS")
    # Check before opening so a FIFO cannot block this process. The same check
    # is repeated on the descriptor below to close the validation/open race.
    if not stat.S_ISREG(expected.st_mode):
        raise ToolError("file_path must refer to a regular file")

    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lexical, flags)
    except OSError as exc:
        raise ToolError("file is not accessible") from exc

    try:
        actual = os.fstat(descriptor)
        if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            raise ToolError("file changed while it was being validated")
        if not stat.S_ISREG(actual.st_mode):
            raise ToolError("file_path must refer to a regular file")

        filename = lexical.name
        _validate_filename(filename)
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

        # HTTPX consumes file objects lazily.  Never hand it the live source FD:
        # another process could modify or grow that inode after validation and
        # thereby change the request or bypass the byte limit.  Snapshot through
        # a bounded reader, close the source, then validate and upload only the
        # private snapshot.  SpooledTemporaryFile is memory-backed for small
        # documents and rolls over to a private temporary file for larger ones.
        snapshot = tempfile.SpooledTemporaryFile(
            max_size=min(max_file_bytes, SNAPSHOT_CHUNK_BYTES),
            mode="w+b",
        )
        try:
            with os.fdopen(descriptor, "rb", closefd=True) as source:
                descriptor = -1
                size_bytes = _snapshot_source(
                    source,
                    snapshot,
                    max_file_bytes=max_file_bytes,
                )

            signature = snapshot.read(8)
            snapshot.seek(0)
            if not allow_archives and _looks_like_blocked_archive(filename, signature):
                raise ToolError("ZIP and 7z archives are disabled for RAG uploads")

            yield OpenUpload(
                stream=snapshot,
                filename=filename,
                size_bytes=size_bytes,
                content_type=content_type,
            )
        finally:
            snapshot.close()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
