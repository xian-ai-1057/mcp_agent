# Spec 001 · `glossary-core`

**Layer:** knowledge
**Owns:** `glossary/`
**Depends on:** `contracts/glossary.py`, `contracts/tools.py`
**Must not import:** `tools/`, `server.py`, `agent/`

> The glossary layer does not know MCP exists. It takes strings in and returns
> structured findings. Anything in this package that mentions a tool name, a
> JSON-RPC frame, or a prompt is a layering bug.

---

## 1. Why this layer is the core asset

The offline experiment settled the question of whether models can follow a
glossary: with terms injected, the single-word tier scored **100.0%** on both
models. What the experiment did *not* settle is whether the right terms get
found in the first place — that is this package's job, and it is where the
remaining 1–3% of failures live.

So the design bias throughout is: **recall of the scanner beats cleverness
anywhere else.**

---

## 2. Modules

| Module | Responsibility | Explicitly not responsible for |
|---|---|---|
| `normalize.py` | Unicode/case/whitespace normalisation, English surface-form expansion, pattern compilation | Deciding what to look for |
| `loader.py` | Read the CSV, expand aliases, pre-compile patterns, reload on change | Scanning, judging |
| `scanner.py` | Find glossary terms in a Chinese sentence, longest-match-first | Formatting, translating |
| `matcher.py` | Judge whether a translation used the required English | Deciding whether to re-translate |
| `runtime.py` | Process-wide loader instance, configured from `GLOSSARY_CSV` | Any logic of its own |

---

## 3. `normalize.py`

### 3.1 `normalize_zh(text) -> str`
NFKC-normalise. Nothing else — Chinese has no case and no word boundaries, and
stripping punctuation would shift the character offsets that `TermMatch`
promises.

### 3.2 `normalize_en(text) -> str`
NFKC → lowercase → hyphens/en-dashes/underscores to space → collapse runs of
whitespace → strip. Applied to *both* sides of every English comparison, so
`Anti-Money Laundering`, `anti money laundering` and `ANTI‑MONEY  LAUNDERING`
are one string by the time they meet.

### 3.3 `expand_forms(en) -> list[str]`
Produces every surface form that counts as having used the term. Order is
longest-first, de-duplicated.

1. **Parenthetical acronym.** `Know Your Customer (KYC)` yields the full string,
   `know your customer`, and `kyc`. A translation that writes only "KYC" used
   the term.
2. **Plural of the final word.** `credit limit` → `credit limits`;
   `facility` → `facilities`; `stress test` → `stress tests`;
   words ending in `s/x/z/ch/sh` take `es`.
3. Forms shorter than 2 characters are dropped (guards against a stray acronym
   like "(A)").

### 3.4 `compile_pattern(forms) -> re.Pattern`
Alternation over the escaped forms, longest-first, `IGNORECASE`. Boundaries are
`(?<![a-z0-9])` / `(?![a-z0-9])` rather than `\b`, so `limit` does not match
inside `limitation` while `AML)` and `(AML` still match.

---

## 4. `loader.py`

### 4.1 CSV contract
Columns `zh,en,aliases,category`. `aliases` is `|`-separated and may be blank.
Aliases are additional Chinese source surfaces, not alternative English
translations. Blank lines, a leading BOM, and unrelated extra columns are
tolerated; extra columns do not change matcher semantics.

The public `load_glossary()` and `GlossaryLoader` APIs default to
`conflict_policy="error"`. Strict mode rejects at load time, with the offending
row number in the message:
- a missing required column
- a blank `zh`, `en` or `category`
- a duplicate `zh`
- an alias that collides with another entry's alias

An alias that collides with some entry's canonical `zh` is **not** an error: the
canonical form wins and the alias is dropped, because a canonical term is always
the more specific claim.

The process-wide production runtime explicitly selects
`conflict_policy="quarantine"`. In this mode:

- duplicate `zh` rows with the same `normalize_en(en)` are collapsed, aliases
  are de-duplicated, and the first row remains the reporting row;
- duplicate `zh` rows with different English are excluded from authoritative
  indexes rather than resolved by source order;
- an alias claimed by multiple canonical entries is excluded while both
  canonical entries remain usable;
- quarantined surfaces remain in the longest-first scan pattern. Selecting one
  raises `GlossaryConflictError` with source line numbers, so the tools return a
  recoverable error instead of silently choosing a translation;
- a valid longer surface still suppresses a nested quarantined shorter surface.

This policy is term-scoped availability, not conflict resolution. A text-only
lookup has no safe way to choose category-specific English for the same Chinese
surface.

### 4.2 `Glossary` (immutable value object)
Built once per load and shared freely:

| Field | Purpose |
|---|---|
| `entries` | Source order, for reporting |
| `by_zh` | Canonical lookup |
| `surface_to_entry` | `zh` and every alias → entry |
| `conflicts` | quarantined surface → source/reason/line metadata |
| `scan_pattern` | Single alternation over authoritative and quarantined surfaces, **sorted longest-first** |
| `en_pattern_for(zh)` | Lazily compiled per-entry English matcher |

### 4.3 Reload policy — `mtime`, not hot-push
`GlossaryLoader.get()` stats the CSV and compares `(st_mtime_ns, st_size)`
against the stamp of the loaded copy. Different ⇒ reload before returning.

- **Why stat-on-access rather than load-at-startup:** the asset is updated on no
  fixed schedule. A long-running server that loaded once would serve stale
  translations indefinitely, and — the real hazard — silently.
- **Why not a push/webhook mechanism:** 71 rows today, 379 in production. A
  reload is sub-millisecond. Any invalidation protocol would cost more to
  operate than the thing it saves.
- **Why size as well as mtime:** one-second `mtime` granularity on some
  filesystems can hide an edit made within the same second as the previous load.
- **Failure mode:** if structural validation raises (someone saved a broken
  CSV), the
  previously loaded `Glossary` is retained, the error is logged once per stamp,
  and `get()` still returns a working glossary. A bad edit degrades to "stale",
  never to "down".
- **Quarantine reload:** duplicate conflicts are a usable new snapshot in
  production. Non-conflicting edits become visible immediately while only the
  ambiguous surfaces are isolated; the old translation for a newly conflicted
  term is not kept authoritative.
- Guarded by an `RLock`; concurrent readers see either the old or the new
  `Glossary`, never a half-built one.

---

## 5. `scanner.py` — longest-match-first

```
scan(text, glossary) -> list[TermMatch]
```

Single left-to-right pass with `Glossary.scan_pattern`. Because the alternation
is ordered longest-first, at any position the longest surface wins; because
`re.finditer` resumes *after* the match, a shorter term nested inside an
accepted span is never re-examined.

If the selected surface is quarantined, scanning raises
`GlossaryConflictError`; it never falls through to a nested term or an arbitrary
duplicate row. Translation tools convert this to `ToolError`, which MCP reports
as an expected invocation failure.

That is the whole algorithm, and it gives the property the acceptance criteria
demand:

| Input | Accepted | Suppressed |
|---|---|---|
| `客戶申請提高臨時額度` | `臨時額度` | `額度` |
| `永久額度和臨時額度` | `永久額度`, `臨時額度` | `額度` ×2 |
| `警示帳戶通報機制` | `警示帳戶通報機制` | `警示帳戶`, `帳戶` |
| `外幣帳戶的牌告利率` | `外幣帳戶`, `牌告利率` | `帳戶`, `利率` |

**Rationale.** The glossary contains families like `額度 / 臨時額度 / 永久額度`
where the short term's translation is *wrong* for the long term, not merely less
precise. Injecting both hands the model two contradictory instructions about the
same span of text — measurably worse than injecting neither.

**Output rules.**
- Returned in text order.
- An alias hit reports the canonical `zh` and `en`; `start`/`end` still point at
  the alias as written.
- Repeat occurrences are all returned, each with its own span. De-duplication is
  the caller's choice, which is why offsets are in the contract.

---

## 6. `matcher.py` — the shared verdict

```
match_terms(translation, terms, glossary) -> list[TermVerdict]
hit_rate(verdicts) -> float
```

**This module is the single implementation of the HIT/WRONG/MISS judgement.**
Offline evaluation and the online `verify_translation` tool import the same
function. Duplicating this logic — even "just to avoid a dependency" — is
forbidden by spec, because the failure it produces (passing offline, failing
online) is invisible until it reaches users.

| Verdict | Condition |
|---|---|
| `HIT` | A form from `expand_forms(entry.en)` appears in the translation **on its own** (§6.1) |
| `WRONG` | Not a HIT, **and** the English of an overlapping entry appears instead |
| `MISS` | Neither |

"Overlapping entry" means an entry whose `zh` is a proper substring of this
term's `zh`, or vice versa. This targets exactly the confusion the glossary
families create: rendering `臨時額度` as "credit limit" is a *wrong* term, not an
absent one, and the two deserve different diagnoses. `found` carries the surface
that was matched instead — the longest one, since that is the most informative
thing to hand back to the model on a re-translation.

### 6.1 Swallowed spans

A naive "is the string present" test is wrong in the one direction that matters
most. `額度` expands to `credit limit`, which is also a substring of
`temporary credit limit`. Translating `額度` as "temporary credit limit" would
score HIT, and the system would be blind to precisely the error this layer
exists to catch.

So a candidate span counts only if it is **not strictly contained in a longer
match of an overlapping entry**:

| Source term | Translation | Verdict |
|---|---|---|
| `臨時額度` | "…the temporary credit limit." | HIT |
| `臨時額度` | "…the credit limit." | WRONG (`credit limit`) |
| `額度` | "Insufficient temporary credit limit." | WRONG (`temporary credit limit`) |
| `額度` | "There are limitations…" | MISS |

Both directions of the nesting are therefore judged correctly, and the rule is
symmetric: it never needs to know which of the two terms is "the real one".

`hit_rate` is `HIT / total`, and `1.0` for an empty term list — a sentence with
no glossary terms cannot fail this check.

---

## 7. Performance

379 terms × a typical sentence is one compiled-regex pass. No pre-optimisation:
`tests/test_scanner.py::test_scan_throughput` records the cost, and Aho-Corasick
gets considered only if that number becomes a problem. Measure, then decide.

---

## 8. Acceptance

Covered by `tests/test_normalize.py`, `test_loader.py`, `test_scanner.py`,
`test_matcher.py`, and acceptance criteria **1** (longest-first) and **7**
(mtime reload).
