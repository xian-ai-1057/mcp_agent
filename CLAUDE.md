# CLAUDE.md

Working notes for agents editing this repository. `README.md` is the user-facing
document; the specs under `specs/` are authoritative for behaviour.

## Layering — the rule that matters most

```
glossary/  knows nothing about MCP
tools/     knows nothing about the gateway
agent/     knows nothing about how terms are compared
```

If a change requires one layer to learn another's internals, the split is wrong —
fix the split, not the symptom.

**The one sanctioned exception:** `tools/translate_lookup.py` imports
`format_glossary_block` from `agent/prompts.py`. This is allowed *only* because
`prompts.py` is a pure text module. It may import `contracts` and nothing else,
and `tests/test_prompts.py::TestPromptsModuleIsPure` enforces that with an AST
check. Do not add an import to `prompts.py` to make something convenient.

## Things that will bite you

- **`server.py` and `agent/prompts.py` must not change when you add a tool.**
  Acceptance criterion 6 hashes both files. If your change to either is genuinely
  required, you have broken the pluggability guarantee — say so explicitly rather
  than updating the test.
- **The system prompt must not name a tool.** Same reason. The tool list travels
  in the API's `tools` field.
- **`glossary/matcher.py` is the single verdict implementation.** Offline
  evaluation and the online tool share it. Never copy this logic; a divergence
  between the two is invisible until it reaches users.
- **stdout is the MCP JSON-RPC channel.** Anything in `server.py` or `tools/`
  that writes to stdout corrupts the protocol. Log to stderr.
- **Scanner order is the algorithm.** `Glossary.scan_pattern` is an alternation
  sorted longest-surface-first; that sort is what produces longest-match-first
  semantics. Do not "tidy" it into alphabetical order.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q          # everything, ~7s
.venv/bin/python -m pytest tests/acceptance/  # the 12 criteria
```

- Async MCP sessions use `tests/mcp_session.py`, **not** a pytest fixture.
  pytest-asyncio finalises async fixtures on a different task, and anyio's cancel
  scopes refuse to be exited from another task. Enter the session inside the test
  body.
- Tests that need a model are marked `requires_gateway` and skip without
  `GATEWAY_BASE_URL`. **Do not make them pass by faking a model.** A simulated
  routing score is a number that looks like evidence and isn't.
- `agent/testing.py` doubles prove the *loop* works. They cannot answer questions
  about model behaviour; that is what `evals/` is for.

## Adding a tool

One file in `tools/` exporting a `SPEC`. Nothing else. Write the `description`
to say *when to call it*, and to disambiguate against its siblings — the system
prompt is generic, so the description carries the entire routing burden.

## Open items

- `data/glossary.csv` is a 71-term sample; the production asset is 379 terms and
  is not in this repository. Swapping it needs no code change.
- The plan referenced an existing `term_eval.py` to reuse; it is not in this
  repository. Its functions were reimplemented in `glossary/`. If that file is
  ever merged in, make it import from `glossary.matcher` rather than keeping a
  second copy.
- `get_weather`'s data source is undecided, so it defaults to an offline stub.
  Adding the real source is a new function in `PROVIDERS`.
