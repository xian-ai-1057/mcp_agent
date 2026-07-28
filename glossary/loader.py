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
from typing import Literal, Mapping

from contracts.glossary import GlossaryEntry
from glossary.normalize import compile_pattern, expand_forms, normalize_en, normalize_zh

logger = logging.getLogger(__name__)

REQUIRED_COLUMNS = ("zh", "en", "category")
ALIAS_SEPARATOR = "|"
ConflictPolicy = Literal["error", "quarantine"]


class GlossaryError(Exception):
    """The CSV could not be turned into a usable glossary."""


@dataclass(frozen=True)
class GlossaryConflict:
    """One surface that cannot be mapped to a single authoritative entry."""

    source: str
    surface: str
    lines: tuple[int, ...]
    terms: tuple[str, ...]
    reason: str

    @property
    def message(self) -> str:
        line_list = ", ".join(str(line) for line in self.lines)
        if self.reason == "duplicate term":
            return (
                f"{self.source}: glossary term {self.surface!r} has conflicting "
                f"translations on lines {line_list}"
            )
        term_list = ", ".join(repr(term) for term in self.terms)
        return (
            f"{self.source}: glossary alias {self.surface!r} is claimed by {term_list} "
            f"on lines {line_list}"
        )


class GlossaryConflictError(GlossaryError):
    """A request selected a quarantined, ambiguous glossary surface."""

    def __init__(self, conflict: GlossaryConflict) -> None:
        self.conflict = conflict
        super().__init__(conflict.message)


@dataclass(frozen=True)
class Glossary:
    """An immutable, fully pre-compiled view of the glossary CSV."""

    entries: tuple[GlossaryEntry, ...]
    by_zh: Mapping[str, GlossaryEntry]
    surface_to_entry: Mapping[str, GlossaryEntry]
    scan_pattern: re.Pattern[str]
    stamp: tuple[int, int] = (0, 0)
    conflicts: Mapping[str, GlossaryConflict] = field(
        default_factory=lambda: MappingProxyType({})
    )
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

    def conflict_for(self, surface: str) -> GlossaryConflict | None:
        """Return quarantine metadata when ``surface`` is ambiguous."""
        return self.conflicts.get(surface)


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


def _build_entries(
    rows: list[dict[str, str]],
    path: Path,
    conflict_policy: ConflictPolicy,
) -> tuple[
    tuple[GlossaryEntry, ...],
    dict[str, tuple[int, ...]],
    dict[str, GlossaryConflict],
]:
    sourced: list[tuple[GlossaryEntry, int]] = []
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

        if conflict_policy == "error" and entry.zh in seen_zh:
            raise GlossaryError(
                f"{path}:{line}: duplicate term {entry.zh!r} "
                f"(first seen on line {seen_zh[entry.zh]})"
            )
        seen_zh.setdefault(entry.zh, line)
        sourced.append((entry, line))

    if not sourced:
        raise GlossaryError(f"{path}: no usable rows")

    grouped: dict[str, list[tuple[GlossaryEntry, int]]] = {}
    for entry, line in sourced:
        grouped.setdefault(entry.zh, []).append((entry, line))

    entries: list[GlossaryEntry] = []
    source_lines: dict[str, tuple[int, ...]] = {}
    conflicts: dict[str, GlossaryConflict] = {}

    for zh, group in grouped.items():
        first_entry, _ = group[0]
        lines = tuple(line for _, line in group)
        source_lines[zh] = lines
        if len(group) == 1:
            entries.append(first_entry)
            continue

        if conflict_policy == "error":  # pragma: no cover - rejected while parsing
            raise AssertionError("strict duplicate should have failed during row parsing")

        translations = {normalize_en(entry.en) for entry, _ in group}
        if len(translations) == 1:
            aliases = list(
                dict.fromkeys(alias for entry, _ in group for alias in entry.aliases)
            )
            entries.append(first_entry.model_copy(update={"aliases": aliases}))
            logger.warning(
                "%s: duplicate term %r on lines %s has the same translation; "
                "collapsed to one entry",
                path,
                zh,
                ", ".join(str(line) for line in lines),
            )
            continue

        conflict = GlossaryConflict(
            source=str(path),
            surface=zh,
            lines=lines,
            terms=(zh,),
            reason="duplicate term",
        )
        conflicts[zh] = conflict
        for entry, _ in group:
            for alias in entry.aliases:
                conflicts.setdefault(
                    alias,
                    GlossaryConflict(
                        source=str(path),
                        surface=alias,
                        lines=lines,
                        terms=(zh,),
                        reason="duplicate term",
                    ),
                )
        logger.warning("%s; quarantining the ambiguous term", conflict.message)

    return tuple(entries), source_lines, conflicts


def _build_surface_index(
    entries: tuple[GlossaryEntry, ...],
    path: Path,
    source_lines: Mapping[str, tuple[int, ...]],
    conflicts: dict[str, GlossaryConflict],
    conflict_policy: ConflictPolicy,
) -> tuple[dict[str, GlossaryEntry], dict[str, GlossaryConflict]]:
    """Map every scannable Chinese surface to its entry.

    A canonical term always wins over another entry's alias — the canonical form
    is the more specific claim. Two entries claiming the *same* alias fail in
    strict mode or quarantine only that alias in production mode.
    """
    canonical = {entry.zh: entry for entry in entries}
    surfaces = dict(canonical)
    alias_owner: dict[str, GlossaryEntry] = {}

    for entry in entries:
        for alias in entry.aliases:
            existing_conflict = conflicts.get(alias)
            if existing_conflict is not None:
                if (
                    conflict_policy == "quarantine"
                    and existing_conflict.reason == "alias collision"
                    and entry.zh not in existing_conflict.terms
                ):
                    terms = (*existing_conflict.terms, entry.zh)
                    lines = tuple(
                        dict.fromkeys(
                            line for term in terms for line in source_lines.get(term, ())
                        )
                    )
                    conflicts[alias] = GlossaryConflict(
                        source=str(path),
                        surface=alias,
                        lines=lines,
                        terms=terms,
                        reason="alias collision",
                    )
                continue
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
                if conflict_policy == "error":
                    previous_lines = source_lines.get(previous.zh, ())
                    current_lines = source_lines.get(entry.zh, ())
                    location = f"{path}:{current_lines[0]}" if current_lines else str(path)
                    first_claim = (
                        f" (first claimed on line {previous_lines[0]})"
                        if previous_lines
                        else ""
                    )
                    raise GlossaryError(
                        f"{location}: alias {alias!r} is claimed by both "
                        f"{previous.zh!r} and {entry.zh!r}{first_claim}"
                    )
                terms = tuple(dict.fromkeys((previous.zh, entry.zh)))
                lines = tuple(
                    dict.fromkeys(
                        line for term in terms for line in source_lines.get(term, ())
                    )
                )
                conflict = GlossaryConflict(
                    source=str(path),
                    surface=alias,
                    lines=lines,
                    terms=terms,
                    reason="alias collision",
                )
                conflicts[alias] = conflict
                surfaces.pop(alias, None)
                logger.warning("%s; quarantining the ambiguous alias", conflict.message)
                continue
            alias_owner[alias] = entry
            surfaces[alias] = entry

    return surfaces, conflicts


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
    *,
    source_lines: Mapping[str, tuple[int, ...]] | None = None,
    conflicts: Mapping[str, GlossaryConflict] | None = None,
    conflict_policy: ConflictPolicy = "error",
) -> Glossary:
    if conflict_policy not in {"error", "quarantine"}:
        raise ValueError(f"unknown glossary conflict policy: {conflict_policy!r}")
    seen_canonical: set[str] = set()
    for entry in entries:
        if entry.zh in seen_canonical:
            raise GlossaryError(
                f"{path}: duplicate term {entry.zh!r} in pre-built glossary entries"
            )
        seen_canonical.add(entry.zh)

    mutable_conflicts = dict(conflicts or {})
    surfaces, mutable_conflicts = _build_surface_index(
        entries,
        path,
        source_lines or {},
        mutable_conflicts,
        conflict_policy,
    )
    # A canonical entry is always more specific than an ambiguous alias. A
    # quarantined canonical term, however, must suppress another entry's alias.
    canonical = {entry.zh for entry in entries}
    for surface in tuple(mutable_conflicts):
        if surface in canonical:
            mutable_conflicts.pop(surface)
        else:
            surfaces.pop(surface, None)
    # Longest first: the scanner relies on alternation order for longest-match
    # semantics, so this sort *is* the algorithm (spec 001 §5).
    ordered = sorted(
        {*surfaces, *mutable_conflicts},
        key=lambda surface: (-len(surface), surface),
    )
    pattern = (
        re.compile("|".join(re.escape(s) for s in ordered)) if ordered else re.compile(r"(?!x)x")
    )
    return Glossary(
        entries=entries,
        by_zh=MappingProxyType({entry.zh: entry for entry in entries}),
        surface_to_entry=MappingProxyType(surfaces),
        conflicts=MappingProxyType(mutable_conflicts),
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


def load_glossary(
    path: str | Path,
    *,
    conflict_policy: ConflictPolicy = "error",
) -> Glossary:
    """Read and compile the CSV once.

    Strict conflict handling is the public default. Production runtime opts in
    to term-scoped quarantine explicitly; malformed rows remain fatal in both.
    """
    if conflict_policy not in {"error", "quarantine"}:
        raise ValueError(f"unknown glossary conflict policy: {conflict_policy!r}")
    resolved = Path(path)
    try:
        stamp = _stamp(resolved)
    except FileNotFoundError as exc:
        raise GlossaryError(f"{resolved}: glossary CSV not found") from exc
    except OSError as exc:
        raise GlossaryError(f"{resolved}: cannot stat glossary CSV ({exc})") from exc
    entries, source_lines, conflicts = _build_entries(
        _read_rows(resolved), resolved, conflict_policy
    )
    return build_glossary(
        entries,
        resolved,
        stamp,
        source_lines=source_lines,
        conflicts=conflicts,
        conflict_policy=conflict_policy,
    )


class GlossaryLoader:
    """Serves a `Glossary`, reloading it when the CSV changes on disk.

    Deliberately stat-on-access rather than load-once: the asset is updated on no
    fixed schedule, and a server that read it only at startup would serve stale
    translations silently. A reload of a few hundred rows is sub-millisecond, so
    there is nothing to amortise and no invalidation protocol worth operating.
    ``conflict_policy`` controls only duplicate/alias ambiguity; it never makes
    structurally invalid rows acceptable.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        conflict_policy: ConflictPolicy = "error",
    ) -> None:
        if conflict_policy not in {"error", "quarantine"}:
            raise ValueError(f"unknown glossary conflict policy: {conflict_policy!r}")
        self.path = Path(path)
        self.conflict_policy = conflict_policy
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
                glossary = load_glossary(
                    self.path,
                    conflict_policy=self.conflict_policy,
                )
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
            logger.info(
                "loaded %d glossary terms from %s (%d quarantined surfaces)",
                len(glossary),
                self.path,
                len(glossary.conflicts),
            )
            return glossary
