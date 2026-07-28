"""Find glossary terms in a Chinese sentence, longest match first.

See `specs/001-glossary-core/spec.md` §5.
"""

from contracts.glossary import TermMatch
from glossary.loader import Glossary, GlossaryConflictError
from glossary.normalize import normalize_zh


def scan(text: str, glossary: Glossary) -> list[TermMatch]:
    """Return every glossary term in `text`, in text order.

    One left-to-right pass over `glossary.scan_pattern`. That pattern's
    alternation is ordered longest-surface-first, so at any position the longest
    term wins; `finditer` then resumes *after* the match, so a shorter term
    nested inside an accepted span is never re-examined. `臨時額度` is therefore
    reported and the `額度` inside it is not.

    This matters because the glossary contains families — `額度 / 臨時額度 /
    永久額度` — where the short term's English is *wrong* for the long term, not
    merely vaguer. Injecting both would hand the model two contradictory
    instructions about the same span of text.

    Repeat occurrences are each returned with their own span; de-duplication is
    the caller's decision, which is why offsets are part of the contract. A
    quarantined surface participates in the same longest-first pattern but
    raises instead of falling through to an arbitrary or nested translation.
    """
    if not text:
        return []

    normalized = normalize_zh(text)
    matches: list[TermMatch] = []

    for found in glossary.scan_pattern.finditer(normalized):
        surface = found.group(0)
        conflict = glossary.conflict_for(surface)
        if conflict is not None:
            raise GlossaryConflictError(conflict)
        entry = glossary.surface_to_entry.get(surface)
        if entry is None:  # pragma: no cover - pattern is built from both indexes
            continue
        matches.append(
            TermMatch(zh=entry.zh, en=entry.en, start=found.start(), end=found.end())
        )

    return matches


def unique_terms(matches: list[TermMatch]) -> list[TermMatch]:
    """One `TermMatch` per canonical term, keeping the first occurrence."""
    seen: set[str] = set()
    result: list[TermMatch] = []
    for match in matches:
        if match.zh not in seen:
            seen.add(match.zh)
            result.append(match)
    return result
