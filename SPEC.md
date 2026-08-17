---
module: kami-agent
version: 2.1
describes: v0.5.1
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
8. **Read the profile's prompt assets** (P13): the base string plus the
   appendices this `scaffold_profile` pins. A pure filesystem read, done
   before the spawn below on purpose — a run directory that cannot
   deliver its profile's asset raises here, naming the missing file,
   before any child, any telemetry, or any `session_start` exists. The
   claimed session number (step 7) is spent; nothing else is.
9. **Spawn the harness child** and handshake (D1). Failure → a
   `session_start` / `session_end reason=errors` pair with zero model
   calls, a default-source `schedule_next`, and `session_aborted`.
10. **Emit `session_start`** carrying `tools_hash` of the loaded surface,
    the `scaffold_profile` this run pins, and, when the manifest pins
    one, the harness `presentation_mode` (D1).
11. **Build context**: system prompt = the base string + the profile's
    appendices + `\n\n` + the file index (full `workspace/` tree with
    byte sizes, `reference/` collapsed to one `N files, N bytes,
    read-only` line).
12. **Kickoff**: the first user message is the frozen constant
    `prompts/kickoff.txt`. No dynamic content, no digits.
13. **Session-start injections** (P1.12, below): the roster, the wallets'
    gas balances, and — on the `planning` profile — the plan file, each
    injected as a completed call/result pair.
14. **Agent loop** (P2) until a stop reason (P5).
15. **Persist**: `session_end`, transcript file, state cache.
16. **Schedule** (P6): exactly one `schedule_next`.
17. **Release lock** → `session_ran`.

#### P1.12 Session-start injections

Before the first model call — the same point at which the file index is
built into the system prompt (P1.11) — the scaffold performs the reads
below and injects each as a **completed call/result pair**: an assistant
turn carrying the call, then its result. In this order, always:

| # | injection | source | profiles | owner of the answer |
|---|---|---|---|---|
| 1 | compact **roster** | `lens` | all (unless no daemon is configured) | the world-state daemon (D7) |
| 2 | the wallets' **gas balances** | `harness` | all (unless no harness is configured) | the harness's own balance tool (D1) |
| 3 | the **plan file** `workspace/plan.md` | `scaffold` | `planning` | the agent itself (P11) |

What they share — and what makes them one contract rather than three
special cases:

- **Before the first model call**, so call 1 already carries them.
- **Injected as a tool result**, passed through **verbatim**: the
  scaffold owns the serialization and the P2 byte cap, which is the
  transformation every tool result gets, and nothing else. Nothing is
  summarized, reordered, filtered, or annotated.
- **Exactly one attempt each, no retry, degrade visibly** (X21). A
  failure is injected as the failure it is and the session proceeds.
- **They bound nothing the agent does** (X20): no `session_tool_cap`, no
  consecutive-error counter, no repetition breaker. They do emit
  `tool_call` events and so count in `session_end.tool_calls`.
- **`initiator: scaffold`** on every one of them, which is how analysis
  excludes reads the agent did not choose. From 0.5.0 that field alone no
  longer identifies the roster brief: split on `tool` (P9).

Where they differ: only the roster is a **special path** (X22) — not a
tool, not on the surface, not issuable by the agent. The balance call and
the plan read name tools that **are** on the surface, so for those two the
scaffold is pre-calling a call the agent could equally make, and does not
own anything the agent cannot reach.

##### P1.12.1 The roster brief

- **The query is `roster`**, taken straight from the daemon over its own
  socket, not through the harness. One line per kami — on-chain `index`,
  `state`, and `[hp, hpTotal]` — plus the room the account itself is
  standing in. It carries no authored strings at all by the query's
  design, so its `untrusted` path list is empty and its answer is
  identical in name-free mode.
- **No arguments are sent.** The daemon prefills the account index of an
  operator-argument query from its own configured default operator. The
  scaffold has no way to know which account a run is, so it asks the
  daemon rather than asserting one (D7).
- **It is a special path** (X22). The previous version's claim that it
  was not is retired: the roster is not a tool, it is not on the surface
  the model is shown, the agent cannot issue it, and it runs on its own
  execution path rather than through the loop's tool dispatch. What the
  agent keeps is the **full** per-kami detail — names, HP rate, accrual,
  cooldown, node — on the harness's own `lens_party`, unchanged and
  callable for any account.
- **Its serialization is compact JSON** of the daemon envelope, under
  the P2 byte cap.
- **Its failure record.** A failure is injected as a minimal
  machine-shaped record — `{"error": {"code",
  "message"}}` — and the session continues; there is no retry, no
  fallback content, and no abort. A query error carries the daemon's own
  code and message. A transport failure has no daemon text to quote, so
  its code is the scaffold's (`LENS_UNAVAILABLE`) and its message is the
  operating system's; that record is frozen and leak-scanned exactly as
  the prompt assets are (P13, I1).
- It is **skipped entirely** when no daemon is configured
  (`lens.enabled: false`), leaving no telemetry. It is *not* skipped when
  a configured daemon is unreachable: that degrades visibly instead, so a
  run that expected a brief and got none says so.
- The brief sits in call-1 context, so it is part of the fixed floor D1's
  cap arithmetic is sized against, and its size is linear in the
  account's roster size. The compact form cut both the constant and the
  slope by close to an order of magnitude against the party report it
  replaced, which is most of why D1's ⅓-cap assumption is no longer
  under pressure from roster growth. The smoke tier reports both numbers
  together; floors do not compare across 0.4.0.

##### P1.12.2 The wallets' gas balances

- **Every profile.** Gas visibility is not a rung of the ladder: the
  ladder varies how *documentation* reaches the agent, while how much ETH
  its wallets hold is world state, on the same footing as its kamis'
  health. The fixed system prompt states that the balances arrive each
  session (P13), so their presence is a stated fact rather than a
  surprise.
- **The call is the harness's own balance tool, with no arguments.** The
  tool's empty account label reports every account the harness holds, so
  the scaffold never has to know which account the run is — the same
  reasoning as the roster's argument-free query. The payload is whatever
  the pinned harness serves (owner and operator ETH per account, plus the
  owner's mainnet balance where one is configured), verbatim.
- **It is the scaffold's one by-name dependency on the surface** (D1),
  because only the harness knows the run's wallet addresses: the operator
  keypair is generated inside the harness process and its key never
  leaves it, and `init` has no key path at all (P12). The name is
  asserted at bring-up by `init`'s harness check, which prints a warning
  when the pinned surface does not carry it.
- **Three degradations, all visible.** A harness that raises returns its
  own words, verbatim (D1, I21). A surface without the tool yields the
  loop's ordinary `unknown tool: <name>` result — what any absent name
  yields — so a mis-pin is legible in the session record every session
  instead of showing up as an absence. Neither aborts anything.
- **Skipped entirely, with no telemetry, when no harness is configured:**
  with no surface there is nothing to ask. This is the balance analogue
  of the roster's no-daemon skip.
- The agent may call the balance tool itself at any time. That is its
  business, and it counts as its own behaviour, not the scaffold's.

##### P1.12.3 The plan file

- **Profile `planning` only.** `prompts/planning.txt` tells the agent
  that `workspace/plan.md` is where its goals and plan live, that the
  scaffold shows the file at the start of every session, and that keeping
  it current is up to it (P13). Mechanism, not advice about what to plan.
- **It is an ordinary workspace file.** The agent writes it with
  `workspace_write`; the scaffold never creates, edits, or seeds it
  (P11). The injection is a `workspace_read` of `plan.md` through the
  same tool the agent uses, so the row carries `path` like any file call.
- **A missing file is the normal not-found error result** — visible, and
  the expected shape of session 1 on a fresh run.
- **Contents pass through the normal truncation** at
  `tool_result_max_bytes`, with the re-read hint every truncated
  `workspace_read` gets. An agent that grows its plan to that cap spends
  its own call-1 context on it, on every call of every session. That is
  the agent's decision to make; D1's cap arithmetic names it as a term
  the operator sizes the cap against.

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
  injections' call/result pairs (P1.12) — which the loop synthesizes
  rather than dispatches; from there the alternation is the agent's.
  Error counting, the tool cap, and the repetition breaker all count
  agent-executed intents only.
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
- **Phantom model requests.** Every model request is written ahead as an
  `llm_request` carrying a session-monotonic `request_seq`, before the
  request is sent; the `llm_call` that completes it carries the same
  number. A request with no completion is one the provider may have
  billed and whose outcome nothing recorded — the crash landed between
  them. Recovery writes a synthetic `llm_call` for each, before the
  crash `session_end` so the totals count it, on exactly the terms any
  other failed-but-billed attempt gets: `usage_unknown: true`,
  `cost_usd: 0`, `stop_reason: "error"`, plus `phantom: true`. No usage
  is estimated — the row names a gap, it does not fill one. Idempotent:
  a second pass finds the request completed by the row the first wrote.
  `llm_request` events are never folded into accounting (one exists per
  model call, so counting them would double every total).
- **Residual exposure, stated precisely.** The write-ahead closes the
  window between a request being billed and its outcome being recorded.
  It cannot close the window between the process deciding to send and
  the `llm_request` line reaching disk — a kill inside that interval
  (microseconds, bounded by one `write`+`fsync`) still loses a request
  that may have been billed. Nothing local can observe that case; only
  the provider's own ledger can.

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
(logged at cost 0 with `usage_unknown: true`), retried empty responses
(cost 0, `empty_response: true`), and recovery-written phantoms (cost 0,
`phantom: true`, P3). `llm_request` events contribute nothing. In-world
resources (MUSU, ONYX, gas) are outside `budget_usd` and are not tracked
here.

`usage_unknown` is honest but lossy, and lossy in **two different ways**
that analysis must not merge:

- *Transport-lossy* — the call failed before a response existed
  (timeout, connection failure, 5xx, a rate limit). There is genuinely
  no usage to record, and the provider may or may not have billed it.
- *Normalization-discarded* — a response arrived, **with its usage in
  hand**, and the adapter refused it (an unmappable stop reason,
  unparseable tool arguments, no candidates). The tokens were real and
  are known at that moment; the current implementation discards them and
  records cost 0 anyway, which understates spend by exactly those calls.

Recovering the second class is a behavior change, deliberately out of
scope at this version and named here so it is not mistaken for a bug
report. Both classes reconcile against the provider ledger; neither
reconciles against this scaffold alone.

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
  provider_state?, provider_meta, request_id?}`; `provider_meta` is
  logged raw and never parsed by the loop. `request_id` is the
  provider's own identifier for the call where the SDK serves one (D2),
  never minted by the adapter, and it is also carried on `AdapterError`
  so a failed-but-billed attempt is as traceable as a successful one.
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
- **No exception escapes the call site unrecorded.** A fault the adapter
  did not normalize into an `AdapterError` — an SDK shape it did not
  expect, a fault inside response parsing — is caught on the same terms
  as a non-retryable error: an `llm_call` at cost 0 with
  `usage_unknown: true`, then `reason=errors`. Before 0.4.0 such a fault
  propagated out of the loop and the session died with **no `llm_call`
  row at all**, leaving a billed call invisible to accounting. The catch
  is deliberately broad; the point is that no exception type can
  reintroduce that hole.

### P9. Telemetry event schema — downstream contract

`run/telemetry.jsonl`, one JSON object per line, append-only. Machine
contract: **`schema/telemetry.json`**, JSON Schema draft 2020-12,
`version: 0.5.0`, shipped inside the wheel as package data and kept
byte-identical to the repo copy. Every event is validated **before** it
is written; an invalid event raises and never lands. Unknown fields are
rejected (`unevaluatedProperties: false`), so additive changes require a
schema version bump.

Common required fields on every event: `ts` (ISO-8601 UTC, pattern
enforced), `run_id`, `session`, `event`.

| event | required | optional |
|---|---|---|
| `run_start` | `manifest_hash`, `model`, `harness_sha`, `agent_sha`, `gdd_sha`, `harness_tools[]`, `price_table` | — |
| `session_start` | `trigger` (`scheduled`\|`manual`), `budget_remaining_usd`, `wallclock_elapsed_s`, `tools_hash` | `scaffold_profile`, `presentation_mode`, `harness_tools_hash` |
| `llm_request` | `request_seq` | — |
| `llm_call` | `model`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `cost_usd`, `cumulative_usd`, `cumulative_tokens`, `latency_ms`, `stop_reason`, `retry_count` | `reasoning_tokens`, `usage_unknown`, `continuation`, `empty_response`, `request_seq`, `phantom`, `provider_request_id`, `cache_write_5m_tokens`, `cache_write_1h_tokens` |
| `tool_call` | `tool`, `source` (`harness`\|`scaffold`\|`lens`), `duration_ms`, `ok` | `initiator` (`model`\|`scaffold`), `call_seq`, `path` (file tools), `query` + `hits` (`search_reference`), `error`, `truncated`, `original_bytes`, `skipped`, `tx_hash`, `tx_terminal_state`, `txs[]`, `result_error_shaped`, `provider_call_id`, `provider_call_id_duplicate`, `lens_stale`, `lens_block` |
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
  does not: `source` names the layer that owns the thing called,
  `initiator` names the layer that wanted it run. `model` is every
  intent the agent returned; `scaffold` is the session-start injections
  (P1.12). **Any measure of agent behavior must exclude
  scaffold-initiated calls**: they are reads the agent did not choose,
  and they consume none of the caps that bound what it does. The field is
  optional in the schema so streams written under 0.3.0 and earlier still
  validate; from 0.3.1 on it is emitted on every `tool_call`, so its
  absence in a 0.3.1+ stream is a defect, not a default.
- **`initiator: scaffold` is no longer one row.** Through 0.4.0 it meant
  the roster brief and nothing else, and analysis could use it as that
  filter. From 0.5.0 a session carries two such rows on every profile and
  three on `planning`, so **split on `tool`**: the roster (`source:
  lens`), the balance tool (`source: harness`), and the plan read
  (`source: scaffold`, a `workspace_read` of `plan.md`). A query that
  still reads `initiator=scaffold` as "the brief" silently counts up to
  three different things.
- **`session_end.tool_calls` does not compare across 0.5.0.** Every
  session gained one or two rows no agent chose. Compare agent behaviour
  on `initiator=model` counts, which are unaffected.
- `session_start.scaffold_profile` is the rung the manifest pinned (D3),
  and it is what explains a `tools_hash` that differs between arms of one
  family: a profile at or above `search` carries one more scaffold tool by
  design (P10).
- **`tool_call.call_seq` is the stream's own call identity**, minted by
  the scaffold, one per emitted row, monotonic within a session, skipped
  intents included. Before 0.4.0 telemetry carried no call identity at
  all: two rows of the same tool in one turn were indistinguishable
  without reading the transcript. `provider_call_id` records the id the
  provider supplied, verbatim and **not trusted to be unique** — when
  one repeats inside a turn, `provider_call_id_duplicate` says so (X23).
  **Join rule: pair results to calls by ORDER, on every provider, never
  by id.** This is not hypothetical prudence: a run-004 case that looked
  like a scaffold routing defect — two same-tool calls reported with
  identical results and one id — was adjudicated against the raw
  transcript and found to be an **analysis mis-join**; the recorded
  calls had distinct ids and distinct results, and the loop's routing
  was correct. The identity fields exist so that adjudication never
  again requires a transcript read.
- **`tool_call.ok` is not moved by `result_error_shaped`.** `ok` stays
  exception-keyed: a tool that reports failure by *returning* a body
  with an `error` field still records `ok: true`. The new field names
  that shape without redefining `ok`, which would have silently rewritten
  the meaning of every stream written before 0.4.0. Read `ok`,
  `tx_terminal_state`, and `result_error_shaped` together.
- **`tool_call.tx_hash` now covers the raised path.** Reverted and
  unconfirmed transactions name their hash in the harness's prose; from
  0.4.0 it is lifted onto the field at ingestion, so the hash is no
  longer reachable only by parsing `error` — the one field this section
  tells readers not to parse. Batch errors and validation rejections
  still carry none, because neither is one transaction.
- **`tool_call.txs[]`** carries the per-transaction receipts a
  multi-transaction result reported in band, verbatim and in document
  order, including the step that failed. Those transactions are final
  on-chain regardless of the call's overall outcome; a transaction-keyed
  reconciliation that ignores them comes up short.
- `session_start.harness_tools_hash` is the hash the **harness**
  published of its **own** registry, taken verbatim from the handshake
  (bare hex). It answers a different question over different bytes than
  `tools_hash`, and the two are different by construction: never equate,
  reconcile, or assert them against each other (D1).
- `llm_request` is a write-ahead marker, not a call (P3). It carries no
  usage and **must never be folded into accounting**: one exists per
  model request, so counting them doubles every total. An `llm_request`
  with no matching `llm_call` in a closed session means recovery did not
  run; in a live stream it means the request is in flight.
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
  the `path` of file-tool calls and the `query` / `hits` of
  `search_reference`, promoted so documentation- and memory-access
  patterns are analyzable without transcript parsing. `query` is the
  agent's own text, verbatim; `hits` is how many passages came back (0 to
  `k`). What an agent searched for, and whether the tree answered, is the
  knowledge-delivery family's process observable, and seeing it should not
  require a transcript read.
- **The plan-file injection is a `workspace_read` row with `path:
  plan.md` and `initiator: scaffold`.** A measure of the agent's own file
  access must exclude it — it is one read per session the agent did not
  choose. Plan *churn* is measured on `workspace_write` rows, which are
  always the agent's.
- Quest completions are not logged locally; they are read from chain
  state and joined by timestamp / `tx_hash` in analysis.
- `provider_state` never appears in telemetry.

### P10. Scaffold tools (never part of the harness MCP surface)

The surface is **profile-selected** (D3). The base tools below ship for
every run; a profile at or above `search` adds `search_reference`.

**Base surface (every profile):**

| tool | signature | contract |
|---|---|---|
| `workspace_write` | (path, content) | creates parent dirs, replaces the whole file, `workspace/` only, quota-checked on the projected total |
| `workspace_read` | (path, offset?, length?) | serves `workspace/` and `reference/`; byte-based slicing so truncated results are re-readable |
| `workspace_list` | (path?) | tree with byte sizes; no path → full `workspace/` + one-line `reference/` summary |
| `workspace_delete` | (path) | `workspace/` only |
| `set_next_wake` | (minutes_from_now) | clamped, last call wins (P6) |
| `get_status` | () | JSON with exactly `current_time_utc`, `session_number`, `workspace_bytes_used`, `workspace_quota_bytes` — nothing else |
| `end_session` | (reason: free text) | immediate; reason logged (P2) |

**Profile-added (profiles ≥ `search`):**

| tool | signature | contract |
|---|---|---|
| `search_reference` | (query, k?) | deterministic BM25 keyword search over `reference/`; top-k (default 5, clamped to 1–10) passages, each `{path, offset, length, text}` with BYTE offsets so `workspace_read(path, offset, length)` expands the hit; snippet bounded at 600 chars; an index with no indexable file answers `no reference files` |

`search_reference` in detail, because every number in it is a pinned
artifact of the arms it serves: the index is built lazily, once per
session, from `run_dir/reference` over `.md` / `.markdown` / `.txt` /
`.csv`; chunks are paragraphs packed to ≤ 1200 bytes (a longer paragraph
is cut on a whitespace boundary near that limit) and, for CSV, whole row
groups of 20 rows with the header riding in the first group; tokens are
lowercase `[a-z0-9]+` with no stemming and no stop-word list; scoring is
Okapi BM25 with `k1 = 1.5`, `b = 0.75`, `idf = ln(1 + (N − n + 0.5) /
(n + 0.5))`; ordering is score descending, then `path`, then `offset`.
**Same tree plus same query yields a byte-identical result.** It searches
`reference/` only: the agent's own `workspace/` notes are reachable
through `workspace_list` / `workspace_read` and are not indexed. Its
description is mechanism-only, and it carries no advice about when
searching is worth doing (I3).

**Ordering rule** (unchanged in kind, now stated per profile): game tools
first, then base scaffold tools in declaration order, then profile-added
tools in declaration order. Because `tools_hash` hashes that list *in
order*, appending the additions last means a profile that adds nothing
produces the **same** `tools_hash` as 0.4.0 did at the same harness pin,
and every profile at or above `search` produces exactly one other value.

**So `session_start.tools_hash` differs by profile BY DESIGN**, while the
harness's own registry hash and mass are identical across every arm of a
family (the arms differ in the scaffold, not the environment). Recorded,
expected, and — as always — never equated with the harness's value (D1).

The name-collision check at loop construction runs against the **union**
of the base and every profile's added names, so a harness that registers
one of them is refused identically on every arm rather than only on the
arms whose profile carries it.

The two tables above are the whole scaffold surface. The session-start brief
(P1.12) adds no entry to it, and from 0.4.0 that is true more strongly
than before: the brief is not a tool at all. Its name appears in the
injected assistant turn and in telemetry, never in the tool definitions
sent to the provider, so the surface the model is shown — and with it
`tools_hash` — is unchanged by its existence. A harness that registered
a tool of the brief's name is refused at loop construction
(`ValueError`), before any model call, on the same terms as a
scaffold-name collision: one name meaning two things is a mis-pin.

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
├── prompts/          # frozen assets: system.txt, kickoff.txt, continue.txt,
│                     #   orientation.txt, planning.txt (P13)
├── transcripts/      # session-NNNN.jsonl, messages exactly as sent (post-truncation)
└── telemetry.jsonl   # append-only event stream (P9) — source of truth
```

- `kami-agent init --manifest M --run-dir DIR` — validation and
  scaffolding only: copies the manifest, materializes **all five** frozen
  prompt assets (P13, whatever the profile), creates `workspace/` and
  `transcripts/`, runs connectivity checks (chain RPC, mainnet RPC with
  `eth_chainId == 1`, provider API, MCP handshake — which also reports
  whether the pinned surface carries the balance tool, D1), emits
  `run_start`. **There is no key path through
  init**: it never generates, imports, or writes key material.
  `--skip-connectivity` skips the four checks (and leaves
  `run_start.harness_tools` empty).
- `kami-agent run-session --run-dir DIR [--manual]` — one session (P1).
- `kami-agent status --run-dir DIR` — prints the `state.json` summary.
  Operator-facing; never an agent channel.

### P13. Frozen prompt assets

Five files ship per run and every one is byte-frozen. Three are the
strings every session uses; two are **profile appendices**, appended to
the system prompt by the profiles that carry them (D3). `init`
materializes all five regardless of profile, so a run directory can be
inspected against any rung, and a profile whose asset is missing fails
loudly before the session starts (P1 step 8).

| asset | used by | content |
|---|---|---|
| `prompts/system.txt` | every profile | the fixed system prompt |
| `prompts/kickoff.txt` | every profile | the first user message; no dynamic content, no digits |
| `prompts/continue.txt` | every profile | the tool-less-turn continuation |
| `prompts/orientation.txt` | profiles ≥ `orientation` | the core-loop paragraph |
| `prompts/planning.txt` | profile `planning` | what `workspace/plan.md` is and that it is shown each session |

The system prompt states, in order: the situation (autonomous agent,
periodic sessions, tool calls are the only effect, no human reads the
text); the objective (complete as many quests as possible); persistence
(`workspace/` survives, its use and structure are the agent's own);
`reference/` as read-only documentation; the two tool families;
scheduling via `set_next_wake` within the bounds, and that there is no
in-session waiting; that on-chain actions cost gas even when they revert;
and — new at 0.5.0, on **every** profile — that gas is paid in ETH from
the agent's wallets whose balances it is shown at the start of every
session (P1.12.2). No numbers: the balances themselves arrive as a tool
result, never as prompt text.

`orientation.txt` states what the world's core loop *is* — kamis,
harvesting, health, liquidation, MUSU, food, experience, levels, skill
points, quests, gas. Every sentence is a rule of the game; none is a
recommendation. That boundary is the rung's whole point, and the text is
a pinned artifact of the design that varies it: rewording it changes what
those arms were measured on.

`planning.txt` states where the plan file is, that the scaffold shows it
each session, and that keeping it current is the agent's business — the
mechanism of a file, not advice about what to put in it.

Dynamic content is never prompt text. Balances and plan contents are
**injected context** (P1.12), which keeps every prompt asset a fixed
artifact that a byte-exact test can freeze.

**The fixed floor therefore depends on the profile.** Call-1 context
carries the base prompt plus this profile's appendices plus the file
index plus the tool surface plus the injections, so a floor measured on
one rung is not a floor on another. The smoke tier prints the terms
separately — `system_chars`, `orientation_chars`, `planning_chars`,
`balance_chars`, `plan_file_chars` — for exactly that reason, and the
plan-file term is the one the *agent* controls (P1.12.3).

Excluded by construction: budget, cost, tokens, compute limits, run
duration, session caps, forced truncation, the existence of measurement
— and equally, strategy hints, tool-usage advice, memory-structure
suggestions, XML-tag formatting, and vendor-idiomatic phrasing (I5). Gas
and ETH are world facts, not apparatus (P7.4), which is why the leak scan
carves out "cost(s) gas" and nothing else.

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
- **Exactly one tool is depended on by name: the balance tool
  (`get_gas_balance`).** 0.4.0 had none — the brief's `lens_party` call
  was the last one and moving the brief onto the daemon retired it (D7).
  0.5.0 re-introduces one, deliberately and with its cost stated, because
  the session-start gas balances (P1.12.2) have no other possible source:
  the run's wallet addresses are known only to the harness, whose operator
  keypair is generated in-process and whose key never leaves it, while
  this scaffold has no key path (P12) and no account identity of its own
  (D7). A lens query would need a query the pinned daemon does not serve;
  a direct RPC read would need addresses the operator would have to
  hand-copy after an in-run wallet creation, which is state going stale by
  construction.
  The coupling is made safe by being **explicit, asserted early, and
  visibly degrading**, never by being enforced: the name is a module
  constant, `init`'s harness connectivity check reports whether the pinned
  surface carries it (a warning line when it does not), and at runtime an
  absent name yields the loop's ordinary `unknown tool` result in the
  session record rather than a refusal — runtime refusal on surface drift
  stays a non-goal (N10, X21).
  Apart from that one name the scaffold still consumes the surface
  entirely as given, and it still refuses the reverse case: a harness tool
  named like the brief, or like any profile's scaffold tool (P10).
- **Cap arithmetic assumption.** Every call re-sends the system prompt
  (with this profile's appendices, P13), the file index, the entire tool
  surface, and every session-start injection (P1.12) — the roster, the
  gas balances, and on `planning` the plan file.
  That fixed floor must leave room for a session to be more than one
  call: the worst-case first-call floor is assumed **≤ 1/3 of
  `session_token_cap`**. The brief still makes the floor a function of
  the account's roster size, and that number still grows over a run —
  but the compact roster cut both the constant and the per-kami slope by
  close to an order of magnitude against the party report it replaced,
  so roster growth is no longer the term that threatens the assumption.
  It remains a standing measurement, not a one-off. From 0.5.0 the floor
  gained three terms and one of them is not the operator's to bound: the
  appendices are fixed bytes, the balance payload is small and bounded by
  the account count, but **the plan file is the agent's own file and can
  grow to `tool_result_max_bytes`** (default 64 KiB ≈ 16k tokens) on every
  call of every session. Accepted rather than knobbed: it is the agent
  spending its own context on its own plan, and the operator sizes
  `session_token_cap` and `tool_result_max_bytes` knowing it. Floors do
  not compare across profiles. This is **not enforced anywhere in the
  scaffold** — it is an operator sizing obligation on the manifest. The tri-provider smoke tier reports the
  observed floor (`fixed_floor_input_tokens=…`) for that purpose;
  floors do not compare across 0.4.0, in either the brief's content or
  its serialization. A violated assumption degrades quietly into
  single-call sessions; a grossly undersized `session_token_cap`
  relative to the model's context window instead produces
  `reason=errors` sessions that analysis would misread as model failure.
- **The harness's own published identity is recorded.** The MCP
  handshake carries the harness's hash of its own registry in the
  `instructions` field. It is parsed out and recorded on `session_start`
  as `harness_tools_hash`, bare hex, unmodified. This does not weaken
  the never-equate rule above — it strengthens the CI drift check by
  giving it the harness's own claim to compare against the harness's own
  SPEC, while the scaffold's `tools_hash` keeps answering its separate
  question. Absent when the pinned harness publishes nothing.
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

**Per-call provenance served, by provider.** Recorded where it exists,
recorded as absent where it does not — absence is never read as zero.

| provider | per-call request id | cache-lifetime split |
|---|---|---|
| Anthropic | `_request_id` on the response, from the `request-id` header; also on API errors | **yes** — `usage.cache_creation.ephemeral_5m_input_tokens` / `.ephemeral_1h_input_tokens`, recorded as `cache_write_5m_tokens` / `cache_write_1h_tokens` |
| OpenAI | `_request_id` on the response, from the `x-request-id` header; also on API errors | **none** — `prompt_tokens_details` carries `cached_tokens` and `audio_tokens` only |
| Google | `response_id` on the response — a **model** response id, not a transport request id; no header without `include_sdk_http_response`, which would change the pinned request configuration and is deliberately not set. Errors carry none | **none** — `cache_tokens_details` is a per-**modality** breakdown, not a lifetime one |

The Anthropic split is worth having beyond bookkeeping: the adapter
requests 5-minute entries only, so a non-zero 1-hour figure would be a
finding, and recording both makes N5's no-long-TTL claim a measured fact
per call rather than a claim about the request that was sent.

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
| `scaffold_profile` (`control`\|`orientation`\|`search`\|`pushed`\|`planning`) | P10, P13, P1.12.3 — the knowledge-delivery rung: it selects the scaffold surface, the prompt appendices, and the plan-file injection, and it is recorded on every `session_start`. **Validated at `build_run_config`**: an unknown value exits there, before any lock, telemetry, or session number, exactly as an unknown provider does. Absent = `control`. Cumulative: each value implies the ones to its left. `pushed` changes **nothing agent-side** beyond the recorded value — its rung is lens/harness result enrichment behind their own runtime flags, which the manifest records for provenance and the scaffold neither reads nor asserts |
| `presentation_mode` | D1 — passed to the harness child as `PRESENTATION_MODE`, recorded on `session_start`; never validated scaffold-side. Also selects the brief's `noAuthored` request flag (D7), so both read paths ask the daemon for the same composition |
| `harness.{command,args,cwd,env,handshake_timeout_s}` | D1 |
| `lens.{socket_path,timeout_s,enabled}` | D7 — the world-state daemon the session-start brief reads. `socket_path` unset resolves `KAMI_LENS_SOCKET`, then the platform default; `enabled: false` is the only way to run with no brief at all |
| `pins.{agent_sha,harness_sha,lens_sha,gdd_sha}` | `run_start` provenance only — recorded, never verified |

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

### D7. kami-lens daemon socket

New at 0.4.0: the scaffold consumes the world-state daemon **directly**,
for one thing only — the session-start brief (P1.12). Everything else
the agent perceives still arrives through the harness (D1), which reads
the same daemon over the same socket.

- **Wire protocol**: JSON-lines over a unix domain socket. One request
  object per line in — `{"id", "query", "args"?: [string],
  "noAuthored"?}` — one response per line out, either
  `{"id", "ok": true, data, untrusted, meta}` or
  `{"id", "ok": false, "error": {"code", "message"}}`. The envelope is
  returned verbatim minus the transport keys.
- **Socket resolution**: `lens.socket_path`, then `KAMI_LENS_SOCKET`,
  then the daemon's platform default. The harness resolves the same
  three levels for its own reads; a mismatch would point the brief at a
  different daemon than every other read in the run.
- **Operator obligation, and the shape of not meeting it yet.** The
  brief sends no account argument, because the scaffold has no way to
  know which account a run is. The daemon fills it from its own
  configured default operator. Until that is set, the brief **degrades
  visibly every session** with the daemon's own error — which is the
  *expected* early-run shape, not a misconfiguration: provisioning sets
  the default operator once the account exists, which is after the run
  has begun. `init` reports which of the two states it found and fails on
  neither.
- **No client-side retry, ever.** One attempt per session (X21). The
  loop's retry policy covers model calls only, exactly as it does for
  harness tools (D1).
- **Nothing is validated.** The answer is whatever the pinned daemon
  serves. A daemon whose `roster` changed shape produces a brief of that
  shape — recorded, not checked — on the same terms as every harness
  result. The scaffold owns the serialization and the byte cap and
  nothing else.
- **Construction cannot fail.** A client opens no connection until it is
  queried, so an unreachable daemon can never abort a session; it is
  discovered by the brief and degrades there.

---

## Invariants

| # | claim | enforcement |
|---|---|---|
| I1 | No budget, spend, run-duration, cap, or measurement information reaches the agent through any channel — system prompt, prompt appendices, tool descriptions, tool results, error messages, or `get_status`. **In-world resources are not apparatus**: gas and the ETH that pays it are world facts (P7.4), so "cost(s) gas" is carved out of the vocabulary scan and nothing else is; the wallets' ETH balances reach the agent as world state through a tool result, while the dollar budget stays unreachable and `budget_visible` stays pinned false (X10) | `tests/unit/test_prompts.py::test_no_apparatus_or_policy_leaks` (forbidden-vocabulary scan over all **five** frozen assets, with the `\bcosts? gas\b` carve-out), `::test_the_gas_sentence_states_the_resource_and_where_it_is_shown`, `tests/unit/test_profiles.py::test_no_apparatus_leaks_in_any_profiles_tool_strings` (every profile's surface), `tests/unit/test_scaffold_tools.py::test_no_apparatus_leaks_in_agent_visible_tool_strings`, `::test_get_status_exactly_four_fields`; tri-provider smoke re-scans every agent-visible string of a real session |
| I2 | Budget and t_max are checked **only** at session boundaries; no in-flight session is ever terminated for either | single `boundary_check` call site in `runner.run_session`; `tests/unit/test_governor.py` (budget, t_max, precedence, overspend), `tests/unit/test_runner.py::test_budget_boundary_completes_run`, `::test_t_max_boundary` |
| I3 | Zero strategy content in the scaffold, the prompts, or the profile appendices — mechanics and rules only. The orientation appendix states what the core loop *is* and never what to do; `search_reference`'s description states what it searches and never when to search | `tests/unit/test_prompts.py::test_frozen_strings_are_exactly_as_reviewed`, `::test_profile_appendices_are_exactly_as_reviewed` (byte-exact; any reword must be re-frozen in the same commit), `tests/unit/test_search.py::test_the_tool_description_is_mechanism_only` + the I1 scans + review discipline on every agent-visible string |
| I4 | Forced endings are silent: no warning message, no final model call, no tool result, no `tool_call` event for the carried wake | `tests/unit/test_loop.py::test_context_guard_trips_post_call_and_is_silent`, `tests/unit/test_repetition.py::test_trip_is_silent_and_ends_like_tool_cap`, the carried-wake suite (`test_token_cap_carries_final_turn_wake_intent` … `test_cap_without_wake_intent_carries_nothing`) |
| I5 | Frozen strings and code defaults cannot silently diverge (wake bounds), and the packaged copies match the repo copies byte-for-byte — all five prompt assets and the telemetry schema | `tests/unit/test_prompts.py::test_wake_bounds_in_frozen_prompt_match_code_defaults`, `::test_packaged_prompts_match_repo_prompts` (five assets), `::test_init_materializes_every_asset`, `tests/unit/test_telemetry.py::test_packaged_schema_matches_repo_schema`, `::test_schema_resolves_inside_the_installed_package` |
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
| I20 | The agent's only channels are the harness tools, `reference/`, and `workspace/` — the scaffold exposes no web, shell, or other egress. `search_reference` adds a *view* of `reference/`, not a channel: it reads the same read-only tree `workspace_read` already serves | the scaffold tool list is exactly the base seven of P10 plus, per profile, the one added tool (`test_tool_defs_cover_spec_surface`, `tests/unit/test_profiles.py::test_the_surface_per_profile`); network-level closure is operator-owned (see *Unowned*, README) |
| I21 | A harness error reaches the model verbatim — no rewording, no added judgment or advice, no swallowing — and the tool call behind it is dispatched exactly once | `tests/unit/test_loop.py::test_raised_outcome_reaches_the_model_verbatim_and_telemetry_by_field` (whole-message equality against the harness text, per terminal state), `::test_a_raised_outcome_is_executed_once_and_never_retried`, `tests/unit/test_harness_client.py::test_raised_terminal_states_reach_the_caller_verbatim` (through a real MCP child, whose error wrapping the classifier must tolerate) |
| I22 | The three post-broadcast terminal states plus the pre-signing rejection are recorded as distinct field values, and nothing else is ever recorded as one of them | `tests/unit/test_receipts.py` (per-state classification, MCP-wrapped and bare; batch messages never read as the item states they quote; non-transaction errors classify as nothing), `tests/unit/test_telemetry.py::test_every_terminal_state_is_accepted`, `::test_invented_terminal_state_rejected` (closed enum), `tests/unit/test_loop.py::test_scaffold_failures_carry_no_terminal_state`, `::test_reads_carry_no_terminal_state` |
| I23 | The pinned presentation mode reaches the harness child unvalidated and lands on every `session_start`; an unsupported mode is neither normalized nor caught | `tests/unit/test_cli.py::test_presentation_mode_reaches_the_harness_child`, `::test_presentation_mode_is_passed_through_unvalidated`, `::test_unpinned_presentation_mode_sets_nothing`, `::test_explicit_harness_env_still_wins`, `tests/unit/test_runner.py::test_pinned_presentation_mode_lands_on_every_session_start`, `::test_presentation_mode_is_recorded_as_given` |
| I24 | The session-start brief is one daemon query, executed before the first model call, injected verbatim as a tool result, attempted exactly once, separable in telemetry from what the agent chose — and it bounds nothing the agent does | `tests/unit/test_brief.py` — ordering (`test_brief_is_executed_before_the_first_model_call`), whole-message verbatimness (`::test_brief_result_is_injected_verbatim`), special-path closure (`::test_the_brief_is_a_special_path_and_the_agent_cannot_make_it`, `::test_full_per_kami_detail_stays_on_the_harness_surface`, `::test_a_harness_tool_of_the_briefs_name_is_refused_before_any_model_call`), provenance (`::test_brief_is_telemetered_and_marked_scaffold_initiated_from_the_lens`), cap/counter/breaker exclusion (`::test_brief_consumes_no_session_tool_cap`, `::test_a_failed_brief_does_not_advance_the_consecutive_error_counter`, `::test_brief_never_feeds_the_repetition_breaker`), degradation (`::test_a_query_error_is_injected_as_the_daemons_own_words`, `::test_an_unreachable_daemon_is_injected_as_the_minimal_record`, `::test_a_failing_brief_is_attempted_exactly_once`, `::test_no_brief_when_no_daemon_is_configured`); wire protocol against a real socket in `tests/unit/test_lens.py`; end to end through the real CLI against a stand-in daemon in the `cron-smoke` job, and natively per provider in the tri-provider tier |
| I25 | A model request that was sent always leaves a record, whether or not its outcome did — and the write-ahead marker never inflates accounting | `tests/unit/test_loop.py::test_every_model_request_is_written_before_it_is_sent` (asserts the marker is on disk at the moment the request goes out), `::test_each_retry_is_its_own_request`, `::test_write_ahead_markers_never_contribute_to_accounting`, `::test_an_unnormalizable_response_is_recorded_instead_of_escaping`; recovery in `tests/unit/test_runner.py::test_a_request_that_never_completed_is_named_not_lost`, `::test_the_phantom_is_counted_by_the_crash_session_end`, `::test_phantom_recovery_is_idempotent`, `::test_a_completed_request_is_never_called_phantom`; pairing re-asserted per session by `tests/cron_smoke/check_telemetry.py` |
| I26 | Every emitted `tool_call` row is 1:1 with the intent behind it, and a provider that reuses a call id is recorded rather than obeyed or refused | `tests/unit/test_loop.py::test_every_emitted_row_carries_a_monotonic_call_identity`, `::test_skipped_intents_also_get_an_identity`, `::test_provider_call_ids_are_recorded_verbatim`, `::test_a_reused_provider_call_id_is_flagged_and_both_calls_still_execute` (both calls run, in order, with their own arguments, and nothing raises); `check_telemetry.py` asserts `call_seq` is unique and ordered in a real session |
| I27 | Transaction evidence survives into telemetry from both the returned and the raised path, and from every nesting level a multi-transaction payload uses | `tests/unit/test_receipts.py` (`test_raised_revert_and_unconfirmed_yield_their_transaction_hash`, `::test_a_batch_error_yields_no_single_hash`, `::test_a_pre_signing_rejection_has_no_hash_to_report`, `::test_a_hash_quoted_outside_the_contract_clause_is_not_read_as_the_transaction`), `tests/unit/test_harness_client.py::test_per_hop_receipts_are_copied_from_a_top_level_array`, `::test_per_row_receipts_are_copied_from_inside_a_batch_result_list`, `tests/unit/test_loop.py::test_a_reverted_transaction_records_its_hash_on_the_field`, `::test_in_band_receipts_and_error_shaped_results_reach_telemetry` |
| I28 | **The session-start injections run in one fixed order — roster, balances, plan — all before the first model call, each attempted exactly once, each injected verbatim, and none of them bounds anything the agent does** | `tests/unit/test_injections.py::test_the_three_injections_run_in_order_before_the_first_model_call`, `::test_call_seq_covers_the_injections_in_order`, `::test_balances_reach_the_model_verbatim`, `::test_the_balance_call_is_attempted_exactly_once`, `::test_balances_bound_nothing_the_agent_does`, `::test_a_failed_balance_call_does_not_advance_the_error_counter`, `::test_the_plan_injection_bounds_nothing_the_agent_does`; end to end under a cron environment, per profile, in `tests/cron_smoke/check_telemetry.py` |
| I29 | **Every injection degrades visibly rather than vanishing**: a harness that raises is quoted verbatim, a surface without the balance tool yields the ordinary unknown-tool result, a missing plan file yields the ordinary not-found result — and an injection is skipped silently only when its whole source is unconfigured | `tests/unit/test_injections.py::test_a_surface_without_the_balance_tool_degrades_visibly`, `::test_a_failing_balance_call_is_injected_as_the_harness_own_words`, `::test_a_missing_plan_file_is_the_normal_not_found_error`, `::test_no_harness_means_no_balance_injection_and_no_telemetry`, `tests/unit/test_brief.py::test_no_brief_when_no_daemon_is_configured` |
| I30 | **The profile selects the surface and the prompt, and both are byte-exact artifacts**: an unknown rung never starts a run, a rung whose asset is missing never starts a session, a profile below `search` cannot execute the search tool, and a profile that adds no tool hashes exactly as 0.4.0 did | `tests/unit/test_profiles.py::test_an_unknown_rung_fails_before_anything_starts`, `::test_the_surface_per_profile`, `::test_profile_added_defs_come_after_the_base_ones`, `::test_control_and_orientation_hash_exactly_as_the_base_surface_does`, `::test_a_profile_that_adds_a_tool_has_its_own_hash_by_design`, `::test_the_system_prompt_gains_one_appendix_per_rung`, `::test_a_missing_appendix_fails_loudly_and_names_the_rung`, `tests/unit/test_search.py::test_a_profile_below_search_cannot_execute_it_by_name`, `tests/unit/test_runner.py::test_the_profile_lands_on_every_session_start`, `::test_a_mis_provisioned_profile_starts_no_session` |
| I31 | **Reference search is deterministic and its hits are re-readable**: the same tree and query give byte-identical output, ties break on path then offset, spans never overlap or leave their file, and `workspace_read` with a hit's own offset and length returns that passage | `tests/unit/test_search.py::test_same_tree_and_query_give_byte_identical_results`, `::test_ordering_breaks_ties_on_path_then_offset`, `::test_chunk_spans_do_not_overlap_and_stay_inside_their_file`, `::test_hits_are_ordered_and_carry_a_re_readable_span`, `::test_k_is_clamped_to_the_allowed_range`, `::test_an_empty_tree_says_so` |

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
  **Session-start ETH balances are not a reversal of this, and the
  distinction is not a technicality.** `budget_visible` would expose the
  *apparatus'* own accounting — dollars spent against `budget_usd`, a
  quantity that exists only because someone is paying for inference and
  that no player of this world can observe. ETH is the opposite kind of
  fact: a world resource every player holds, which the harness has served
  on its balance tool for many versions and which the agent could already
  read unprompted. Pre-reading it changes *when* the agent sees a world
  fact, not *whether* it can see the apparatus. `get_status` still returns
  exactly its four fields; the dollar budget, the caps, `t_max` and the
  session counts remain unreachable; `budget_visible` remains pinned false
  and un-wired. The honest residue: an agent reasoning about ETH
  burn-down has a weak proxy for run length. Weak by measurement — gas on
  this chain is tiny (one full run's 224 transactions cost 0.00064 ETH),
  so a starting balance is nowhere near a horizon signal — and accepted,
  because the alternative is hiding from the agent the resource its own
  actions consume.
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
- **X20 — the session-start injections consume no cap, and each is
  skipped only when its own source is unconfigured.** (Written for the
  brief, the first of them; from 0.5.0 it governs all three identically —
  the balance read is skipped only when no harness is configured, and the
  plan read only on profiles that do not carry it.) It performs a real read yet
  counts toward neither `session_tool_cap` nor the consecutive-error
  counter nor the repetition breaker: those counters exist to bound what
  the *agent* does, and a read the agent did not choose must not shrink
  its session or end it. The asymmetry is deliberate — the brief still
  emits a `tool_call` event and still counts in
  `session_end.tool_calls`, so nothing is hidden, it is only excluded
  from the caps. The skip condition narrowed at 0.4.0: with no tool
  surface to consult, the only way to learn whether a daemon is there is
  to ask, so a *configured but unreachable* daemon degrades visibly
  rather than vanishing. Silence would make "the brief never ran" and
  "the brief was never wanted" the same reading.
- **X21 — a failed injection is injected as the failure it is and the
  session proceeds.** (Below in the brief's terms; the same rule and the
  same reasoning cover the balance read — whose failure text is the
  harness's own, or the loop's unknown-tool wording when the pinned
  surface lacks the tool — and the plan read, whose failure is the
  ordinary not-found result.) One attempt, no retry, no fallback content, no
  abort. For a query error the record is the daemon's own code and
  message. For a transport failure there is no daemon text to quote, so
  the scaffold composes `{"error": {"code": "LENS_UNAVAILABLE",
  "message": <the OS's own text>}}` — the narrowest thing that can be
  said. That single authored token is a real cost, accepted over the
  alternatives: retrying would make the scaffold do for itself what D1
  forbids it to do for the agent; substituting placeholder *content*
  would put scaffold-authored prose where world state belongs; aborting
  would let an unavailable read-side daemon end sessions that could
  still act. The record is machine-shaped, carries no advice, and is
  frozen and leak-scanned like the prompt assets (P13, I1).
- **X22 — the brief is a special path, and says so.** Through 0.3.2 the
  brief was a call to a general harness tool the agent could equally
  make, and "no special path" was a contract. It is not one any more:
  the roster is read straight from the daemon, the name is not on the
  tool surface, the agent cannot issue the call, and the loop
  synthesizes the call/result pair instead of dispatching it. The
  compaction is worth the exception — the daemon owns compaction, and
  the compact form is what keeps the fixed floor from growing with the
  roster — but the exception is real and is not to be re-described as
  symmetry. What preserves the agent's reach is that the **full**
  per-kami detail stays on `lens_party`, unchanged.
  **This exception covers the roster and nothing else.** The other two
  injections (P1.12.2, P1.12.3) name tools that ARE on the surface, so
  there the scaffold only pre-calls something the agent can call itself —
  0.3.2's symmetry, still true for those two. Do not generalize X22 into
  "scaffold-initiated calls are special paths": one of the three is.
- **X23 — a duplicated provider call id is recorded, never obeyed and
  never refused.** The loop routes results positionally, so a repeated
  id changes nothing about execution: both calls run, in order, with
  their own arguments. It is flagged because anything that joins results
  to calls *by id* is silently wrong for those rows. It does not raise,
  because a provider quirk must not end a session — and it is not
  deduplicated, because N9 forbids that and because the two calls are
  genuinely different intents.
- **X24 — the scaffold surface differs between arms of one experiment,
  and so does `session_start.tools_hash`.** A profile at or above `search`
  is shown one tool the profiles below it are not (P10), which is the
  point of a scaffold-ablation ladder: the delivery mechanism is the
  variable. The consequence is that two arms running the same agent SHA,
  the same harness SHA and the same world record different `tools_hash`
  values, which looks like drift and is not. It is recorded rather than
  suppressed (a single hash across profiles would require hashing a
  surface the model was not shown), and the harness-side hash and registry
  mass stay identical across every arm — so the *environment* is provably
  constant while the scaffold varies.
- **X25 — `pushed` is a recorded value with no agent-side behaviour.** Of
  the five profiles, one changes nothing in this repo: its rung is result
  enrichment inside the lens and the harness, behind their own runtime
  flags. The scaffold records the name so the arm's provenance is
  self-describing, and does not read, verify, or assert the flags — a
  profile naming a rung the VM does not actually carry is a launch-gate
  question, not something this scaffold can detect. It still carries the
  search tool, because the ladder is cumulative.

---

## Non-goals

- **N1** Multi-model roles (executor/optimizer splits).
- **N2** Knowledge packs or calibrated strategy priors of any kind. The
  `orientation` appendix is not one and the line is worth stating: it is
  the world's *rules* (what harvesting does, what experience is for),
  never a policy, a priority, an efficiency claim, or a number to aim at.
  A profile that shipped tactics would be this non-goal broken, not a new
  rung.
- **N3** Mid-session compaction or context summarization. Cross-session
  memory exists only as agent-written `workspace/` files.
- **N4** Self-funding or economic self-sustainability.
- **N5** Long-TTL or cross-session prompt caching, and explicit cache
  APIs on OpenAI/Gemini — their automatic caching is measured, not
  managed. From 0.4.0 the Anthropic half of this is **verified rather
  than asserted**: cache writes are recorded split by entry lifetime, so
  a 1-hour entry would show up as one (D2).
- **N6** Web access, shell access, or any non-harness network channel
  from the agent loop.
- **N7** Any UI.
- **N8** Mid-session budget enforcement (X6), and any agent-visible
  budget channel while `budget_visible` is pinned false.
- **N9** Reordering, deduplicating, or dependency-analyzing parallel
  tool intents; retrying reverted transactions on the agent's behalf.
- **N10** Runtime refusal on harness surface drift (D1) — detection is
  analytical and CI-side. This now covers the one by-name dependency too:
  a pinned surface without the balance tool degrades visibly every session
  and is warned about at `init`, but never refuses to run.
- **N11** Searching anything but `reference/`. `search_reference` indexes
  the documentation snapshot only — not the agent's own `workspace/`
  notes, not its transcripts, not the run record. Retrieval over the
  agent's own history is a later rung's question, and answering it here
  would quietly change what the `search` arm measures.

---

## Changelog

| version | describes | change |
|---|---|---|
| 2.1 | v0.5.1 | `prompts/orientation.txt` gains one rule sentence — "quest objectives count your account's totals across all your kamis" — a stated fact of the world (quest objective progress is tracked per account, not per kami) that the reference client's quest text leaves ambiguous; rules only, no advice; both copies and the frozen literal re-frozen (I3, P13). Nothing else moves; tools_hash per profile unchanged. |
| 2.0 | v0.5.0 | **A manifest-selected `scaffold_profile` makes the scaffold the experimental variable** (D3, P10, P13): five cumulative rungs — `control`, `orientation`, `search`, `pushed`, `planning` — served by ONE agent version, validated at `build_run_config`, recorded on every `session_start`. The surface is profile-selected: profiles at or above `search` carry `search_reference`, a deterministic pure-python BM25 index over `reference/` returning top-k passages with BYTE offsets that `workspace_read` re-reads exactly, whose `query` and `hits` are promoted into telemetry as the family's process observable. Prompt assets become base + **pinned appendices** (`orientation.txt`, `planning.txt`), each byte-frozen and each materialized by `init`; they are read before the harness spawn, so a rung whose asset is missing fails loudly and starts no session (P1 step 8). **Gas visibility on every profile** (P1.12.2): one fixed system-prompt sentence stating that gas is paid in ETH from the agent's wallets and that the balances arrive each session, plus a session-start injection of the harness's own balance tool — which re-introduces exactly one by-name dependency on the surface, argued and bounded in D1 (asserted at `init`, degrading visibly, never refusing), and argued against `budget_visible` in X10 (ETH is a world resource; the dollar budget stays unreachable). **The `planning` profile adds the plan-file surface** (P1.12.3): the agent's own `workspace/plan.md`, re-read at session start through the ordinary tool, missing-file error included, and named in D1's cap arithmetic as the one floor term the agent itself controls. P1.12 is restated as the **session-start injections** — one ordered contract (roster → balances → plan) with shared verbatimness, single-attempt, visible-degradation and cap-exclusion rules — and X22's special-path exception is confined to the roster, since the other two name tools that are on the surface. Consequences for readers: `initiator=scaffold` is no longer a synonym for the brief (split on `tool`), `session_end.tool_calls` does not compare across 0.5.0, and `tools_hash` differs between arms by design (X24) while the harness's own hash and mass stay identical. New deviations X24 (per-profile surface) and X25 (`pushed` is agent-side inert); new non-goal N11 (search covers `reference/` only). Telemetry schema 0.4.0 → 0.5.0: three additive optional fields (`session_start.scaffold_profile`, `tool_call.query`, `tool_call.hits`), no new event types. New invariants I28–I31. |
| 1.9 | v0.4.0 | Consumption of the kami-harness 2.1.0 surface (101 tools) and of the kami-lens 0.3.0 compact roster. **The session-start brief becomes a direct daemon read** (P1.12, new D7): one `roster` query over the daemon's own socket, replacing the harness `lens_party` call. It is now explicitly a special path (X22) — not a tool, not on the surface, not issuable by the agent, refused at construction if a harness registers its name (P10) — and the compaction cuts both the fixed floor's brief term and its slope in roster size by close to an order of magnitude, which is what takes roster growth off D1's cap-arithmetic assumption. Failures degrade to a minimal machine-shaped record, the transport half of which is the one string the scaffold authors and is frozen accordingly (X21, P13). **Telemetry integrity** (P3, P9): every model request is written ahead as `llm_request` and paired by `request_seq`, so a request billed but never completed is recovered as a named `phantom` row instead of vanishing, with the residual window stated exactly; no exception can escape the call site unrecorded (P8); `call_seq` gives the stream its own 1:1 call identity and `provider_call_id`/`provider_call_id_duplicate` record — without obeying or refusing — a provider that reuses an id (X23, I26), the class that once presented as a routing defect and was adjudicated to be an analysis mis-join; `tx_hash` now covers the raised path and `txs[]` carries in-band per-transaction receipts from both nesting levels (I27); `result_error_shaped` names a returned-rather-than-raised failure without moving `ok`. Per-call `provider_request_id` and the Anthropic cache-lifetime split are recorded where served and as absent where not (D2), which makes N5 measured. `harness_tools_hash` records the harness's own published registry hash beside — never equated with — the scaffold's. `usage_unknown`'s two distinct lossy classes are separated in P7.4, with the recoverable one named as out of scope. Telemetry schema 0.3.1 → 0.4.0: one new event type, one widened enum (`tool_call.source` gains `lens`), the rest additive. |
| 1.8 | v0.3.2 | Session-start status brief (P1.12): before the first model call the scaffold calls the pinned surface's general any-operator party report, `lens_party`, for the account's own operator, and injects the result verbatim as a normal tool result — one call covering every owned kami's on-chain state, HP current/total/rate, and cooldown, so orientation is not re-derived from scratch each session. Explicitly not a special path: the same tool stays available to the agent for any account, both invocations share one execution path, and only the new `tool_call.initiator` (`model` \| `scaffold`) separates them (P9). The brief is attempted exactly once and degrades visibly — a failure is injected as the error it is and the session continues (X21) — and it bounds nothing the agent does: no `session_tool_cap`, no error counter, no repetition breaker (X20). No new scaffold tool, so `tools_hash` is unchanged (P10). Telemetry schema 0.3.0 → 0.3.1 (one additive optional field). D1's cap arithmetic restated: the fixed floor now grows linearly with the account's roster size, so it is a standing measurement, not a one-off. |
| 1.7 | v0.3.0 | Consumption of a harness that **raises** confirmed reverts and unconfirmed transactions instead of returning them: harness error messages are contractually verbatim to the model and dispatched once (I21); the transaction outcome is classified once at ingestion into `tool_call.tx_terminal_state`, a closed five-value enum, so analysis splits validation-rejects / reverts / unconfirmed on a field rather than on prose (I22, X18, X19); `tool_call.ok` restated as exception-keyed and harness-dependent, to be read with the new field and never alone; P5.1's error-or-revert note restated as harness-dependent, with knobs unchanged. Harness `presentation_mode` pinned in the manifest, passed to the child unvalidated, and recorded on every `session_start` (I23, X17). `tools_hash` restated as the scaffold's own fingerprint, different by construction from any hash a harness publishes of its own registry. Telemetry schema 0.2.0 → 0.3.0 (two additive optional fields). |
| 1.6 | v0.2.0 (18f75d04) | Converged to a contract registry: Provides / Depends / Invariants / Deliberate deviations / Non-goals / Changelog, every claim verified against the code and paired with its enforcement. Newly stated as contract: the `run.lock` and transcript layout, the closed run-session outcome set, executed-vs-emitted tool-call accounting, `stop_reason: "error"`, the telemetry schema version as a downstream contract, the recorded-not-negotiated harness identity, per-provider caching modes and what accounting assumes of each, the consumed manifest key list, and the sixteen accepted deviations. Narrative, packaging, and CI-tier prose moved to `README.md` / `docs/packaging.md`. |
| 1.5 | — | Repetition breaker as a third forced-ending class; carried execution of a cap-skipped final-turn `set_next_wake`; three system-prompt additions (no human reads the text, no in-session waiting, gas is spent on reverts); workspace-root-relative file paths; empty-response retry semantics; consecutive (not cumulative) identical-call counting. |
| 1.4 | — | Cache-aware token accounting: provider-side prompt-cache usage measured on all three providers, Anthropic caching explicitly requested via `cache_control` request metadata, `cost_usd` cache-aware, price table extended with cache-rate columns. Prompt bytes and agent-visible channels unchanged. |
| 1.3 | — | `init` performs validation and connectivity checks only; no key path through it — operator-wallet creation became an in-run harness tool. |
| 1.2 | — | Opaque provider reasoning state on assistant messages, adapter-owned and same-session. |
| 1.1 | — | CI split into a per-PR recorded-surface gate and a scheduled live-harness tier. |
| 1.0 | — | First implementable specification: budget invisible to the agent, silent forced endings with boundary-checked soft budget, bundled read-only documentation snapshot, no constraint on agent interaction, plus the engineering semantics for cost basis, context guard, parallel-call serialization, and the tool-result cap. |
