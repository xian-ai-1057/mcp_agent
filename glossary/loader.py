"""CSV loading, pattern pre-compilation, and mtime-driven reload.

See `specs/001-glossary-core/spec.md` §4.
"""

import csv
import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from contracts.glossary import GlossaryEntry
from glossary.normalize import compile_pattern, expand_forms, normalize_zh

logger = logging.getLogger(__name__)

REQUIRED_COLUMNS = ("zh", "en", "category")
ALIAS_SEPARATOR = "|"


class GlossaryError(Exception):
    """The CSV could not be turned into a usable glossary."""


@dataclass(frozen=True)
class Glossary:
    """An immutable, fully pre-compiled view of the glossary CSV."""

    entries: tuple[GlossaryEntry, ...]
    by_zh: Mapping[str, GlossaryEntry]
    surface_to_entry: Mapping[str, GlossaryEntry]
    scan_pattern: re.Pattern[str]
    stamp: tuple[int, int] = (0, 0)
    _en_patterns: Mapping[str, re.Pattern[str]] = field(default_factory=dict, repr=False)
    _overlaps: Mapping[str, tuple[str, ...]] = field(default_factory=dict, repr=False)

    def __len__(self) -> int:
        return len(self.entries)

    def en_pattern_for(self, zh: str) -> re.Pattern[str]:
        """Compiled matcher for every accepted English form of `zh`."""
        return self._en_patterns[zh]

    def overlapping(self, zh: str) -> tuple[GlossaryEntry, ...]:
        """Entries whose Chinese term nests with `zh` in either direction.

        These are the confusable neighbours: rendering `臨時額度` as "credit
        limit" is a wrong term, not a missing one, and the matcher uses this to
        tell the two apart.
        """
        return tuple(self.by_zh[other] for other in self._overlaps.get(zh, ()))


def _split_aliases(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(ALIAS_SEPARATOR) if part.strip()]


def _read_rows(path: Path) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                raise GlossaryError(f"{path}: file is empty")
            missing = [c for c in REQUIRED_COLUMNS if c not in reader.fieldnames]
            if missing:
                raise GlossaryError(f"{path}: missing required column(s): {', '.join(missing)}")
            return list(reader)
    except FileNotFoundError as exc:
        raise GlossaryError(f"{path}: glossary CSV not found") from exc
    except UnicodeDecodeError as exc:
        raise GlossaryError(f"{path}: not valid UTF-8 ({exc})") from exc


def _build_entries(rows: list[dict[str, str]], path: Path) -> tuple[GlossaryEntry, ...]:
    entries: list[GlossaryEntry] = []
    seen_zh: dict[str, int] = {}

    for offset, row in enumerate(rows):
        line = offset + 2  # header is line 1
        if not any((value or "").strip() for value in row.values()):
            continue
        try:
            entry = GlossaryEntry(
                zh=normalize_zh(row["zh"] or ""),
                en=(row["en"] or "").strip(),
                aliases=[normalize_zh(a) for a in _split_aliases(row.get("aliases"))],
                category=(row["category"] or "").strip(),
            )
        except Exception as exc:
            raise GlossaryError(f"{path}:{line}: invalid row ({exc})") from exc

        if entry.zh in seen_zh:
            raise GlossaryError(
                f"{path}:{line}: duplicate term {entry.zh!r} (first seen on line {seen_zh[entry.zh]})"
            )
        seen_zh[entry.zh] = line
        entries.append(entry)

    if not entries:
        raise GlossaryError(f"{path}: no usable rows")
    return tuple(entries)


def _build_surface_index(
    entries: tuple[GlossaryEntry, ...], path: Path
) -> dict[str, GlossaryEntry]:
    """Map every scannable Chinese surface to its entry.

    A canonical term always wins over another entry's alias — the canonical form
    is the more specific claim. Two entries claiming the *same* alias is an
    unresolvable ambiguity and fails the load.
    """
    canonical = {entry.zh: entry for entry in entries}
    surfaces = dict(canonical)
    alias_owner: dict[str, GlossaryEntry] = {}

    for entry in entries:
        for alias in entry.aliases:
            if alias in canonical:
                logger.warning(
                    "%s: alias %r of %r collides with a canonical term; keeping the canonical entry",
                    path,
                    alias,
                    entry.zh,
                )
                continue
            previous = alias_owner.get(alias)
            if previous is not None and previous.zh != entry.zh:
                raise GlossaryError(
                    f"{path}: alias {alias!r} is claimed by both {previous.zh!r} and {entry.zh!r}"
                )
            alias_owner[alias] = entry
            surfaces[alias] = entry

    return surfaces


def _build_overlaps(entries: tuple[GlossaryEntry, ...]) -> dict[str, tuple[str, ...]]:
    terms = [entry.zh for entry in entries]
    overlaps: dict[str, tuple[str, ...]] = {}
    for term in terms:
        neighbours = [
            other for other in terms if other != term and (other in term or term in other)
        ]
        if neighbours:
            overlaps[term] = tuple(neighbours)
    return overlaps


def build_glossary(
    entries: tuple[GlossaryEntry, ...],
    path: Path,
    stamp: tuple[int, int] = (0, 0),
) -> Glossary:
    surfaces = _build_surface_index(entries, path)
    # Longest first: the scanner relies on alternation order for longest-match
    # semantics, so this sort *is* the algorithm (spec 001 §5).
    ordered = sorted(surfaces, key=lambda s: (-len(s), s))
    pattern = (
        re.compile("|".join(re.escape(s) for s in ordered)) if ordered else re.compile(r"(?!x)x")
    )
    return Glossary(
        entries=entries,
        by_zh=MappingProxyType({entry.zh: entry for entry in entries}),
        surface_to_entry=MappingProxyType(surfaces),
        scan_pattern=pattern,
        stamp=stamp,
        _en_patterns=MappingProxyType(
            {entry.zh: compile_pattern(expand_forms(entry.en)) for entry in entries}
        ),
        _overlaps=MappingProxyType(_build_overlaps(entries)),
    )


def _stamp(path: Path) -> tuple[int, int]:
    """Change token for the CSV.

    Size travels with mtime because one-second `mtime` granularity on some
    filesystems can hide an edit made in the same second as the previous load.
    """
    stat = path.stat()
    return (stat.st_mtime_ns, stat.st_size)


def load_glossary(path: str | Path) -> Glossary:
    """Read and compile the CSV once. Raises `GlossaryError` on bad data."""
    resolved = Path(path)
    try:
        stamp = _stamp(resolved)
    except FileNotFoundError as exc:
        raise GlossaryError(f"{resolved}: glossary CSV not found") from exc
    except OSError as exc:
        raise GlossaryError(f"{resolved}: cannot stat glossary CSV ({exc})") from exc
    entries = _build_entries(_read_rows(resolved), resolved)
    return build_glossary(entries, resolved, stamp)


class GlossaryLoader:
    """Serves a `Glossary`, reloading it when the CSV changes on disk.

    Deliberately stat-on-access rather than load-once: the asset is updated on no
    fixed schedule, and a server that read it only at startup would serve stale
    translations silently. A reload of a few hundred rows is sub-millisecond, so
    there is nothing to amortise and no invalidation protocol worth operating.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._glossary: Glossary | None = None
        self._failed_stamp: tuple[int, int] | None = None
        self.reload_count = 0

    def get(self) -> Glossary:
        with self._lock:
            try:
                stamp = _stamp(self.path)
            except OSError as exc:
                if self._glossary is not None:
                    logger.warning("glossary %s unreadable (%s); serving cached copy", self.path, exc)
                    return self._glossary
                raise GlossaryError(f"{self.path}: cannot stat glossary CSV ({exc})") from exc

            if self._glossary is not None and self._glossary.stamp == stamp:
                return self._glossary

            try:
                glossary = load_glossary(self.path)
            except GlossaryError:
                # A broken edit degrades the system to "stale", never to "down".
                if self._glossary is None:
                    raise
                if self._failed_stamp != stamp:
                    self._failed_stamp = stamp
                    logger.exception("glossary %s failed to reload; keeping previous copy", self.path)
                return self._glossary

            self._glossary = glossary
            self._failed_stamp = None
            self.reload_count += 1
            logger.info("loaded %d glossary terms from %s", len(glossary), self.path)
            return glossary
