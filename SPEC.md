---
module: kami-agent
version: 1.8
describes: v0.3.2
---

# kami-agent — Contract Registry

kami-agent turns a stateless model API into a persistent actor in the
Kamigotchi world: one loop, three provider adapters, sessions on disk.
This file is the contract, not a tour — every entry is a claim that can
be falsified against the code at the version named above. Narrative,
rationale, and setup live in `README.md` and `docs/`.

Sections: **Provides** (what other components may rely on), **Depends**
(what this module relies on, and who owns it), **Invariants** (claim ×
enforcement), **Deliberate deviations** (accepted behaviors that must
not be silently "fixed"), **Non-goals**, **Changelog**.

IDs are stable: cite `P4.2`, `I7`, `X3` from downstream code, analysis,
and reviews.

---

## Provides

### P1. Session lifecycle

`kami-agent run-session --run-dir DIR` executes at most one session and
exits. It returns exactly one outcome (printed on stdout, `runner.py`):
`lock_held` | `not_due` | `already_complete` | `run_complete` |
`session_aborted` | `session_ran`.

Ordered steps, as implemented:

1. **Acquire lock** (P4). Held by a live session → `lock_held`, nothing
   written.
2. **Fold telemetry** into run state (P3). No file is written yet.
3. **Wake gate.** `now < next_wake_at` → `not_due`, nothing written.
   `--manual` bypasses this gate and nothing else.
4. **Recover** (P3): an unmatched `session_start` gets a synthetic
   `session_end reason=crash`; the `state.json` cache is refreshed from
   the fold.
5. `run_status == complete` → `already_complete`.
6. **Boundary check** (P7.3). Tripped → `run_complete` event, run
   disabled, `run_complete`.
7. **Claim the session number**: `session_counter += 1`, persisted
   before any model call, so a crash never reuses a number.
8. **Spawn the harness child** and handshake (D1). Failure → a
   `session_start` / `session_end reason=errors` pair with zero model
   calls, a default-source `schedule_next`, and `session_aborted`.
9. **Emit `session_start`** carrying `tools_hash` of the loaded surface
   and, when the manifest pins one, the harness `presentation_mode`
   (D1).
10. **Build context**: frozen system prompt + `\n\n` + the file index
    (full `workspace/` tree with byte sizes, `reference/` collapsed to
    one `N files, N bytes, read-only` line).
11. **Kickoff**: the first user message is the frozen constant
    `prompts/kickoff.txt`. No dynamic content, no digits.
12. **Session-start brief** (P1.12, below): one harness party-report call
    for the account's own operator, injected as a normal tool result.
13. **Agent loop** (P2) until a stop reason (P5).
14. **Persist**: `session_end`, transcript file, state cache.
15. **Schedule** (P6): exactly one `schedule_next`.
16. **Release lock** → `session_ran`.

#### P1.12 Session-start status brief

Before the first model call — the same point at which the file index is
built into the system prompt (P1.10) — the scaffold calls the pinned
harness's **general any-operator party-report tool** once, for the
account's own operator, and injects the result into the session context.

- **The tool is `lens_party`** on the pinned surface (D1). Chosen because
  one call covers the whole contract in one result: every kami the
  account owns, each with its on-chain `state` and its calculated vitals
  — `hp` (current/total/percent), `hpRatePerHr` (from which projected HP
  follows), `cooldownSec`, and accrual. It is the general tool, not a
  brief-specific entry point: `account_index` selects any account, and
  the brief passes the tool's own sentinel for "the operator this harness
  runs as" (`-1`) explicitly rather than relying on its default.
- **No special path.** The same tool stays available for the agent to
  call itself, for any account, unchanged. Both invocations execute
  through one code path and are telemetered identically; only
  `tool_call.initiator` separates them (P9).
- **Injected as a normal tool result**: an assistant turn carrying the
  call, then its result — the shape the model would have seen had it made
  the call itself. The result is passed through **verbatim**: envelope
  untouched, nothing summarized, reordered, filtered, or annotated. The
  P2 byte cap is the only transformation, as it is for every tool result.
- **Degrade visibly, never block** (X21). Exactly one attempt. A failure
  is injected as the error result it is and the session continues; there
  is no retry, no fallback content, and no abort. The failure is data.
- The brief consumes no `session_tool_cap`, never advances the
  consecutive-error counter, and never feeds the repetition breaker
  (X20). It emits a `tool_call` event and so counts in
  `session_end.tool_calls`.
- It is **skipped entirely** when the loaded surface does not carry the
  tool, leaving no telemetry — an absent tool is already visible in
  `run_start.harness_tools` and in `tools_hash`.
- The brief sits in call-1 context, so it is part of the fixed floor D1's
  cap arithmetic is sized against, and its size is linear in the
  account's roster size. The smoke tier reports both numbers together.

### P2. Agent loop

- Alternates model calls and tool executions. The loop speaks only the
  canonical adapter types (P8) and never inspects provider payloads.
- **Parallel intents execute strictly sequentially in the order
  returned** — no reordering, no deduplication, no dependency analysis.
  Later intents observe the world produced by earlier ones, failures
  included. Serialization also removes same-wallet nonce contention at
  the scaffold layer.
- Each executed intent appends one `tool_result` message and emits one
  `tool_call` event.
- The session opens on the kickoff message followed by the session-start
  brief's call/result pair (P1.12); from there the alternation is the
  agent's. Error counting, the tool cap, and the repetition breaker all
  count agent-executed intents only.
- `end_session` takes effect immediately: every later intent in the same
  batch is skipped, emitted as `tool_call` with `ok=false, skipped=true`,
  and produces no tool-result message.
- An assistant turn with no tool calls (including `stop_reason:
  max_tokens` with no complete call) cannot advance the loop: the runner
  sends the frozen continuation string `prompts/continue.txt` and counts
  one error. The next `llm_call` carries `continuation: true`.
- Every tool result inserted into context is capped at
  `tool_result_max_bytes` (default 65536) with an explicit marker naming
  the original size; `workspace_read` results additionally name the
  byte-sliced re-read. Recorded as `truncated` / `original_bytes`.
- Error counting: any successfully executed tool call resets the
  consecutive-error counter; malformed calls (unknown tool, schema
  violation), failed executions, timeouts, and tool-less turns each
  count one. Reaching `max_consecutive_errors` (default 5) ends the
  session.
- Tool execution is bounded by `tool_timeout_s` (default 120) via a
  watchdog thread; a timeout is an error result, not a crash.
- Harness tool names that collide with scaffold tool names are rejected
  at loop construction (`ValueError`), before any model call.

### P3. Crash resume and accounting recovery

- `telemetry.jsonl` is the **source of truth** for all accounting.
  `state.json` is a cache, rebuilt by folding the stream on every run
  and never trusted from disk.
- The fold yields: `session_counter` (max session seen),
  `cumulative_usd` and `cumulative_tokens` (summed over every `llm_call`
  event), `first_session_at` (first `session_start.ts`), `next_wake_at`
  (last `schedule_next.next_wake_at`), `run_status` (`complete` once a
  `run_complete` event exists).
- A `session_start` with no matching `session_end` is a crashed session.
  Recovery writes a synthetic `session_end reason=crash` whose totals
  are folded from that session's events. Recovery is idempotent.
- A crashed session keeps its session number (the counter is persisted
  at P1.7, before the first model call).

### P4. Lockfile semantics

- One lock per run directory: `run/run.lock`, JSON `{pid, created}`.
- Held by a live process, younger than `lock_stale_s` (default 7200) →
  the invocation exits immediately with `lock_held`.
- A lock is **stale**, and is broken with a logged warning, when its PID
  is dead, its age exceeds `lock_stale_s`, or its content is unreadable.
  A crashed session can never deadlock a run.
- The lock is held for the whole invocation and released in a `finally`,
  including on the `not_due` and `lock_held` paths.

### P5. Session caps and stop reasons

Every session ends with exactly one `session_end.reason`. The enum is
closed and schema-enforced:

| reason | trigger |
|---|---|
| `agent` | the agent called `end_session` |
| `token_cap` | a call's `input_tokens + output_tokens ≥ session_token_cap`, checked **after** the call; that turn's intents are never executed |
| `tool_cap` | `session_tool_cap` (default 50) intents **executed** |
| `repetition` | a repetition-breaker rule tripped (P5.1) |
| `errors` | `max_consecutive_errors` reached, a non-retryable model error, or retries exhausted |
| `crash` | synthetic, written by recovery (P3) — never by a live loop |

All non-`agent` endings are **silent**: no warning message, no final
model call, no disclosure that caps or breaker rules exist. The agent
observes only that the session stopped.

#### P5.1 Repetition breaker

Three mechanical rules, evaluated in this order after every executed
tool call, on executed calls only (skipped intents never count). Knob
names are manifest-pinned (`caps:` block):

| rule | knob (default) | trip condition |
|---|---|---|
| `identical_call` | `repetition_identical_cap` (5) | the same signature executed that many times **consecutively**, success or error alike |
| `window_diversity` | `repetition_window` (30) / `repetition_min_distinct` (4) | over a **full** trailing window of that size, the number of distinct signatures is `≤ min_distinct` |
| `same_tool_errors` | `repetition_same_tool_error_cap` (8) | that many consecutive executed calls of the same tool (args may differ), all classified error-or-revert |

- A **signature** is `toolname:sha256(canonical_json(args))[:12]`;
  argument key order never distinguishes two calls.
- **error-or-revert** = a loop-level failure, or a success-shaped result
  whose JSON content carries `status: "reverted"` or a non-empty
  `error` field, at the top level or one `result` level down. Both
  halves are retained deliberately: **which half fires is a property of
  the pinned harness, not of this rule.** Against a harness that
  *returns* reverts in band they arrive success-shaped, are caught only
  by the content half, and never advance the consecutive-error counter —
  which is why this rule exists. Against one that *raises* them (D1)
  the same call is a loop-level failure, so it advances
  `max_consecutive_errors` too and a revert loop may end as `errors`
  before reaching `repetition_same_tool_error_cap`. The knobs are
  unchanged across that difference; the ending's `reason` is what
  moves, so analysis must not read `reason=repetition` counts as a
  harness-invariant measure of revert looping.
- The first rule to trip names the `session_end` telemetry fields (P9).

### P6. Wake scheduling and clamps

- `set_next_wake(minutes_from_now)` clamps to `[wake_min, wake_max]`
  (defaults 5 min – 24 h) and rejects NaN/Inf. **Last call in a session
  wins.**
- Exactly one `schedule_next` event per session, on every path
  (including harness-handshake abort) — wake-interval analysis has no
  holes. `source: agent` when a `set_next_wake` executed, else
  `default` at `wake_default` (60 min).
- **Carried wake.** When a session ends by `token_cap`, `tool_cap`, or
  `repetition` and the intents the cap prevented from executing contain
  a `set_next_wake`, that ONE intent (the last of them, normal
  last-call-wins) is executed at teardown — validated and clamped
  exactly as a normal call — and `schedule_next` carries
  `carried: true`.
  - Invalid args are discarded and recorded as `carried_invalid: true`;
    a previously executed wake stands, else `wake_default`.
  - No other skipped intent is ever executed. Intents skipped by
    `end_session` are never carried. `errors` endings never carry.
  - The carry is invisible to the agent: no tool result, no `tool_call`
    event.
- Effective wake resolution equals the invoker's cadence (D4), so
  `wake_min` must be ≥ that cadence.

### P7. Budget accounting

**P7.1 Token invariant.** `input_tokens` is the **TOTAL** prompt token
count for a call. `cache_read_tokens` and `cache_write_tokens` are
component subsets of it; the uncached remainder is `input_tokens −
cache_read_tokens − cache_write_tokens`. `output_tokens` **includes**
reasoning/thinking tokens; `reasoning_tokens` is an informational subset
logged when the provider reports it. Wire-format differences die inside
adapters (D2).

**P7.2 Cost formula** (`governor.cost_usd`), per call, from the
manifest-pinned list-price table:

```
cost_usd = ((input_tokens − cache_read_tokens − cache_write_tokens) × price_in
            + cache_read_tokens  × price_read
            + cache_write_tokens × price_write
            + output_tokens      × price_out) / 1e6
```

`price_read` / `price_write` fall back to `price_in` when the manifest
omits the cache columns (conservative: accounted ≥ invoiced). With all
cache fields zero the formula reduces exactly to input×in + output×out.
Per-call component counts reconcile digit-for-digit against provider
ledger columns; dollars are derived, never authoritative.

**P7.3 Boundary check.** At P1.6 only: `cumulative_usd ≥ budget_usd` →
`run_complete reason=budget`; else elapsed since `first_session_at ≥
t_max_days` → `reason=t_max`. Budget is checked **before** t_max; stop =
min(budget, t_max). An in-flight session is never terminated for budget
or t_max; the overshoot is bounded by the session caps and recorded as
`overspend_usd`. On stop the supervisor entry is removed.

**P7.4 What is counted.** Every `llm_call` event contributes to
`cumulative_usd` / `cumulative_tokens`, including failed attempts
(logged at cost 0 with `usage_unknown: true`) and retried empty
responses (cost 0, `empty_response: true`). In-world resources (MUSU,
ONYX, gas) are outside `budget_usd` and are not tracked here.

### P8. Model adapter interface

```python
class ModelAdapter(Protocol):
    def complete(self, system: str, messages: list[Message],
                 tools: list[ToolDef], params: SamplingParams) -> AdapterResponse: ...
```

- `Message` is `{role: "user", text}` |
  `{role: "assistant", text?, tool_calls?, provider_state?}` |
  `{role: "tool_result", tool_call_id, content, is_error}`.
- `ToolDef = {name, description, input_schema}` — JSON Schema authored
  once, translated per provider, restricted to the subset all three
  providers accept (objects, scalars, arrays, enums, required; no
  `oneOf`/`anyOf`/`allOf`).
- `AdapterResponse = {text_blocks, tool_calls, stop_reason, usage,
  provider_state?, provider_meta}`; `provider_meta` is logged raw and
  never parsed by the loop.
- `stop_reason` is the closed enum `end_turn | tool_use | max_tokens |
  refusal`. An unmappable provider stop reason raises rather than being
  guessed.
- **Provider reasoning state** (`provider_state`) is an opaque,
  adapter-owned payload (signed thinking blocks, thought signatures) set
  by the emitting adapter and replayed by that same adapter **within one
  session**. The loop never inspects it; it never crosses sessions and
  never reaches telemetry. An adapter ignores state it did not produce.
- **Retries** (loop-owned, SDK retries disabled): exponential backoff
  `min(60s, 1s × 2^attempt)` for rate limits, 5xx, timeouts and
  connection failures, up to `retry_max_attempts` (default 5) retries
  after the initial attempt. Every attempt is logged. Non-retryable
  errors end the session immediately (`errors`).
- **Empty responses**: no text, no tool calls, and zero usage is treated
  as a provider fault — retried under the same backoff, logged at cost 0
  with `empty_response: true`, never routed into the continuation/error
  path. An empty-but-billed response (nonzero usage) keeps normal
  handling.

### P9. Telemetry event schema — downstream contract

`run/telemetry.jsonl`, one JSON object per line, append-only. Machine
contract: **`schema/telemetry.json`**, JSON Schema draft 2020-12,
`version: 0.3.1`, shipped inside the wheel as package data and kept
byte-identical to the repo copy. Every event is validated **before** it
is written; an invalid event raises and never lands. Unknown fields are
rejected (`unevaluatedProperties: false`), so additive changes require a
schema version bump.

Common required fields on every event: `ts` (ISO-8601 UTC, pattern
enforced), `run_id`, `session`, `event`.

| event | required | optional |
|---|---|---|
| `run_start` | `manifest_hash`, `model`, `harness_sha`, `agent_sha`, `gdd_sha`, `harness_tools[]`, `price_table` | — |
| `session_start` | `trigger` (`scheduled`\|`manual`), `budget_remaining_usd`, `wallclock_elapsed_s`, `tools_hash` | `presentation_mode` |
| `llm_call` | `model`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `cost_usd`, `cumulative_usd`, `cumulative_tokens`, `latency_ms`, `stop_reason`, `retry_count` | `reasoning_tokens`, `usage_unknown`, `continuation`, `empty_response` |
| `tool_call` | `tool`, `source` (`harness`\|`scaffold`), `duration_ms`, `ok` | `initiator` (`model`\|`scaffold`), `path` (file tools), `error`, `truncated`, `original_bytes`, `skipped`, `tx_hash`, `tx_terminal_state` |
| `workspace_write` | `path`, `bytes`, `workspace_total_bytes` | — |
| `workspace_delete` | `path`, `workspace_total_bytes` | — |
| `schedule_next` | `source` (`agent`\|`default`), `clamped_min`, `next_wake_at` | `requested_min`, `carried`, `carried_invalid` |
| `session_end` | `reason` (P5 enum), `llm_calls`, `tool_calls`, `session_cost_usd`, `session_tokens` | `repetition_rule`, `repetition_signature`, `repetition_tool`, `repetition_count`, `repetition_window`, `repetition_distinct`, `repetition_signatures[]` |
| `run_complete` | `reason` (`budget`\|`t_max`\|`manual`), `totals{sessions, llm_calls, cumulative_usd, cumulative_tokens, overspend_usd}` | — |

Reader notes (stable semantics):

- `llm_call.stop_reason` accepts the P8 enum **plus `error`**, which
  marks a failed-but-logged attempt with no provider stop reason.
- `llm_calls` counts emitted `llm_call` events, retries and empty
  responses included; filter on `usage_unknown` / `empty_response` for
  billable calls.
- `session_end.tool_calls` counts emitted `tool_call` events, **skipped
  intents included**; `session_tool_cap` counts executed intents only.
- `tool_call.ok` is **exception-keyed, agent-side**: false when the call
  raised into the loop, true otherwise. It is not a claim about the
  chain. Against a harness that returns confirmed reverts in band,
  `ok=true` covers reverted transactions; against one that raises them
  (D1), a revert is `ok=false` and `ok=true` regains its plain meaning
  for a submitted transaction. Since which holds is a property of the
  pinned harness, `ok` must be read together with `tx_terminal_state`
  and never alone.
- `tool_call.initiator` names **who asked for the call**, which `source`
  does not: `source` names the layer that owns the tool, `initiator`
  names the layer that wanted it run. `model` is every intent the agent
  returned; `scaffold` is only the session-start brief (P1.12) — a
  `harness`-source call with a `scaffold` initiator. **Any measure of
  agent behavior must exclude scaffold-initiated calls**: they are reads
  the agent did not choose, and they consume none of the caps that bound
  what it does. The field is optional in the schema so streams written
  under 0.3.0 and earlier still validate; from 0.3.1 on it is emitted on
  every `tool_call`, so its absence in a 0.3.1+ stream is a defect, not a
  default.
- `tool_call.tx_terminal_state` names the transaction outcome the
  harness reported — `confirmed_success` | `reverted` | `unconfirmed` |
  `validation_rejected` | `batch_error` — classified once at ingestion
  so downstream analysis never string-matches harness prose. It is
  **absent** whenever the call was not one transaction outcome: reads,
  scaffold tools, non-transaction errors, in-band partial batches, and
  pre-send dry-run skips. Absence means *not classifiable as one
  terminal state*, never *succeeded*.
- `session_start.presentation_mode` is the mode the manifest pinned and
  the scaffold passed to the harness child. Absent when the manifest
  pinned none, in which case the harness applied its own default —
  recorded as absence rather than guessed at.
- `schedule_next` appears exactly once per session, `wake_default` case
  included.
- `session_end reason=crash` is synthetic (P3).
- Telemetry is not an agent-visible channel: budget fields recorded here
  never reach the agent.
- Tool arguments and results live in transcripts, not telemetry — except
  the `path` of file-tool calls, promoted so documentation- and
  memory-access patterns are analyzable without transcript parsing.
- Quest completions are not logged locally; they are read from chain
  state and joined by timestamp / `tx_hash` in analysis.
- `provider_state` never appears in telemetry.

### P10. Scaffold tools (never part of the harness MCP surface)

| tool | signature | contract |
|---|---|---|
| `workspace_write` | (path, content) | creates parent dirs, replaces the whole file, `workspace/` only, quota-checked on the projected total |
| `workspace_read` | (path, offset?, length?) | serves `workspace/` and `reference/`; byte-based slicing so truncated results are re-readable |
| `workspace_list` | (path?) | tree with byte sizes; no path → full `workspace/` + one-line `reference/` summary |
| `workspace_delete` | (path) | `workspace/` only |
| `set_next_wake` | (minutes_from_now) | clamped, last call wins (P6) |
| `get_status` | () | JSON with exactly `current_time_utc`, `session_number`, `workspace_bytes_used`, `workspace_quota_bytes` — nothing else |
| `end_session` | (reason: free text) | immediate; reason logged (P2) |

Game perception and action come exclusively from the harness MCP tools
(D1), loaded per session. Tool order presented to the model is game
tools first, scaffold tools second, deterministic so `tools_hash` is
stable.

The list above is the whole scaffold surface. The session-start brief
(P1.12) added no entry to it: the brief is a call to a harness tool, so
the tool surface the model is shown — and with it `tools_hash` — is
unchanged by its existence.

### P11. Workspace conventions

- **The agent may write only under `workspace/`**, subject to
  `workspace_quota_bytes` (default 10 MB) measured over the whole tree.
  A rejected write leaves no partial file.
- `reference/` is read-only by construction: writes and deletes are
  rejected, not silently ignored.
- **Paths are relative to the workspace root.** A bare `notes.md` and a
  prefixed `workspace/notes.md` name the same file (exactly one leading
  `workspace/` segment is stripped). `reference/...` addresses the
  read-only tree. Tool descriptions state this.
- Absolute paths, `~`-paths, NUL bytes, `..` escapes, and symlinks
  leaving a root are rejected. Bare paths can never reach run-directory
  internals (`state.json`, `telemetry.jsonl`, `prompts/`,
  `transcripts/`, `config.yaml`, `run.lock`).
- The scaffold never writes into `workspace/` beyond creating the
  directory; its content is entirely agent-authored and is the only
  thing that survives between sessions.

### P12. Run directory and CLI

```
run/
├── config.yaml       # verbatim manifest copy (D3); immutable per run
├── state.json        # scaffold-owned CACHE (P3): session_counter, cumulative_usd,
│                     # cumulative_tokens, next_wake_at, run_status, first_session_at
├── run.lock          # PID + created (P4); absent between sessions
├── workspace/        # agent-owned (P11)
├── reference/        # read-only documentation snapshot (D5)
├── prompts/          # frozen strings: system.txt, kickoff.txt, continue.txt
├── transcripts/      # session-NNNN.jsonl, messages exactly as sent (post-truncation)
└── telemetry.jsonl   # append-only event stream (P9) — source of truth
```

- `kami-agent init --manifest M --run-dir DIR` — validation and
  scaffolding only: copies the manifest, materializes the frozen
  prompts, creates `workspace/` and `transcripts/`, runs connectivity
  checks (chain RPC, mainnet RPC with `eth_chainId == 1`, provider API,
  MCP handshake), emits `run_start`. **There is no key path through
  init**: it never generates, imports, or writes key material.
  `--skip-connectivity` skips the four checks (and leaves
  `run_start.harness_tools` empty).
- `kami-agent run-session --run-dir DIR [--manual]` — one session (P1).
- `kami-agent status --run-dir DIR` — prints the `state.json` summary.
  Operator-facing; never an agent channel.

### P13. Frozen prompt strings

Three strings ship per run and are byte-frozen: `prompts/system.txt`,
`prompts/kickoff.txt`, `prompts/continue.txt`. The system prompt states,
in order: the situation (autonomous agent, periodic sessions, tool calls
are the only effect, no human reads the text); the objective (complete
as many quests as possible); persistence (`workspace/` survives, its use
and structure are the agent's own); `reference/` as read-only
documentation; the two tool families; scheduling via `set_next_wake`
within the bounds, and that there is no in-session waiting; and that
on-chain actions cost gas even when they revert.

Excluded by construction: budget, cost, tokens, compute limits, run
duration, session caps, forced truncation, the existence of measurement
— and equally, strategy hints, tool-usage advice, memory-structure
suggestions, XML-tag formatting, and vendor-idiomatic phrasing (I5).

---

## Depends

### D1. Harness MCP surface

- Spawned per session as a **stdio child** from the manifest
  (`harness.command`, `args`, `cwd`, `env`, `handshake_timeout_s`); the
  scaffold's environment is passed through. The harness owns its own
  required environment (e.g. a mainnet RPC URL) and refuses to start
  without it.
- Handshake failure aborts the session before any model call (P1.8).
- The tool surface is read at session start via MCP `list_tools` and
  used as given: names, descriptions, and input schemas are passed to
  the provider unmodified.
- **Identity is recorded, not negotiated.** There is no
  `SCHEMA_VERSION`-style version handshake. Two artifacts stand in:
  `pins.harness_sha` from the manifest, recorded on `run_start`
  (operator-asserted, **not** verified against the running child), and
  `tools_hash` — `sha256` over the sorted `(name, description,
  input_schema)` of the full loaded surface — recorded on every
  `session_start`. That value is the **scaffold's** fingerprint of what
  the model was shown: it spans harness *and* scaffold tools, uses this
  module's serialization, and carries a `sha256:` prefix. A harness may
  publish a hash of its own registry as well; such a value answers a
  different question over different bytes and is **different by
  construction**. The two are never equated, reconciled, or asserted
  against each other. Drift is **detected in analysis and in CI**
  (a committed recorded-surface fixture whose hash is asserted), never
  refused at runtime.
- **Presentation mode.** When the manifest pins `presentation_mode`, the
  scaffold sets it in the harness child's environment as
  `PRESENTATION_MODE` (an explicit `harness.env` entry still wins) and
  records it on `session_start`. The value is passed through
  **unvalidated and uncaught**: the scaffold owns no mode enum, and a
  mode the pinned harness does not implement must abort the handshake
  there — surfaced loudly by `init`'s connectivity check — rather than
  be normalized here into a silently different run. With no mode pinned
  the scaffold sets nothing and records nothing.
- Failure surface consumed: an MCP `isError` result becomes a tool error
  (P2), and **its message reaches the model verbatim** — the only
  transformation applied to any tool result is the P2 byte cap. The
  scaffold never rewords a harness error, never appends judgment or
  advice to one, and never retries a failed tool call: the loop's retry
  policy covers model calls only (P8), so an intent is dispatched to the
  harness exactly once. A success-shaped result carrying revert/error
  markers is classified by P5.1 and by nothing else.
- **Transaction outcomes are recorded, not interpreted.** A harness may
  report a submitted transaction's outcome by returning it in band or by
  raising it; either way the outcome is classified once, at ingestion,
  into `tool_call.tx_terminal_state` (P9) from the harness's own
  contract text. Analysis therefore splits validation-rejects, reverts,
  and unconfirmed transactions on a field. The classification is
  observation only: it changes nothing the agent sees, and an
  unrecognized message is recorded as no state rather than guessed at.
- `tx_hash` is extracted best-effort from structured content or JSON
  text, top level or one `result` level down, for telemetry only.
- **One tool is depended on by name.** The session-start brief (P1.12)
  calls `lens_party`, the surface's general any-operator party report,
  for the account's own operator. This is the scaffold's only
  name-coupling to the harness surface, and it is a soft one: a pin whose
  surface does not carry the tool simply produces no brief (X20), and one
  that carries it under changed semantics produces a brief whose content
  is whatever the harness now returns — recorded, not validated, exactly
  as every other harness result is.
- **Cap arithmetic assumption.** Every call re-sends the system prompt,
  the file index, the entire tool surface, and the session-start brief.
  That fixed floor must leave room for a session to be more than one
  call: the worst-case first-call floor is assumed **≤ 1/3 of
  `session_token_cap`**. The brief makes the floor a function of the
  account's roster size — it grows linearly in the number of kamis the
  account owns, and that number grows over a run — so the assumption is
  no longer a one-off measurement but one that has to be re-checked as
  the roster grows. This is
  **not enforced anywhere in the scaffold** — it is an operator sizing
  obligation on the manifest. The tri-provider smoke tier reports the
  observed floor (`fixed_floor_input_tokens=…`) for that purpose. A
  violated assumption degrades quietly into single-call sessions; a
  grossly undersized `session_token_cap` relative to the model's context
  window instead produces `reason=errors` sessions that analysis would
  misread as model failure.
- **Context-guard headroom** (same owner): because the guard is checked
  post-call, one full turn lands in context before the next check.
  Headroom below the model's context window must cover
  `(max parallel intents × tool_result_max_bytes / 4) + max_tokens`.

### D2. Provider APIs via adapters

One adapter per provider; provider quirks never leave the adapter. All
three disable SDK-level retries so the loop owns retry policy and
logging (P8).

| provider | caching mode | what accounting assumes |
|---|---|---|
| Anthropic | **explicit** — the adapter places `cache_control` ephemeral (5-minute) breakpoints: one on the last system block (render order tools → system → messages, so one entry covers the whole fixed floor) and a rolling one on the last content block of the final message, plus at most one intermediate breakpoint when a turn exceeds the 20-block lookback. Never more than 3 of the provider's 4 breakpoints; never on thinking blocks; annotation is non-destructive | wire `usage.input_tokens` **excludes** cached tokens, so `cache_read_input_tokens` + `cache_creation_input_tokens` are folded back in; `output_tokens` already includes thinking; no reasoning-token figure is reported |
| OpenAI | **automatic** — nothing is requested | `prompt_tokens` already **includes** cached tokens (passed through); `prompt_tokens_details.cached_tokens` → `cache_read_tokens`, 0 when absent; no write premium, so `cache_write_tokens` is always 0; `completion_tokens` already includes reasoning, `completion_tokens_details.reasoning_tokens` is the informational subset |
| Google | **automatic (implicit)** — nothing is requested | `promptTokenCount` already **includes** cached tokens (passed through); `cachedContentTokenCount` → `cache_read_tokens`; `cache_write_tokens` always 0; thoughts are reported **outside** the candidate count and are folded into `output_tokens`, with `thoughts_token_count` as `reasoning_tokens`; `reasoning_effort` has no native equivalent and is not sent |

`cache_control` is request metadata: the prompt bytes sent to the model
are byte-identical with or without it, and nothing about caching reaches
the agent (I1). Prices are manifest-pinned list prices (D3); their
correctness against provider invoices is operator-owned.

### D3. Manifest fields consumed

The manifest is **owned by the operator side**; the scaffold copies it
verbatim into `run/config.yaml` and reads exactly these keys. There is
no scaffold-side manifest schema — an unknown key under `caps:` raises
at construction, everything else is silently ignored.

| key | consumed by |
|---|---|
| `run_id` | every telemetry record |
| `provider` (`anthropic`\|`openai`\|`google`), `model` | adapter selection, `llm_call.model` |
| `price_table.{input,output}_usd_per_mtok` | P7.2 (required) |
| `price_table.cache_{read,write}_usd_per_mtok` | P7.2 (optional; absent → input rate) |
| `params.{max_tokens,temperature,reasoning_effort}` | sampling; each sent only where the provider accepts it |
| `caps.session_token_cap` | P5 (**required**, no default — sized per model) |
| `caps.{session_tool_cap,max_consecutive_errors,retry_max_attempts,tool_timeout_s,tool_result_max_bytes}` | P2, P5, P8 |
| `caps.{repetition_identical_cap,repetition_window,repetition_min_distinct,repetition_same_tool_error_cap}` | P5.1 |
| `budget_usd`, `t_max_days` | P7.3 |
| `wake.{min_minutes,max_minutes,default_minutes}` | P6 |
| `workspace_quota_bytes` | P11 |
| `lock_stale_s` | P4 |
| `chain_rpc_url` | `init` connectivity check only |
| `presentation_mode` | D1 — passed to the harness child as `PRESENTATION_MODE`, recorded on `session_start`; never validated scaffold-side |
| `harness.{command,args,cwd,env,handshake_timeout_s}` | D1 |
| `pins.{agent_sha,harness_sha,gdd_sha}` | `run_start` provenance only — recorded, never verified |

Not manifest-driven despite being run parameters: **`poll_cadence`**
(an argument to the supervisor's cron installer, default 5 min) and
**`budget_visible`** (X10).

### D4. Periodic invoker

The scaffold assumes only that `run-session` is invoked repeatedly; it
holds no daemon state between invocations. `supervisor.install_cron` /
`uninstall_cron` manage a tagged crontab line at `poll_cadence`, and
`run_complete` removes it — any equivalent scheduler satisfies the
contract. Wake resolution equals the invoker's cadence (P6). The runner
must behave identically under a cron-like environment — minimal `PATH`,
explicit `HOME`, non-interactive shell — which is what I9 asserts.

### D5. Bundled documentation snapshot

`run/reference/` is populated outside the scaffold — by packaging, at
the pinned `gdd_sha` — and is read-only by construction (P11). `init`
warns rather than fails when it is absent, so a dev run without it is
possible; a run without it silently deprives the agent of its only
documentation.

### D6. Host filesystem

Durability of telemetry (I6) assumes `flush` + `fsync` semantics and an
atomic `os.replace` for the state cache. Keys are read from a run-dir
`.env` (existing environment wins) and never enter the manifest, the
config copy, transcripts, or telemetry.

---

## Invariants

| # | claim | enforcement |
|---|---|---|
| I1 | No budget, spend, run-duration, cap, or measurement information reaches the agent through any channel — system prompt, tool descriptions, tool results, error messages, or `get_status` | `tests/unit/test_prompts.py::test_no_apparatus_or_policy_leaks` (forbidden-vocabulary scan over all three frozen strings), `tests/unit/test_scaffold_tools.py::test_no_apparatus_leaks_in_agent_visible_tool_strings`, `::test_get_status_exactly_four_fields`; tri-provider smoke re-scans every agent-visible string of a real session |
| I2 | Budget and t_max are checked **only** at session boundaries; no in-flight session is ever terminated for either | single `boundary_check` call site in `runner.run_session`; `tests/unit/test_governor.py` (budget, t_max, precedence, overspend), `tests/unit/test_runner.py::test_budget_boundary_completes_run`, `::test_t_max_boundary` |
| I3 | Zero strategy content in the scaffold or the prompts — mechanics only | `tests/unit/test_prompts.py::test_frozen_strings_are_exactly_as_reviewed` (byte-exact; any reword must be re-frozen in the same commit) + the I1 scans + review discipline on every agent-visible string |
| I4 | Forced endings are silent: no warning message, no final model call, no tool result, no `tool_call` event for the carried wake | `tests/unit/test_loop.py::test_context_guard_trips_post_call_and_is_silent`, `tests/unit/test_repetition.py::test_trip_is_silent_and_ends_like_tool_cap`, the carried-wake suite (`test_token_cap_carries_final_turn_wake_intent` … `test_cap_without_wake_intent_carries_nothing`) |
| I5 | Frozen strings and code defaults cannot silently diverge (wake bounds), and the packaged copies match the repo copies byte-for-byte (prompts and telemetry schema) | `tests/unit/test_prompts.py::test_wake_bounds_in_frozen_prompt_match_code_defaults`, `::test_packaged_prompts_match_repo_prompts`, `tests/unit/test_telemetry.py::test_packaged_schema_matches_repo_schema`, `::test_schema_resolves_inside_the_installed_package` |
| I6 | Telemetry is append-only and crash-consistent: one line per event, validated before write, `write → flush → fsync` before the action it describes is complete; a crash loses at most the event being written | `TelemetryWriter.emit`; `tests/unit/test_telemetry.py::test_append_only_ordering`, `::test_appends_across_writer_instances`, and the rejection suite (`unknown event`, `missing required`, `bad enum`, `wrong type`, `extra field`, `non-UTC ts`) proving invalid events never land |
| I7 | Telemetry is the source of truth for accounting; `state.json` is a cache rebuilt by folding the stream, and a crashed session is closed exactly once | `tests/unit/test_state.py::test_fold_recomputes_accounting_from_the_stream`, `::test_crashed_session_detected`, `tests/unit/test_runner.py::test_crash_recovery_writes_synthetic_end_and_refolds_accounting`, `::test_crash_recovery_is_idempotent` |
| I8 | Every stop reason is enumerated and telemetered — the `session_end.reason` enum is closed and each value has a producing path | schema enum + `tests/unit/test_telemetry.py::test_schema_covers_exactly_the_spec_events`, `::test_bad_enum_value_rejected`; producers covered by `test_loop.py` (agent, token_cap, tool_cap, errors), `test_repetition.py` (repetition), `test_runner.py` (crash, harness-abort errors) |
| I9 | Cron-env parity: a session behaves identically under a cron-like environment and a manual start | the `cron-smoke` CI job — one full `init` + `run-session` under `env -i PATH=/usr/bin:/bin HOME=…`, absolute interpreter path, no provider keys, real exit codes, followed by `tests/cron_smoke/check_telemetry.py` (exactly one `session_start`/`session_end` pair, expected reason, exactly one agent-source `schedule_next`, every event re-validated) |
| I10 | `input_tokens` is the total prompt count and the cache fields are components of it, never additions; `output_tokens` always includes reasoning tokens | per-adapter tests: `test_anthropic_adapter.py::test_usage_folds_cache_components_into_total_input`, `test_openai_adapter.py::test_cached_prompt_tokens_are_a_component_not_an_addition`, `test_google_adapter.py::test_cached_content_tokens_are_a_component_not_an_addition`, `::test_the_reasoning_token_fold`, `test_governor.py::test_cache_zero_reduces_exactly_to_v0_formula` |
| I11 | No agent-supplied path escapes `workspace/` or `reference/`, and run-directory internals are unreachable | `tests/unit/test_sandbox.py` — escapes, one-segment stripping, run-dir internals, symlink escape, plus a Hypothesis property test over arbitrary segments |
| I12 | Parallel intents execute strictly sequentially in the returned order; `end_session` is immediate and later intents are skipped and logged | `tests/unit/test_loop.py::test_batch_executes_in_order_and_skips_after_end_session`, `::test_later_intents_see_earlier_effects`, `::test_end_session_at_cap_is_still_agent` |
| I13 | The session number is claimed before the first model call, so a crash never reuses one | ordering in `runner.run_session` (persist, then run) + `tests/unit/test_runner.py::test_crash_recovery_writes_synthetic_end_and_refolds_accounting` |
| I14 | At most one session per run directory at a time; a crashed session never deadlocks the run | `tests/unit/test_supervisor.py` (live lock respected, dead PID / age-stale / corrupt lock broken), `tests/unit/test_runner.py::test_lock_held_exits_without_touching_anything`, `::test_stale_lock_is_broken_and_run_proceeds` |
| I15 | Exactly one `schedule_next` per session, on every ending path | `emit_schedule` on all runner paths; `tests/unit/test_runner.py::test_default_schedule_when_agent_never_calls_set_next_wake`, `::test_harness_failure_aborts_with_zero_model_calls`, `::test_normal_session_emits_no_carried_fields`, cron-smoke assertion |
| I16 | Every tool result entering context is capped with an explicit marker, and the cap is recorded | `tests/unit/test_truncation.py` (marker, multibyte boundary, default), `tests/unit/test_loop.py::test_big_read_truncated_with_reread_hint` |
| I17 | Provider reasoning state is opaque, same-session, same-adapter, and never reaches telemetry | `tests/unit/test_provider_state.py` (capture, verbatim replay, foreign-state ignore per adapter, `test_loop_copies_state_verbatim_without_inspecting`, `test_transcript_records_state_as_sent`) |
| I18 | The tool surface presented to the model is deterministic and collision-free | `tests/unit/test_loop.py::test_harness_scaffold_name_collision_rejected`, `tests/unit/test_harness_client.py::test_tools_hash_is_deterministic_and_sensitive` |
| I19 | Tool schemas stay inside the subset all three providers accept | `tests/unit/test_scaffold_tools.py::test_tool_defs_cover_spec_surface` (no `oneOf`/`anyOf`/`allOf`) + the tri-provider tier parsing every call natively |
| I20 | The agent's only channels are the harness tools, `reference/`, and `workspace/` — the scaffold exposes no web, shell, or other egress | the scaffold tool list is exactly the seven of P10 (`test_tool_defs_cover_spec_surface`); network-level closure is operator-owned (see *Unowned*, README) |
| I21 | A harness error reaches the model verbatim — no rewording, no added judgment or advice, no swallowing — and the tool call behind it is dispatched exactly once | `tests/unit/test_loop.py::test_raised_outcome_reaches_the_model_verbatim_and_telemetry_by_field` (whole-message equality against the harness text, per terminal state), `::test_a_raised_outcome_is_executed_once_and_never_retried`, `tests/unit/test_harness_client.py::test_raised_terminal_states_reach_the_caller_verbatim` (through a real MCP child, whose error wrapping the classifier must tolerate) |
| I22 | The three post-broadcast terminal states plus the pre-signing rejection are recorded as distinct field values, and nothing else is ever recorded as one of them | `tests/unit/test_receipts.py` (per-state classification, MCP-wrapped and bare; batch messages never read as the item states they quote; non-transaction errors classify as nothing), `tests/unit/test_telemetry.py::test_every_terminal_state_is_accepted`, `::test_invented_terminal_state_rejected` (closed enum), `tests/unit/test_loop.py::test_scaffold_failures_carry_no_terminal_state`, `::test_reads_carry_no_terminal_state` |
| I23 | The pinned presentation mode reaches the harness child unvalidated and lands on every `session_start`; an unsupported mode is neither normalized nor caught | `tests/unit/test_cli.py::test_presentation_mode_reaches_the_harness_child`, `::test_presentation_mode_is_passed_through_unvalidated`, `::test_unpinned_presentation_mode_sets_nothing`, `::test_explicit_harness_env_still_wins`, `tests/unit/test_runner.py::test_pinned_presentation_mode_lands_on_every_session_start`, `::test_presentation_mode_is_recorded_as_given` |
| I24 | The session-start brief is one call to the general party-report tool, executed before the first model call, injected verbatim as a normal tool result, attempted exactly once, and separable in telemetry from what the agent chose — and it bounds nothing the agent does | `tests/unit/test_brief.py` — ordering (`test_brief_is_executed_before_the_first_model_call`), whole-message verbatimness (`::test_brief_result_is_injected_verbatim`), no special path (`::test_brief_is_the_general_tool_and_stays_available_to_the_agent`), provenance (`::test_brief_is_telemetered_like_any_tool_call_and_marked_scaffold_initiated`), cap/counter/breaker exclusion (`::test_brief_consumes_no_session_tool_cap`, `::test_a_failed_brief_does_not_advance_the_consecutive_error_counter`, `::test_brief_never_feeds_the_repetition_breaker`), degradation (`::test_a_failing_brief_is_injected_as_its_error_and_the_session_proceeds`, `::test_a_failing_brief_is_attempted_exactly_once`, `::test_no_brief_when_the_loaded_surface_does_not_carry_the_tool`); end to end through the real CLI in the `cron-smoke` job and natively per provider in the tri-provider tier |

---

## Deliberate deviations

Accepted by design. Each is a behavior a future rework might mistake for
a bug; changing one is a spec change, not a fix.

- **X1 — The repetition breaker clips some legitimate single-call
  loops.** Five consecutive identical signatures end the session even
  when the repetition is productive (polling one value until it
  changes). Accepted: consecutive-not-cumulative counting leaves
  observed productive re-read behavior (max 4) one call of margin, the
  agent loses nothing but the remainder of a session, and `workspace/`
  survives.
- **X2 — `window_diversity` can clip a legitimate low-diversity
  stretch.** A long run of work that normalizes to ≤4 distinct
  signatures over 30 executed calls trips the rule. Accepted for the
  same reason; it is the only catch for rotating poll cycles that
  consecutive counting misses.
- **X3 — Carried wake is deliberately asymmetric.** Exactly one
  cap-skipped `set_next_wake` is executed at teardown, while every other
  skipped intent is discarded forever. Without it, every cap-truncated
  session would fall back to `wake_default` and bias pacing
  measurement. It executes invisibly (no tool result, no `tool_call`
  event) to preserve I4.
- **X4 — Carried wake does not apply to `errors` endings, nor to
  intents skipped by `end_session`.** An erroring session has no
  trustworthy final turn, and an `end_session` batch already expressed
  the agent's intent to stop.
- **X5 — An invalid carried wake is discarded silently.** A previously
  executed wake stands, else `wake_default`; the discard is recorded as
  `carried_invalid`. No error is surfaced anywhere the agent can see.
- **X6 — The budget is a soft cap.** Overshoot up to one session's cost
  is expected and recorded as `overspend_usd`; the exact spend line is
  drawn post hoc from per-call `cumulative_usd`.
- **X7 — The context guard is post-call.** A full turn can land beyond
  `session_token_cap` before the check; the cap must be sized with
  single-turn headroom (D1).
- **X8 — Failed and empty model attempts are emitted as `llm_call`
  events at cost 0** and counted in `session_end.llm_calls`. Analysis
  must filter `usage_unknown` / `empty_response` to count billable
  calls; the alternative (dropping them) would hide retry storms.
- **X9 — Skipped intents emit `tool_call` events and count toward
  `session_end.tool_calls`, but never toward `session_tool_cap` or the
  repetition breaker.** Only executed calls consume caps.
- **X10 — `budget_visible` exists as a constructor flag on the scaffold
  tools, pinned false, and is deliberately not manifest-wired.** It is
  mechanism kept alive for a future budget-visible configuration;
  reaching it requires a code change, which is the intended friction.
- **X11 — The harness child is spawned before `session_start` is
  emitted**, inverting a naive reading of the lifecycle, because
  `session_start` carries `tools_hash`. The hard ordering constraint
  (P1.7 before any model call) is preserved.
- **X12 — Recovery is deferred to the next *due* session.** The wake
  gate runs before recovery, so a crashed session's synthetic
  `session_end` is written when the run next comes due, not at the next
  poll.
- **X13 — `stop_reason: "error"` is telemetry-only.** It has no
  `AdapterResponse` counterpart and marks failed attempts on the retry
  path.
- **X14 — One consecutive-error counter covers both tool failures and
  tool-less turns**, and at the cap the session ends *without* sending
  the continuation string.
- **X15 — Unknown tool names are attributed `source: "scaffold"`** in
  telemetry, because the scaffold layer is what rejects them.
- **X16 — `init` warns rather than fails when `reference/` is absent**,
  so dev runs work; a production bring-up without it is an operator
  error the scaffold will not catch.
- **X17 — `presentation_mode` is passed to the harness unvalidated, on
  purpose.** The scaffold could reject a mode the pinned harness does
  not implement and give a tidier error. It does not: the harness owns
  the mode set, so validating here would duplicate a contract that can
  drift, and catching the harness's own refusal would turn a
  misconfigured manifest into a quietly different run. The failure
  lands at `init`, loudly, which is the intended friction.
- **X18 — `tool_call.tx_terminal_state` is absent, not `unknown`, when a
  call is not one transaction outcome.** Reads, scaffold tools,
  in-band partial batches, and dry-run skips carry no value at all. An
  explicit `unknown` would be indistinguishable from a classification
  failure; absence forces the reader to treat "no state" as "not one
  state" rather than as a fourth outcome.
- **X19 — the classifier matches the harness's contract prose, and
  degrades to no classification rather than to a guess.** The terminal
  state is only recoverable from message text, so drift in that text
  silently costs classification (the field goes absent) instead of
  producing a wrong label. The recorded-surface CI fixture and the
  copied message text in the fake MCP server are what surface the
  drift.
- **X20 — the session-start brief consumes no cap and is skipped, not
  reported, when the tool is absent.** It executes a real harness call
  yet counts toward neither `session_tool_cap` nor the consecutive-error
  counter nor the repetition breaker, and a pin whose surface lacks
  `lens_party` produces no brief and no telemetry at all. Both halves
  follow from the same reading: those counters exist to bound what the
  *agent* does, and a read the agent did not choose must not shrink its
  session or end it. The asymmetry is deliberate — the brief still emits
  a `tool_call` event and still counts in `session_end.tool_calls`, so
  nothing is hidden, it is only excluded from the caps.
- **X21 — a failed brief is injected as its error and the session
  proceeds.** One attempt, no retry, no fallback content, no abort: the
  error text the harness produced becomes the first tool result the model
  sees. The alternatives are worse. Retrying would make the scaffold do
  for itself what D1 forbids it to do for the agent; substituting
  placeholder content would put scaffold-authored prose in an
  agent-visible channel; aborting would let an unavailable read-side
  daemon end sessions that could still act. A session that opens on a
  visible failure is a session whose telemetry says so.

---

## Non-goals

- **N1** Multi-model roles (executor/optimizer splits).
- **N2** Knowledge packs or calibrated strategy priors of any kind.
- **N3** Mid-session compaction or context summarization. Cross-session
  memory exists only as agent-written `workspace/` files.
- **N4** Self-funding or economic self-sustainability.
- **N5** Long-TTL or cross-session prompt caching, and explicit cache
  APIs on OpenAI/Gemini — their automatic caching is measured, not
  managed.
- **N6** Web access, shell access, or any non-harness network channel
  from the agent loop.
- **N7** Any UI.
- **N8** Mid-session budget enforcement (X6), and any agent-visible
  budget channel while `budget_visible` is pinned false.
- **N9** Reordering, deduplicating, or dependency-analyzing parallel
  tool intents; retrying reverted transactions on the agent's behalf.
- **N10** Runtime refusal on harness surface drift (D1) — detection is
  analytical and CI-side.

---

## Changelog

| version | describes | change |
|---|---|---|
| 1.8 | v0.3.2 | Session-start status brief (P1.12): before the first model call the scaffold calls the pinned surface's general any-operator party report, `lens_party`, for the account's own operator, and injects the result verbatim as a normal tool result — one call covering every owned kami's on-chain state, HP current/total/rate, and cooldown, so orientation is not re-derived from scratch each session. Explicitly not a special path: the same tool stays available to the agent for any account, both invocations share one execution path, and only the new `tool_call.initiator` (`model` \| `scaffold`) separates them (P9). The brief is attempted exactly once and degrades visibly — a failure is injected as the error it is and the session continues (X21) — and it bounds nothing the agent does: no `session_tool_cap`, no error counter, no repetition breaker (X20). No new scaffold tool, so `tools_hash` is unchanged (P10). Telemetry schema 0.3.0 → 0.3.1 (one additive optional field). D1's cap arithmetic restated: the fixed floor now grows linearly with the account's roster size, so it is a standing measurement, not a one-off. |
| 1.7 | v0.3.0 | Consumption of a harness that **raises** confirmed reverts and unconfirmed transactions instead of returning them: harness error messages are contractually verbatim to the model and dispatched once (I21); the transaction outcome is classified once at ingestion into `tool_call.tx_terminal_state`, a closed five-value enum, so analysis splits validation-rejects / reverts / unconfirmed on a field rather than on prose (I22, X18, X19); `tool_call.ok` restated as exception-keyed and harness-dependent, to be read with the new field and never alone; P5.1's error-or-revert note restated as harness-dependent, with knobs unchanged. Harness `presentation_mode` pinned in the manifest, passed to the child unvalidated, and recorded on every `session_start` (I23, X17). `tools_hash` restated as the scaffold's own fingerprint, different by construction from any hash a harness publishes of its own registry. Telemetry schema 0.2.0 → 0.3.0 (two additive optional fields). |
| 1.6 | v0.2.0 (18f75d04) | Converged to a contract registry: Provides / Depends / Invariants / Deliberate deviations / Non-goals / Changelog, every claim verified against the code and paired with its enforcement. Newly stated as contract: the `run.lock` and transcript layout, the closed run-session outcome set, executed-vs-emitted tool-call accounting, `stop_reason: "error"`, the telemetry schema version as a downstream contract, the recorded-not-negotiated harness identity, per-provider caching modes and what accounting assumes of each, the consumed manifest key list, and the sixteen accepted deviations. Narrative, packaging, and CI-tier prose moved to `README.md` / `docs/packaging.md`. |
| 1.5 | — | Repetition breaker as a third forced-ending class; carried execution of a cap-skipped final-turn `set_next_wake`; three system-prompt additions (no human reads the text, no in-session waiting, gas is spent on reverts); workspace-root-relative file paths; empty-response retry semantics; consecutive (not cumulative) identical-call counting. |
| 1.4 | — | Cache-aware token accounting: provider-side prompt-cache usage measured on all three providers, Anthropic caching explicitly requested via `cache_control` request metadata, `cost_usd` cache-aware, price table extended with cache-rate columns. Prompt bytes and agent-visible channels unchanged. |
| 1.3 | — | `init` performs validation and connectivity checks only; no key path through it — operator-wallet creation became an in-run harness tool. |
| 1.2 | — | Opaque provider reasoning state on assistant messages, adapter-owned and same-session. |
| 1.1 | — | CI split into a per-PR recorded-surface gate and a scheduled live-harness tier. |
| 1.0 | — | First implementable specification: budget invisible to the agent, silent forced endings with boundary-checked soft budget, bundled read-only documentation snapshot, no constraint on agent interaction, plus the engineering semantics for cost basis, context guard, parallel-call serialization, and the tool-result cap. |
