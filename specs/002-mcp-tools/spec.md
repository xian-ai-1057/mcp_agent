# Spec 002 · `mcp-tools`

**Layer:** tool + MCP interface
**Owns:** `tools/`, `server.py`
**Depends on:** `contracts/`, `glossary/`, `agent/prompts.py` (formatting only)
**Must not import:** `agent/gateway.py`, `agent/loop.py`, `agent/cli.py`

> Tools do not know a gateway exists. A tool takes a dict, returns a dict, and
> has no opinion about who called it or what happens next.

---

## 1. The pluggability constraint

The product is a general-purpose agent whose tool set keeps growing. That makes
one property more important than any individual tool:

**Adding a tool must be adding a file. Nothing else.**

Concretely — no edit to `server.py`, no edit to the system prompt, no entry in a
registry list, no import added anywhere. This is enforced by acceptance
criterion **6**, which drops a new module into `tools/`, restarts the server,
asserts the tool is advertised, and asserts the SHA-256 of `server.py` and
`agent/prompts.py` did not change.

A design claim that cannot be tested is a slogan. This one is tested.

---

## 2. The tool contract — `tools/base.py`

Every tool module exposes exactly one module-level attribute named `SPEC`:

```python
SPEC = ToolSpec(
    name="lookup_terms",
    description="...",          # written for the model, not for humans
    input_schema={...},         # JSON Schema, object at the root
    handler=_run,               # (dict) -> dict, JSON-serialisable
)
```

The three things that travel with the tool — **name, description, schema** — live
next to its implementation. That is what lets `server.py` stay ignorant.

**Rules.**
- `name`: `^[a-z][a-z0-9_]*$`. Unique; a collision fails discovery loudly at
  startup rather than shadowing silently.
- `description`: must state *when to call it*, not just what it does. This is
  the only routing signal the model gets (§4).
- `input_schema`: root `type: "object"`, `additionalProperties: false`, every
  property described. The MCP SDK validates arguments against it before the
  handler runs, so handlers may trust their input's shape.
- `handler`: synchronous, pure with respect to process state, returns a dict.
  Raise `ToolError` for expected failures (bad city name); anything else is a
  bug and is allowed to propagate.

### Side effects
Every tool in this phase is read-only: look up terms, judge a translation, read
the clock, read a weather API, format a greeting. **No confirmation mechanism
exists at the server layer, deliberately.** When a genuinely mutating tool is
proposed, that is the moment to design consent — designing it now would mean
designing against an imagined tool.

---

## 3. Discovery — `tools/registry.py`

`discover()` walks `tools/` with `pkgutil.iter_modules`, imports every module
that is not private (`_`-prefixed) and not infrastructure (`base`, `registry`),
and collects `SPEC`. A module without `SPEC` is skipped with a warning — a
helper module in the package is not an error.

`server.py` calls `discover()` and nothing else. It contains no tool names.

---

## 4. Tool descriptions carry the routing burden

The system prompt does not enumerate tools (spec 003 §3). Therefore the
description field *is* the routing policy, and it is written to disambiguate
against its siblings, not in isolation:

- `get_time` says "the current date or time" **and** "not for translating the
  word 時間" — because `服務時間` is a glossary term and the two intents collide.
- `lookup_terms` says "call this before translating any Chinese text" — the
  offline data shows an un-assisted translation scores ~43–50%, so the cost of a
  needless call is far below the cost of a skipped one.

Tool-selection accuracy is measured (criteria 5 and 11). When a new tool lands
and accuracy drops, the fix is a description edit — a local change to one file,
which is the point of keeping the prompt generic.

---

## 5. Tools in this phase

### 5.1 `lookup_terms` — `tools/translate_lookup.py`
`{ text: string }` → `LookupResult` (`matches[]`, `glossary_block`, `count`).

Scans `text` with `glossary.scanner`, formats the block with
`agent.prompts.format_glossary_block`.

> **Why a tool imports from `agent/`.** `agent/prompts.py` is a pure text module:
> it imports `contracts` and nothing else — no gateway, no HTTP, no loop. Keeping
> the block's formatting there means the prompt-side and tool-side renderings of
> the glossary cannot drift, and re-formatting the block is not a tool-schema
> change. The dependency is on a leaf module, and is one-directional.

`count` is the number of *occurrences* (`len(matches)`); `glossary_block`
de-duplicates by canonical term, first-occurrence order.

### 5.2 `verify_translation` — `tools/translate_verify.py`
`{ source_text: string, translation: string }` → `VerifyResult`.

Re-scans the source, judges the translation via `glossary.matcher`. Stateless:
it reports, it does not decide. Whether a miss is worth another attempt is the
agent loop's call (spec 003 §5), which is what keeps this tool independently
testable.

### 5.3 `get_time` — `tools/get_time.py`
`{ timezone?: string }` → `{ iso, date, time, timezone, weekday }`. IANA name,
default `Asia/Taipei`. Unknown zone → `ToolError`.

Present from Phase 1 by design: a second tool is the minimum needed to observe
whether the model *chooses*, and its implementation cost is near zero. Without
it, the demo would be a translation pipeline wearing an agent costume.

### 5.4 `say_hello` — `tools/say_hello.py`
`{ name: string, language?: "zh"|"en" }` → `{ greeting, language }`.
Parameters, no side effects, no external dependency.

### 5.5 `get_weather` — `tools/get_weather.py`
`{ city: string }` → `{ city, temperature_c, condition, source, observed_at }`.

Provider selected by `WEATHER_PROVIDER`:
- `stub` (**default**) — deterministic canned reading, no network.
- `open-meteo` — live call, no API key required.

The plan lists the weather data source as an open question. Defaulting to `stub`
means the tool ships, the registry gains a third shape (external dependency),
and no outbound call happens until someone opts in. When the sourcing decision
lands, it is a provider function, not a redesign.

**Together these five cover the test matrix the plan asks for:** no arguments
(`get_time`), arguments (`say_hello`), external dependency (`get_weather`),
plus the two glossary tools.

---

## 6. `server.py`

stdio MCP server, `Server("mcp-agent-tools")`.

- `list_tools` → `discover()` mapped to `types.Tool`.
- `call_tool` → look up `SPEC`, run `handler`, return the dict as structured
  content. `ToolError` → `isError` result carrying the message. Unexpected
  exceptions are logged with a traceback and returned as an error result, so one
  broken tool never takes the session down.
- Logging goes to **stderr only**. stdout is the JSON-RPC channel; a stray
  `print` corrupts the protocol.

`server.py` contains no business logic and no tool names. It is expected to stay
untouched for the life of the project.

---

## 7. Acceptance

`tests/test_tools.py`, `tests/test_registry.py`, `tests/test_server_smoke.py`,
and acceptance criteria **1**, **6**, **7**.
