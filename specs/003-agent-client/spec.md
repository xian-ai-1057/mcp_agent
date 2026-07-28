# Spec 003 · `agent-client`

**Layer:** client
**Owns:** `agent/`
**Depends on:** `contracts/`, the MCP server as a subprocess
**Must not import:** `glossary/`, `tools/`

> The agent does not know how terms are compared. It sees tool names, JSON
> schemas and JSON results. If anything in `agent/` needs to reason about the
> glossary's internals, the split is wrong.

One deliberate exception, stated in spec 002 §5.1: `agent/prompts.py` is a pure
text module and `tools/translate_lookup.py` imports one formatting function from
it. `prompts.py` therefore imports `contracts` and nothing else, forever.

---

## 1. Modules

| Module | Responsibility | Not responsible for |
|---|---|---|
| `config.py` | Load `.env` into the environment, at entry points only | Deciding what any variable means |
| `gateway.py` | OpenAI-compatible chat completions over HTTP | Tool semantics |
| `bridge.py` | MCP tool schema ↔ OpenAI `tools` format | Business meaning |
| `mcp_client.py` | Spawn `server.py` over stdio, list/call tools | Choosing tools |
| `prompts.py` | System prompt, user template, glossary block | Calling the model |
| `loop.py` | Multi-turn orchestration, turn cap, self-check policy | What a tool does internally |
| `metrics.py` | Aggregate run statistics into a report | Judging translations |
| `testing.py` | Deterministic gateway doubles | Production behaviour |
| `cli.py` | Argument parsing, output rendering | Everything above |

---

## 2. `gateway.py`

`Gateway` is a Protocol with one method:

```python
async def complete(messages, tools=None, tool_choice="auto") -> AssistantTurn
```

`AssistantTurn` = `{ content: str | None, tool_calls: list[ToolCall] }`.

`HTTPGateway` POSTs to `{GATEWAY_BASE_URL}/chat/completions` with
`{model, messages, tools, tool_choice}` and reads `choices[0].message`.

**Compatibility hazards, handled explicitly.** The plan flags gateway format
drift as the top integration risk, so the response reader tolerates:
- `tool_calls` absent, `null`, or `[]` — all mean "no tool call";
- `function.arguments` as a JSON string (per spec) *or* as an already-decoded
  object (several gateways do this);
- `arguments` being `""` or malformed — recorded as a failed call with the raw
  string preserved, never an exception that kills the run;
- a legacy single `function_call` field instead of `tool_calls`.

Every one of these has a unit test with a captured payload shape. Discovering a
gateway quirk means adding a fixture, not editing the loop.

---

## 3. `prompts.py`

### `SYSTEM_PROMPT`
Written as general policy. **It contains no tool names and no tool count** — the
list arrives in the API's `tools` field, which is where the model actually reads
it from. Repeating it in prose creates a second copy that goes stale the moment
a tool is added, and would break acceptance criterion 6.

It states, generically:
1. Prefer calling a tool over answering from memory when a tool covers the need.
2. For any Chinese→English translation, look up terminology **first**; the
   glossary is authoritative and overrides your own preference.
3. Use the returned English exactly. Do not paraphrase, re-order or "improve" it.
4. Return the translation alone — no commentary, no romanisation, no notes.

Rule 2's bluntness is bought with data: without glossary assistance the two
measured models scored 42.7% and 49.7%; with it, 98.1% and 99.0%.

### `format_glossary_block(matches) -> str`
`- 臨時額度 → temporary credit limit` per line, de-duplicated by canonical term,
first-occurrence order, empty string for no matches. The one renderer, shared by
the tool and the prompt.

### `retranslate_prompt(source, translation, verify) -> str`
Names each missed term with its required English and asks for a corrected
translation only. Includes the previous attempt so the model repairs rather than
restarts.

---

## 4. `bridge.py`

`mcp_tool_to_openai(tool)` → `{"type": "function", "function": {name,
description, parameters}}`.

Total, mechanical, and unit-tested against the real `list_tools` output — the
one place where "the gateway wants a slightly different shape" gets absorbed.

---

## 5. `loop.py`

### 5.1 The generic loop
```
messages = [system, user]
for turn in 1..max_turns:
    turn_result = gateway.complete(messages, tools)
    if turn_result.tool_calls:
        execute each, append one tool message per call
        continue
    output = turn_result.content
    break
else:
    stop_reason = MAX_TURNS
```

- `max_turns` (default 6) bounds the whole run. Exhausting it is a normal,
  recorded outcome — the last assistant text is still returned.
- A tool that raises is reported back to the model as an error message, not
  crashed on. The model gets one chance to recover; the cap stops the loop from
  ping-ponging.
- **A model that calls no tool is not an error.** The run completes, and
  `metrics.called_any_tool` is `False`. Criterion 4 exists because the plan
  refuses to assume tool use, and an assumption you refuse to make is one you
  have to measure.

### 5.2 The self-check policy — why it lives here, not in the tool
`verify_translation` reports. `loop.py` decides. Keeping the decision out of the
tool is what lets the tool be stateless and tested on its own, and lets the
retry policy change without touching an MCP schema.

The policy triggers on **observed tool use, not on the prompt**: if
`lookup_terms` was called during the run, the run was a translation, and the
self-check applies. No keyword sniffing of the user's text, so the agent stays
general — a future tool gets its own policy or none.

```
while hit_rate < 1.0 and retranslations < max_retranslate:
    ask the model to fix the named terms
    re-verify
```

`max_retranslate` (default 2) is a separate budget from `max_turns`. Criterion 9
asserts termination when the model never improves; without the cap, a model that
keeps producing the same output loops forever.

The final `VerifyResult` is attached to `RunResult.verify` whether or not it
improved — a run that failed to reach 100% must say so, not hide it.

---

## 6. `metrics.py`

Aggregates runs into the report criterion 12 requires:

| Metric | Definition |
|---|---|
| `tool_call_rate` | runs with ≥1 tool call / total runs |
| `tool_selection_accuracy` | runs whose first tool call matched the expected tool / runs with an expectation |
| `glossary_hit_rate` | HIT terms / total terms, across runs |
| `mean_turns`, `retranslation_rate`, `max_turns_hit_rate` | loop health |

`tool_call_rate` is a headline number, not a diagnostic: it is the single
statistic that separates a 98% system from a 43% one.

---

## 7. `testing.py` — deterministic doubles

Acceptance criteria must be verifiable in CI, where no gateway exists.

- **`ScriptedGateway`** replays a fixed list of `AssistantTurn`s. Used for loop
  mechanics: turn caps, tool errors, malformed arguments, no-tool runs.
- **`RuleBasedGateway`** simulates a competent model: routes on the request,
  calls the matching tool, and builds its answer from the tool's result. With
  `glossary_fidelity < 1.0` it drops terms on purpose, which is how the
  re-translation criteria get a failure to repair.

These are doubles for *loop* behaviour. They cannot answer "does the real model
choose the right tool" — that is `evals/`, which requires a configured gateway
and is skipped, never faked, when one is absent.

---

## 8. `cli.py`

```
mcp-agent "請幫我翻譯：客戶申請提高臨時額度"
mcp-agent --interactive
mcp-agent --json --gateway fake "現在幾點"
```

`--gateway fake` runs the rule-based double end to end through the real MCP
server, which is what makes the demo runnable with no gateway credentials.
`--json` emits the whole `RunResult` for scripted checks.

### 8.1 Configuration loading

`cli.py` calls `config.load_env_file()` before anything reads `os.environ`, and
`evals/run_eval.py` does the same. `--env-file` points elsewhere.

Three rules, each with a reason:

1. **Entry points only, never at import time.** A module that loaded `.env` on
   import would leak a developer's gateway config into every test run, and the
   `requires_gateway` tests would stop skipping and start making real network
   calls. Asserted by `tests/test_config.py`.
2. **An exported variable wins over the file.** Explicit beats ambient.
3. **An exported-but-*empty* variable does not win.** `HTTPGateway.configured()`
   is `bool(os.environ.get(...))`, so the rest of the system already reads empty
   as unset; a shell that exports `GATEWAY_BASE_URL=` must not shadow a good
   `.env` and reproduce "it is set but the program says it isn't".

**A missing configuration must say why it is missing.** The original error —
`GATEWAY_BASE_URL is not set` printed while a filled-in `.env` sat in the
repository root — gave no way to discover that nothing ever read the file. The
message now names the file it read, or the path it looked for and did not find.

`tests/test_bridge.py`, `test_prompts.py`, `test_gateway.py`, `test_loop.py`,
and acceptance criteria **2, 3, 4, 5, 8, 9, 11, 12**.
