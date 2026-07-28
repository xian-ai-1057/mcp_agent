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

**The sanctioned pure-renderer sharing:** `tools/translate_lookup.py` and the
translation adapters import from `capabilities/translation/prompts.py`.
`agent/prompts.py` separately owns the generic system prompt. Both are pure text
modules: they may import `contracts` and nothing with runtime side effects, and
`tests/test_prompts.py` enforces that with AST checks.

## Things that will bite you

- **`server.py` and `agent/prompts.py` must not change when you add a legacy root-server
  tool.** Acceptance criterion 6 hashes both files. Production capabilities use an
  explicit split-server registry and MCP config instead; they are not automatically
  discovered from `tools/`.
- **The system prompt must not name a tool.** Same reason. The tool list travels
  in the API's `tools` field.
- **`glossary/matcher.py` is the single verdict implementation.** Offline
  evaluation and the online tool share it. Never copy this logic; a divergence
  between the two is invisible until it reaches users.
- **stdout is the MCP JSON-RPC channel.** Anything in `server.py` or `tools/`
  that writes to stdout corrupts the protocol. Log to stderr.
- **Every served HTML page gets its own CSP hash, and may contain exactly one
  inline `<style>` and one inline `<script>`.** `_inline_content_hash` matches
  the *first* block of a tag, so a second `<style>`/`<script>` is silently
  CSP-blocked at runtime — tests pass, the page breaks in the browser. A tag
  with an attribute (`<script defer>`) misses the regex the same way. Editing a
  page's inline content needs no Python change; the hash is recomputed from the
  file at startup. `tests/test_web.py` guards both the one-block rule and that
  `/` and `/flow` do not share a policy.
- **`.env` is loaded at direct entry points only** — `agent/cli.py`, the
  `python -m agent.web` path, and `evals/run_eval.py` call
  `agent.config.load_env_file`. An ASGI import never loads it; Uvicorn must receive
  `--env-file`. Never load env at module import time: local gateway config would
  leak into tests and trigger real network calls. `tests/test_config.py` asserts
  that imports add nothing to `os.environ`.
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

## Adding a tool or capability

For a production capability, create a dedicated MCP server with an explicit
`ToolSpec` registry via `mcp_servers.common`, then add it to default config only
when it is safe as a default; otherwise document an MCP JSON config. For the
legacy 0.3.x root server only, one file in `tools/` exporting `SPEC` is enough.

In both cases write the `description` to say *when to call it*, what its side
effects are, and how it differs from siblings. The system prompt is generic, so
the description carries the model-routing burden.

Then check `docs/architecture.md`. `tests/test_docs.py` automatically compares
legacy-discovered tool names and also guards the current production boundary
names, but it cannot discover every future explicit registry. When adding a
production server, update both the diagrams and their drift assertions.

## Diagrams

`docs/architecture.md` is the canonical diagram set; `README.md` carries only a
compact text overview. Two things to know:

- **Mermaid keywords cannot be participant aliases.** `participant Loop` parses
  in some tools and then fails to render on GitHub — `loop`, `alt`, `opt`, `par`
  and `end` are all reserved. `tests/test_docs.py` checks this.
- **Render when the local Node/Chromium toolchain is available.** The docs tests
  only guard fences, diagram declarations and a few Mermaid hazards; they are not
  a renderer. Optional local rendering:

  ```bash
  npm install @mermaid-js/mermaid-cli
  echo '{"executablePath":"/opt/pw-browsers/chromium","args":["--no-sandbox"]}' > pptr.json
  npx mmdc -p pptr.json -i diagram.mmd -o out.png -b white
  ```

## Open items

- `data/glossary.csv` is a 71-term sample; the production asset is not in this
  repository. Production runtime quarantines conflicting duplicate surfaces so
  unrelated lookups remain available; strict loaders still reject duplicates.
  The `aliases` column means Chinese source aliases, not English alternatives.
- The plan referenced an existing `term_eval.py` to reuse; it is not in this
  repository. Its functions were reimplemented in `glossary/`. If that file is
  ever merged in, make it import from `glossary.matcher` rather than keeping a
  second copy.
- `get_weather`'s data source is undecided, so it defaults to an offline stub.
  Adding the real source is a new function in `PROVIDERS`.
