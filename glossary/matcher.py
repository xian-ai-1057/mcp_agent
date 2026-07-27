"""Judge whether a translation used the required English terms.

**This is the single implementation of the HIT/WRONG/MISS verdict.** Offline
evaluation and the online `verify_translation` tool both import `match_terms`
from here. Re-implementing it anywhere else — even to avoid a dependency —
produces the one failure mode that stays invisible until it reaches users:
passing offline and failing online.

See `specs/001-glossary-core/spec.md` §6.
"""

from typing import Iterable

from contracts.glossary import GlossaryEntry, TermMatch
from contracts.tools import TermVerdict, Verdict
from glossary.loader import Glossary
from glossary.normalize import normalize_en


def _as_entries(
    terms: Iterable[GlossaryEntry | TermMatch | str], glossary: Glossary
) -> list[GlossaryEntry]:
    """Accept whatever the caller has — scan output, entries, or bare terms."""
    entries: list[GlossaryEntry] = []
    seen: set[str] = set()
    for term in terms:
        zh = term if isinstance(term, str) else term.zh
        if zh in seen:
            continue
        entry = glossary.by_zh.get(zh)
        if entry is None:
            continue
        seen.add(zh)
        entries.append(entry)
    return entries


Span = tuple[int, int, str]


def _spans(pattern, haystack: str) -> list[Span]:
    return [(m.start(), m.end(), m.group(0)) for m in pattern.finditer(haystack)]


def _swallowed(span: Span, others: list[Span]) -> bool:
    """True if `span` only occurs inside a strictly longer neighbour's match.

    `額度` expands to "credit limit", which also sits inside "temporary credit
    limit". Without this check, translating `額度` as `臨時額度` would score as a
    HIT — the system would be blind to exactly the confusion the glossary
    families create, which is the failure this whole layer exists to catch.
    """
    start, end, _ = span
    length = end - start
    return any(
        other_start <= start and end <= other_end and (other_end - other_start) > length
        for other_start, other_end, _ in others
    )


def match_terms(
    translation: str,
    terms: Iterable[GlossaryEntry | TermMatch | str],
    glossary: Glossary,
) -> list[TermVerdict]:
    """Verdict per term, de-duplicated by canonical term, in the given order.

    `HIT`   — an accepted English form of the term is present *on its own*, not
              merely as a substring of an overlapping term's English.
    `WRONG` — not a HIT, but an overlapping term's English is present instead;
              `found` carries what was written. This is the failure the glossary
              families produce, and it deserves a different diagnosis from a term
              the model simply never attempted.
    `MISS`  — neither.
    """
    haystack = normalize_en(translation)
    verdicts: list[TermVerdict] = []

    for entry in _as_entries(terms, glossary):
        neighbour_spans: list[Span] = []
        for neighbour in glossary.overlapping(entry.zh):
            neighbour_spans.extend(_spans(glossary.en_pattern_for(neighbour.zh), haystack))

        own = [
            span
            for span in _spans(glossary.en_pattern_for(entry.zh), haystack)
            if not _swallowed(span, neighbour_spans)
        ]
        if own:
            verdicts.append(
                TermVerdict(
                    zh=entry.zh,
                    expected_en=entry.en,
                    verdict=Verdict.HIT,
                    found=own[0][2],
                )
            )
            continue

        # Report the longest confusable that was written instead — the most
        # informative thing to put in front of the model on a re-translation.
        confusion = max(neighbour_spans, key=lambda s: s[1] - s[0], default=None)
        verdicts.append(
            TermVerdict(
                zh=entry.zh,
                expected_en=entry.en,
                verdict=Verdict.WRONG if confusion else Verdict.MISS,
                found=confusion[2] if confusion else None,
            )
        )

    return verdicts


def hit_rate(verdicts: list[TermVerdict]) -> float:
    """HIT / total. `1.0` for an empty list — nothing to get wrong."""
    if not verdicts:
        return 1.0
    hits = sum(1 for verdict in verdicts if verdict.verdict is Verdict.HIT)
    return hits / len(verdicts)


def missed_terms(verdicts: list[TermVerdict]) -> list[str]:
    """Canonical terms that were not used correctly, in order."""
    return [v.zh for v in verdicts if v.verdict is not Verdict.HIT]
