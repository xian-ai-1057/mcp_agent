# `data/` — glossary asset

`glossary.csv` is the read-only knowledge source for `lookup_terms` and
`verify_translation`.

## Status of this file

**This is a 71-term representative sample, not the production asset.** The plan
targets the existing 379-term / 10-category glossary; that CSV was not present
in this repository, so this sample was written to the same schema and to the
same 10 categories, and it deliberately contains every overlap case the plan
calls out:

| Overlap family | Why it is here |
|---|---|
| `額度` / `臨時額度` / `永久額度` | Longest-match-first must suppress `額度` |
| `帳戶` / `警示帳戶` / `警示帳戶通報機制` / `外幣帳戶` / `約定轉入帳戶` | Three-deep nesting, plus unrelated superstrings |
| `利率` / `牌告利率` | Two-character term inside a four-character one |
| `外匯` / `遠期外匯` | Suffix overlap rather than prefix overlap |

Swapping in the production file requires **no code change** — drop it at
`data/glossary.csv` (or point `GLOSSARY_CSV` elsewhere). The loader picks the
new content up on the next tool call, without a server restart.

## Schema

| Column | Required | Notes |
|---|---|---|
| `zh` | yes | Canonical Chinese term. Must be unique across the file. |
| `en` | yes | Target English translation. |
| `aliases` | no | Extra Chinese surface forms, `|`-separated. Scanned for, but reported under the canonical `zh`. |
| `category` | yes | One of the 10 business categories. |

A parenthesised acronym in `en` — `Anti-Money Laundering (AML)` — is expanded by
the matcher into three accepted surface forms: the full phrase, the phrase
without the parenthetical, and the acronym alone. So a translation that says
just "AML" still counts as a HIT.

## Editing

The file is a read-only asset from the application's point of view: nothing in
this repository writes to it. Edit it with any CSV tool; the loader compares
`(mtime_ns, size)` on every access and reloads when either changes.
