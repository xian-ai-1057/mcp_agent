"""Text normalisation and English surface-form expansion.

Every English comparison in the system runs through `normalize_en` on *both*
sides, so casing, hyphenation and whitespace never decide a verdict. See
`specs/001-glossary-core/spec.md` §3.
"""

import re
import unicodedata

# Anything that separates words in English but shouldn't distinguish two
# spellings of the same term: ASCII hyphen, the dash family, underscore,
# non-breaking and narrow spaces.
_SEPARATORS = re.compile(r"[-‐-―_   ]+")
_WHITESPACE = re.compile(r"\s+")
_TRAILING_ACRONYM = re.compile(r"\s*\(([^)]{1,40})\)\s*$")
_ASCII_WORD = re.compile(r"[a-z0-9]+")


def normalize_zh(text: str) -> str:
    """NFKC only.

    No case folding (Chinese has none) and no punctuation stripping: the
    character offsets in `TermMatch` are promised against this string, and
    deleting characters would silently shift them.
    """
    return unicodedata.normalize("NFKC", text)


def normalize_en(text: str) -> str:
    """Fold an English string to its comparison form."""
    folded = unicodedata.normalize("NFKC", text).lower()
    folded = _SEPARATORS.sub(" ", folded)
    return _WHITESPACE.sub(" ", folded).strip()


def _pluralize(phrase: str) -> str | None:
    """Naive English plural of the final word. Returns None if not applicable."""
    words = phrase.split()
    if not words:
        return None
    head, last = words[:-1], words[-1]
    if not _ASCII_WORD.fullmatch(last):
        return None
    if last.endswith(("s", "x", "z")) or last.endswith(("ch", "sh")):
        plural = last + "es"
    elif len(last) > 1 and last.endswith("y") and last[-2] not in "aeiou":
        plural = last[:-1] + "ies"
    else:
        plural = last + "s"
    return " ".join([*head, plural])


def expand_forms(en: str) -> list[str]:
    """Every surface form that counts as having used `en`, longest first.

    `Know Your Customer (KYC)` expands to the full string, `know your customer`,
    `kyc`, and the plural of the base phrase — a translation that writes only
    "KYC" used the term, and so did one that wrote "Know Your Customers".
    """
    raw = en.strip()
    if not raw:
        return []

    bases = [raw]
    acronym = _TRAILING_ACRONYM.search(raw)
    if acronym:
        stripped = _TRAILING_ACRONYM.sub("", raw).strip()
        if stripped:
            bases.append(stripped)
        bases.append(acronym.group(1).strip())

    forms: list[str] = []
    for base in bases:
        normalized = normalize_en(base)
        if not normalized:
            continue
        forms.append(normalized)
        plural = _pluralize(normalized)
        if plural:
            forms.append(plural)

    unique = list(dict.fromkeys(form for form in forms if len(form) >= 2))
    unique.sort(key=lambda form: (-len(form), form))
    return unique


def compile_pattern(forms: list[str]) -> re.Pattern[str]:
    """Alternation over `forms`, longest first, with alphanumeric boundaries.

    `(?<![a-z0-9])` / `(?![a-z0-9])` rather than `\\b`, so `limit` does not fire
    inside `limitation` while forms that begin or end with punctuation — `(aml)`
    — still match.
    """
    if not forms:
        return re.compile(r"(?!x)x")  # never matches
    ordered = sorted(forms, key=lambda form: (-len(form), form))
    body = "|".join(re.escape(form) for form in ordered)
    return re.compile(rf"(?<![a-z0-9])(?:{body})(?![a-z0-9])", re.IGNORECASE)


def find_form(text: str, pattern: re.Pattern[str]) -> str | None:
    """First surface form of `pattern` present in `text`, already normalised."""
    match = pattern.search(normalize_en(text))
    return match.group(0) if match else None
