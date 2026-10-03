---
module: kami-agent
version: 2.3
describes: v0.6.0
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
   **Pairing check** (D1, new at 0.7.0): a harness whose handshake states
   `schema_version` MAJOR ≥ 4 but whose standing text did not reach the
   runner is refused right here — the child is closed, nothing is
   emitted, no model is called, and `run-session` exits non-zero with a
   plain message. The claimed session number (step 7) is spent; nothing
   else is, exactly as with a missing prompt asset (step 8).
10. **Emit `session_start`** carrying `tools_hash` of the loaded surface,
    the `scaffold_profile` this run pins, when the manifest pins one the
    harness `presentation_mode`, and what the handshake stated: the
    harness's own registry hash, its `schema_version`, and the sha256 and
    length of the standing text this session shows the model (D1, P9).
11. **Build context**: system prompt = the base string + the profile's
    appendices + **the harness's standing text, verbatim, when it sent
    any** (D1) + the file index (full `workspace/` tree with byte sizes,
    `reference/` collapsed to one `N files, N bytes, read-only` line),
    the parts joined by `\n\n`. A harness that sends no standing text
    (every harness before 4.0.0) adds nothing — the prompt is byte for
    byte what 0.6.0 composed.
12. **Kickoff**: the first user message is the frozen constant
    `prompts/kickoff.txt`. No dynamic content, no digits.
13. **Session-start injections** (P1.12, below): the roster, the wallets'
    gas balances, on the `planning` profile the plan file, and the
    previous session's journal entry, each injected as a completed
    call/result pair.
14. **Agent loop** (P2) until a stop reason (P5).
15. **Persist**: `session_end`, the session's journal entry (P15), the
    transcript file, the state cache.
16. **Schedule** (P6): exactly one `schedule_next`.
17. **Release lock** → `session_ran`.

#### P1.12 Session-start injections

Before the first model call — the same point at which the file index is
built into the system prompt (P1.11) — the scaffold performs the reads
below and injects each as a **completed call/result pair**: an assistant
turn carrying the call, then its result. In this order, always:

| # | injection | source | profiles | owner of the answer |
|---|---|---|---|---|
| 1 | compact **roster** | `harness` | all (unless no harness is configured) | the harness's own roster tool (D1) |
| 2 | the wallets' **gas balances** | `harness` | all (unless no harness is configured) | the harness's own balance tool (D1) |
| 3 | the **plan file** `workspace/plan.md` | `scaffold` | `planning` | the agent itself (P11) |
| 4 | the previous session's **journal entry** | `scaffold` | all | the scaffold (P15) |

The journal read is **last**, which is a compatibility decision and not
an ordering preference: appending it leaves the first three at the
positions — and the `call_seq` numbers — they had at 0.5.0.

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

Where they differ: nowhere that matters any more. **Every one of the four
names a tool that is on the surface**, so the scaffold only ever
pre-calls a call the agent could equally make. The roster was the
exception through 0.5.1 and is not one now (X22 retired).

What that costs a reader: `source` no longer separates the injections
either — the roster and the balances are both `harness` — and two of them
are `workspace_read` calls. So **neither `initiator` nor `source` nor
`tool` alone identifies an injection**: split the two harness calls on
`tool`, and the two workspace reads on `path` (P9).

##### P1.12.1 The roster brief

- **The call is the harness's own roster tool, `lens_roster`, with no
  arguments.** One line per kami — on-chain `index`, `state`, and
  `[hp, hpTotal]` — plus the room the account itself is standing in. It
  carries no authored strings at all by the query's design, so its
  `untrusted` path list is empty and its answer is identical in name-free
  mode. The tool's account-index parameter defaults to the daemon's own
  configured default operator, so the scaffold never has to know which
  account a run is — the same reasoning as the balance call's empty
  account label (P1.12.2).
- **It is not a special path, and this is new at 0.6.0.** Through 0.5.1
  the scaffold read the daemon's socket itself and injected the answer
  under a name that was *not on the tool surface*: the model saw a call
  in its own transcript that it could not make. Agents tried to make it
  anyway — a confusion the scaffold manufactured and then charged them a
  failed call for. The pinned harness now serves the same compact roster
  as an ordinary tool, so the injected pair shows a real tool succeeding
  and the agent can re-issue it whenever it likes. X22 is retired with
  the pseudo-name. The **full** per-kami detail — names, HP rate,
  accrual, cooldown, node — still lives on `lens_party`, unchanged.
- **The harness surface must carry it.** This is the scaffold's second
  by-name dependency (D1), and unlike the first it is **required rather
  than degraded**: a surface without `lens_roster` is refused at loop
  construction, before any model call, naming the tool and the harness
  version that serves it. The balance tool degrades visibly because a
  session without gas figures is still a session; a session that cannot
  see its own kamis is a session pointed at the wrong environment, and
  every session of that run would be one. The failure belongs at the
  first wake at the latest, and `init`'s harness check reports it at
  bring-up, where an operator is looking.
- **Its serialization is whatever the pinned harness returns**, verbatim,
  under the P2 byte cap — the daemon envelope, since the harness passes
  it through. The scaffold parses it for exactly two operator-side
  telemetry fields (`lens_stale`, `lens_block`, P9) and best-effort: a
  harness free to change its serialization must never be able to end a
  session over a field nobody depends on.
- **Its failure is the harness's own words, verbatim** (D1, I21), like
  any other tool failure. The scaffold composes nothing for it. Through
  0.5.1 an unreachable daemon had no words to quote, so the scaffold
  authored a minimal record and that record was frozen and leak-scanned
  like a prompt asset; it is gone, and with it the last agent-visible
  string this scaffold wrote.
- It is **skipped entirely** when no harness is configured, leaving no
  telemetry — with no surface there is nothing to ask. That is the same
  skip rule the balance read has, and it replaces the old no-daemon skip.
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
- **Contents are cut at `PLAN_FILE_MAX_BYTES` (8192)**, not at
  `tool_result_max_bytes`, with the ordinary truncation marker and the
  re-read hint every truncated `workspace_read` gets. The injected file is
  re-sent on every call of every session, so before this bound an agent
  could grow its plan to the full 64 KiB result cap and pay for it on
  every call for the rest of the run — the one floor term the operator
  could not size (D1). `prompts/planning.txt` **states the number**, so
  the bound is a known mechanism rather than a trap.
- **The bound is a code constant, deliberately not a manifest knob.** A
  frozen asset cannot state a number an operator is free to change: the
  two would drift and the prompt would lie. This is the wake-bounds
  arrangement exactly (P13, I5) — the constant is the value, the asset
  quotes it, and a unit test refuses to let them diverge. An operator who
  needs a different bound is making a prompt change, which is a version
  bump, which is the intended friction.

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

**The session journal does not weaken this** (P15). It is the one
scaffold-written surface the agent reads, so it is the one place the
silence could be broken from behind: an entry naming `session_end.reason`
would tell a successor that forced endings exist, and from a run of them
what the caps are. The entry therefore carries no ending reason and no
accounting of any kind. The wait tool (P10) is the other half of the same
answer: it removes the cause of the breaker firing on cooldown polling
without disclosing that the breaker exists.

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
- **`wait` is excluded from all three rules, and it is the only
  exclusion.** Every rule keys on repetition being evidence that the agent
  has stopped getting anywhere — but waiting is the one call whose purpose
  is to be issued again while nothing has changed, and sitting out a
  three-minute cooldown in clamped chunks is five identical signatures in
  a row. Counting it would end the session for using the tool exactly as
  specified, which is the failure the tool was added to remove: before it
  existed, polling a value until it changed was the only waiting strategy
  the scaffold offered, and the breaker fired on it. Waits still consume
  `session_tool_cap`, which is what keeps a session bounded — see the
  arithmetic in P10.
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
- `AdapterError` carries `retryable`, `status_code`, `request_id`, and —
  new at 0.6.0 — **`error_type` and `error_text`: the provider's own
  words**. `error_type` is the token the API returned
  (`insufficient_quota`, `invalid_request_error`, `RESOURCE_EXHAUSTED`),
  recorded verbatim and never normalized across providers; `error_text` is
  the human-readable message, and **the message only**. Every SDK's own
  `str(exc)` for a status error embeds the whole response body, and
  quoting a body into a row read in bulk is how a diagnostic field becomes
  an unreadable one — so each adapter extracts the message from the error
  object it was handed. Both are `None` when the failure happened before
  any provider answer existed: a connection reset has no type and no
  message the provider authored, and null is the honest record of that.
  The fields exist because a run-wide provider outage produced 150+ error
  rows carrying a request id and nothing else, and the first question
  after both of the last two incidents — what did the provider say? — was
  unanswerable from the record.
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
`version: 0.7.0`, shipped inside the wheel as package data and kept
byte-identical to the repo copy. Every event is validated **before** it
is written; an invalid event raises and never lands. Unknown fields are
rejected (`unevaluatedProperties: false`), so additive changes require a
schema version bump.

Common required fields on every event: `ts` (ISO-8601 UTC, pattern
enforced), `run_id`, `session`, `event`.

| event | required | optional |
|---|---|---|
| `run_start` | `manifest_hash`, `model`, `harness_sha`, `agent_sha`, `gdd_sha`, `harness_tools[]`, `price_table` | — |
| `session_start` | `trigger` (`scheduled`\|`manual`), `budget_remaining_usd`, `wallclock_elapsed_s`, `tools_hash` | `scaffold_profile`, `presentation_mode`, `harness_tools_hash`, `harness_schema_version`, `harness_standing_text_sha256`, `harness_standing_text_chars`, `lens_version`, `lens_upstream_pin`, `lens_enrich`, `lens_default_operator` |
| `llm_request` | `request_seq` | — |
| `llm_call` | `model`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `cost_usd`, `cumulative_usd`, `cumulative_tokens`, `latency_ms`, `stop_reason`, `retry_count` | `reasoning_tokens`, `usage_unknown`, `continuation`, `empty_response`, `request_seq`, `phantom`, `provider_request_id`, `error_status`, `error_type`, `error_text`, `cache_write_5m_tokens`, `cache_write_1h_tokens` |
| `tool_call` | `tool`, `source` (`harness`\|`scaffold`\|`lens`), `duration_ms`, `ok` | `initiator` (`model`\|`scaffold`), `call_seq`, `path` + `path_resolved` (file tools), `query` + `hits` (`search_reference`), `wait_requested_s` + `wait_actual_s` (`wait`), `error`, `truncated`, `original_bytes`, `skipped`, `tx_hash`, `tx_terminal_state`, `txs[]`, `result_error_shaped`, `provider_call_id`, `provider_call_id_duplicate`, `lens_stale`, `lens_block` |
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
- **`initiator: scaffold` is no longer one row, and from 0.6.0 `tool` no
  longer separates them either.** Through 0.4.0 `initiator: scaffold`
  meant the roster brief and nothing else. From 0.5.0 a session carries
  two such rows on every profile and three on `planning`; from 0.6.0 it
  carries three on every profile and four on `planning`, and **two of them
  are `workspace_read`** — the plan file and the journal entry. Split on
  `tool` first (the roster is `source: lens`, the balance tool is
  `source: harness`), then on **`path`** to tell the two reads apart:
  `plan.md` against `journal/sessions.jsonl`. A query that still reads
  `initiator=scaffold` as "the brief" silently counts up to four different
  things.
- **`session_end.tool_calls` does not compare across 0.6.0 either.** Every
  session gained one more row no agent chose, for the same reason it
  gained one at 0.5.0. Compare agent behaviour on `initiator=model`
  counts, which are unaffected.
- **Read `path_resolved`, not `path`, for which files an agent touched.**
  `path` is the agent's own argument, verbatim — whether it typed a bare
  or a `workspace/`-prefixed path is a fact about the agent and is kept as
  one. `path_resolved` is the file that argument named, run-dir-relative
  and therefore always carrying its root segment. The two differ whenever
  the agent prefixes a path, because exactly one leading `workspace/`
  segment is stripped (P11). This is not a hypothetical distinction: a
  run-006 analysis grouped on `path` and concluded that an arm had split
  its notes across `notes/` and `workspace/notes/` as two parallel trees.
  The archived workspace had ONE tree. The same 102 writes had simply
  been issued 41 times bare and 61 times prefixed, and every
  `workspace_write` row — which has always carried the resolved form —
  agreed. The claim in P11 was true; the field that was read did not
  answer the question being asked of it. `path_resolved` is absent when
  the path did not resolve at all, which is also when the call failed.
- **`wait_requested_s` and `wait_actual_s` appear on `wait` rows only**
  (P10), and are recorded as a pair because a clamp is visible only in
  the difference between them. `wait_actual_s` is measured rather than
  assumed, so time an agent chose to spend waiting is separable from
  provider latency in the same session's wall clock.
- **`error_status` / `error_type` / `error_text` appear together on every
  error-path `llm_call`, with explicit nulls where the provider served
  nothing.** This is a deliberate break from the omit-when-absent
  convention every other optional field on this event follows, and the
  reason is the case they exist for: during an outage a reader has to
  tell "the provider sent no status" from "this row predates the field",
  and omission collapses those two into one. `error_type` holds the
  provider's own token and one vocabulary only — a fault the adapter
  never normalized carries its exception class inside `error_text`
  instead, so nothing in this field is ever a Python class name.
  `error_text` is the message cut to 200 characters; the full-length
  message is in the run's `errors.jsonl` (P14), which is where an
  incident is actually read. Absent entirely on successful calls.
- **The lens provenance fields say which daemon served the session**
  (D7): `lens_version`, `lens_upstream_pin`, `lens_enrich`,
  `lens_default_operator`. `lens_enrich` is the daemon half of the
  `pushed` rung, so an arm pinned to `pushed` against a daemon running
  with enrichment off is now visible in the record rather than only on
  the host. **They are recorded, never asserted**: the scaffold compares
  nothing against the manifest's pins, and absence means *not recorded* —
  never agreement with a pin (N10).
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
- **`harness_standing_text_sha256` / `harness_standing_text_chars`**
  (new at 0.7.0) fingerprint the harness's standing text exactly as this
  session put it in the system prompt — sha256 (bare hex) of its UTF-8
  bytes, and its length in characters, the unit every floor term is
  quoted in (D1, P1.11). They appear together and **only when text was
  injected**: absence means nothing was injected, never that an empty
  text was shown. The text itself is never in telemetry. It is fixed by
  the harness pin **and that harness's own configuration** — it states
  the harness's call time box — so unlike `harness_tools_hash` it moves
  when that configuration does; two arms with equal registry hashes and
  different standing-text hashes were shown different environments.
  `harness_schema_version` is the handshake's own `schema_version` token,
  verbatim — the value the pairing check (D1) acted on — absent when the
  harness states none (kami-harness before 3.0.0).
- `llm_request` is a write-ahead marker, not a call (P3). It carries no
  usage and **must never be folded into accounting**: one exists per
  model request, so counting them doubles every total. An `llm_request`
  with no matching `llm_call` in a closed session means recovery did not
  run; in a live stream it means the request is in flight.
- `tool_call.tx_terminal_state` names the transaction outcome the
  harness reported — `confirmed_success` | `reverted` | `unconfirmed` |
  `validation_rejected` | `batch_error` | `not_executed` (new at 0.7.0)
  — classified once at ingestion so downstream analysis never
  string-matches harness prose. It is **absent** whenever the call was
  not one transaction outcome: reads, scaffold tools, non-transaction
  errors (a blocked nonce lane and a cancelled call included), in-band
  partial batches and multi-step results, and pre-send dry-run skips.
  Absence means *not classifiable as one terminal state*, never
  *succeeded*.

  **What each value means for someone reconciling an arm's gas and
  action counts afterwards.** "On-chain" is whether the hash will ever be
  in a block; "gas" is whether this hash spent any; "action" is whether
  the world changed.

  | value | on-chain | gas | action | how to reconcile it |
  |---|---|---|---|---|
  | `confirmed_success` | mined, status 1 | spent | happened | count it once; the hash is in a block |
  | `reverted` | mined, status 0 | spent (the harness reports `gas_used`) | did not happen | count the gas, not the action; the hash is in a block |
  | `unconfirmed` | not known at report time | maybe | maybe | the only open state: resolve it on chain by its hash. It may still mine later — the harness keeps it in its ledger and a later call may report it in a `notice` |
  | `not_executed` | **never** — proven: its nonce was consumed by another hash (a nonce collision; the message names that hash) or the node dropped it and its nonce was released | none by this hash | did not happen | count neither gas nor action for this hash, and do not look for it on chain. After a nonce collision the CONSUMING hash may well be in a block, signed by this harness or by another sender on the same key — count that one by its own row or on chain, never as this call |
  | `validation_rejected` | nothing signed or sent | none | did not happen | no hash exists |
  | `batch_error` | per item | per item | per item | no single hash: read the per-item outcomes in the message and the rows in `txs[]` |
  | absent | — | — | — | not one transaction outcome: reconcile from `txs[]`, whose rows carry the harness's own per-row `status` — `success`, `reverted`, `unconfirmed`, `dropped` (= `not_executed`) — verbatim |

  `ok` stays exception-keyed beside all of these: a raised not-executed
  transaction is `ok: false`; a returned single-transaction row whose
  `status` is `dropped` is `not_executed` with `ok: true`. The hash a
  `not_executed` row carries in `tx_hash` is the call's own transaction —
  lifted so a hash-keyed reconciliation knows it will never appear in a
  block — never the hash that consumed its nonce. Streams written before
  0.7.0 recorded these as absent.
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
- **The plan-file and journal injections are `workspace_read` rows with
  `initiator: scaffold`**, at `path: plan.md` and
  `path: journal/sessions.jsonl` respectively. A measure of the agent's
  own file access must exclude both — they are reads per session the agent
  did not choose. Plan *churn* is measured on `workspace_write` rows,
  which are always the agent's.
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
| `wait` | (seconds) | blocks for up to `wait_max_seconds` without a model turn, then returns; clamped to `[0, wait_max_seconds]`, NaN/Inf rejected, and the result names the seconds actually slept |

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

`wait` in detail, because it is the first tool this scaffold has added
for the agent's own benefit rather than the experiment's:

- **Every profile carries it**, as a base tool. A ladder that varies how
  documentation reaches the agent must not also vary whether the agent can
  pass time; that would confound the rung with a capability.
- **Why it exists.** The world has cooldowns of roughly 85–185 seconds, and
  the scaffold offered no way to spend wall time inside a session at all.
  The strategies agents were left with were filler tool calls — the origin
  of a large-payload call-waste pattern — and polling one value until it
  changed, which is five identical signatures in a row, which trips the
  repetition breaker. The breaker was firing on the only waiting strategy
  the scaffold offered. `wait` removes the cause; P5.1 excludes it from
  the breaker; and **P5's silence on why sessions end is untouched** —
  nothing about the breaker is disclosed, the reason for it is simply
  gone.
- **It is a cooldown primitive, not a scheduler.** `set_next_wake` remains
  the between-session mechanism (P6), and `wait_max_seconds` is sized far
  below any plausible session length so the two never overlap in purpose.
- **The clamp is stated in the description and visible in the result**,
  which is the `set_next_wake` arrangement exactly: a bound an agent can
  discover by hitting it is better named than found. The description
  carries no advice about when passing time is worth doing (I3).
- **Total waiting is bounded by `session_tool_cap × wait_max_seconds`** —
  50 × 300 s ≈ 4.2 hours at the defaults. The scaffold has no session wall
  clock of its own, so this product IS the bound, and sizing the two knobs
  together is an operator obligation on the manifest, of the same kind as
  D1's cap arithmetic. It is not enforced anywhere.
- **The tool watchdog gives `wait` its own timeout.** `tool_timeout_s`
  exists to catch a hung harness call and defaults below `wait_max_seconds`
  (120 s against 300 s), so a wait running its full clamp would otherwise
  be killed while behaving exactly as specified, counted as a consecutive
  error, and leave its sleeping thread behind. Every other tool keeps
  `tool_timeout_s`.

**Ordering rule** (unchanged in kind, now stated per profile): game tools
first, then base scaffold tools in declaration order, then profile-added
tools in declaration order. Because `tools_hash` hashes that list *in
order*, appending the additions last means every profile at or above
`search` produces exactly one value distinct from the profiles below it.

**The 0.5.0 claim that a no-addition profile hashes exactly as 0.4.0 did
is retired at 0.6.0.** `wait` is appended to the BASE surface, so
`tools_hash` moves on **every** profile at the same harness pin. This is
an expected, recorded consequence of adding a base tool and not drift;
what X24 says about per-profile differences is unchanged, and the
harness's own registry hash and mass are — as always — untouched by
anything on this surface.

**So `session_start.tools_hash` differs by profile BY DESIGN**, while the
harness's own registry hash and mass are identical across every arm of a
family (the arms differ in the scaffold, not the environment). Recorded,
expected, and — as always — never equated with the harness's value (D1).

The name-collision check at loop construction runs against the **union**
of the base and every profile's added names — `wait` included from
0.6.0 — so a harness that registers one of them is refused identically on
every arm rather than only on the arms whose profile carries it.

The two tables above are the whole scaffold surface. None of the
session-start injections (P1.12) adds an entry to it: every one of them
now names a tool that is already there — three of the harness's or this
table's own — so the surface the model is shown, and with it
`tools_hash`, is unchanged by their existence. Through 0.5.1 the roster
brief was the exception and its name was reserved AGAINST the harness;
at 0.6.0 that check inverted into a requirement that the harness serve
it (D1).

### P11. Workspace conventions

- **The agent may write only under `workspace/`**, subject to
  `workspace_quota_bytes` (default 10 MB) measured over the whole tree.
  A rejected write leaves no partial file.
- `reference/` and `journal/` are read-only by construction: writes and
  deletes to either are rejected, not silently ignored.
- **Paths are relative to the workspace root.** A bare `notes.md` and a
  prefixed `workspace/notes.md` name the same file (exactly one leading
  `workspace/` segment is stripped). `reference/...` addresses the
  read-only documentation tree and `journal/...` the read-only session
  journal (P15). Tool descriptions state this.
  **This has been true of every workspace tool since 0.2.0 and remains
  true; the sentence has never needed a fix.** It is worth saying plainly
  because a run-006 analysis reported that an arm had split its notes
  across `notes/` and `workspace/notes/` as two distinct trees, and a
  second arm across `state/` and `workspace/state/`. Both readings were
  wrong. The archived workspaces hold ONE tree each; every
  `workspace_write` event recorded the resolved path and 100% of them
  carried the `workspace/` prefix; the split existed only in
  `tool_call.path`, which is the agent's raw argument and was never the
  resolved file. From 0.6.0 the resolved form is recorded beside it as
  `path_resolved` (P9), and the claim is enforced through all four tools
  by test rather than through the resolver alone.
- Absolute paths, `~`-paths, NUL bytes, `..` escapes, and symlinks
  leaving a root are rejected. Bare paths can never reach run-directory
  internals (`state.json`, `telemetry.jsonl`, `errors.jsonl`, `prompts/`,
  `transcripts/`, `config.yaml`, `run.lock`).
- The scaffold never writes into `workspace/` beyond creating the
  directory; its content is entirely agent-authored and is the only
  thing the AGENT writes that survives between sessions. The scaffold's
  own cross-session writing goes to `journal/`, which the agent can read
  and cannot touch (P15), and `journal/` does not count against
  `workspace_quota_bytes`.

### P12. Run directory and CLI

```
run/
├── config.yaml       # verbatim manifest copy (D3); immutable per run
├── state.json        # scaffold-owned CACHE (P3): session_counter, cumulative_usd,
│                     # cumulative_tokens, next_wake_at, run_status, first_session_at
├── run.lock          # PID + created (P4); absent between sessions
├── workspace/        # agent-owned (P11)
├── reference/        # read-only documentation snapshot (D5)
├── journal/          # scaffold-written session record (P15); agent-readable,
│                     #   agent-unwritable; sessions.jsonl, rolled to a size bound
├── errors.jsonl      # persistent provider-error artifact (P14); NEVER agent-visible
├── prompts/          # frozen assets: system.txt, kickoff.txt, continue.txt,
│                     #   orientation.txt, planning.txt (P13)
├── transcripts/      # session-NNNN.jsonl, messages exactly as sent (post-truncation),
│                     #   plus one provenance key that was never sent (P12 note below)
└── telemetry.jsonl   # append-only event stream (P9) — source of truth
```

- `kami-agent init --manifest M --run-dir DIR` — validation and
  scaffolding only: copies the manifest, materializes **all five** frozen
  prompt assets (P13, whatever the profile), creates `workspace/` and
  `transcripts/`, runs connectivity checks (chain RPC, mainnet RPC with
  `eth_chainId == 1`, provider API, MCP handshake — which also reports
  whether the pinned surface carries the balance tool and how long the
  harness's standing text is, and exits with the pairing message when a
  4.x harness's standing text did not arrive, D1), emits
  `run_start`. **There is no key path through
  init**: it never generates, imports, or writes key material.
  `--skip-connectivity` skips the four checks (and leaves
  `run_start.harness_tools` empty).
- `kami-agent run-session --run-dir DIR [--manual]` — one session (P1).
- `kami-agent status --run-dir DIR` — prints the `state.json` summary.
  Operator-facing; never an agent channel.

**Transcripts carry one key that was never sent.** A session-start
injection is a real assistant turn in context — that is what makes the
model read it as a completed call — but it is not a turn the model
produced, and nothing in the file said so. Both halves of an injected
pair now carry `"initiator": "scaffold"` in the transcript, so anything
counting assistant rows as model turns stops over-counting by the number
of injections (three per session, four on `planning`). No adapter reads
the field and no provider receives it: the bytes sent are identical with
or without it, which is why "messages exactly as sent" survives as a
description of the content and needs this one stated exception.

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
scheduling via `set_next_wake` within the bounds; that on-chain actions
cost gas even when they revert;
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
each session **up to a stated number of bytes**, and that keeping it
current is the agent's business — the mechanism of a file, not advice
about what to put in it. The number is `PLAN_FILE_MAX_BYTES` and the
asset quotes the constant's own value, pinned against drift by a unit
test exactly as the wake bounds are (I5, P1.12.3).

Dynamic content is never prompt text. Balances and plan contents are
**injected context** (P1.12), which keeps every prompt asset a fixed
artifact that a byte-exact test can freeze.

**One part of the system prompt is not a scaffold asset** (new at
0.7.0): the harness's standing text (D1), placed between the profile's
appendices and the file index. It is the harness's own wording, pinned
with the harness and fingerprinted on every `session_start`; it is not
frozen here, not materialized by `init`, and not this repository's to
reword (X31). The unit tier's byte-exact and vocabulary tests cover what
this repository authors; the tri-provider tier scans the standing text
with every other agent-visible string of a real session (I1).

**Three assets moved at 0.6.0**, and each is an era difference that a run
record must disclose rather than absorb:

- `system.txt` **loses two sentences on every profile**: "You cannot wait
  or pause within a session" and the instruction to wait by scheduling a
  wake and ending the session. Both became false the moment `wait` joined
  the base surface (P10), and a frozen prompt that contradicts the tool
  surface is worse than one that says less. Nothing replaces them: the
  tool's own description carries the mechanism, and telling the agent when
  to use it would be advice (I3).
- `orientation.txt` changes one word — skills that "change" a kami's stats
  become skills that "improve" them. Every skill in this world is
  positive, so the value-neutral wording understated a rule of the game
  while reading as if it were hedging one.
- `planning.txt` gains the plan-file byte bound as a stated number
  (P1.12.3).

**The fixed floor therefore depends on the profile.** Call-1 context
carries the base prompt plus this profile's appendices plus the file
index plus the tool surface plus the injections, so a floor measured on
one rung is not a floor on another. The smoke tier prints the terms
separately — `system_chars`, `orientation_chars`, `planning_chars`,
`balance_chars`, `plan_file_chars`, `journal_chars`, and from 0.7.0
`standing_text_chars` — for exactly that reason, and the plan-file term
is the one the *agent* controls (P1.12.3). **Floors do not compare across
0.6.0** in either direction: the base surface gained a tool, the system
prompt lost two sentences, the file index gained a journal line, and
call-1 context gained a fourth injection. **Nor across a harness pin that
moves the standing text**: at 4.0.0 the system prompt gains it while the
tool surface sheds the sentences it replaced, so a floor measured on one
side of that pin says nothing about the other.

Excluded by construction on **every** profile: budget, cost, tokens,
compute limits, run duration, session caps, forced truncation, the
existence of measurement; and XML-tag formatting and vendor-idiomatic
phrasing (I5). Gas and ETH are world facts, not apparatus (P7.4), which
is why the leak scan carves out "cost(s) gas" and nothing else.

Excluded from the `control` profile, and from the rules text of every
profile: strategy hints, tool-usage advice, memory-structure
suggestions. Above `control`, structural additions — an appendix, a
retrieval tool, a file read back at session start — are admissible only
as a named profile whose assets are byte-frozen and pinned (P10, P13):
the profile is the variable under study, and what a run measures is the
system it produces — model, profile, and pinned environment together —
never the model alone.

### P14. Persistent provider-error artifact

`run/errors.jsonl`, one JSON object per line, append-only, written **at
the moment of the error** and flushed and fsynced before the attempt is
recorded anywhere else.

Why it exists rather than being covered by telemetry: the hosts keep no
agent-side journal of their own, and the two questions asked after an
incident — what did the provider say, and how many times — were both
unanswerable from the record twice running. Telemetry is pulled from a
host that may already be gone, the process may die before a pull, and the
`llm_call` copy of the message is cut to 200 characters because those
rows are read in bulk. This file is the copy that survives when nothing
else does.

| field | meaning |
|---|---|
| `ts`, `run_id`, `session` | the same identity every telemetry event carries |
| `request_seq` | pairs the row with its `llm_request` / `llm_call` (P3) |
| `attempt` | the retry index, so a retry storm is countable rather than inferred |
| `retryable` | how the loop classified it (P8) |
| `model` | the model string the manifest pinned |
| `error_status`, `error_type` | the provider's own status and type token (P8) |
| `error_text` | the provider's own message, cut at 4096 characters |
| `request_id` | the provider's identifier for the call, where it serves one |

Three deliberate differences from `telemetry.jsonl` (P9), all in the
same direction — **a diagnostic must never become a failure mode**:

- **No schema, no version.** This is an operator artifact, not a
  downstream contract, so nothing validates a line and nothing can refuse
  one.
- **It never raises.** Opening and every write are best-effort and
  swallow their own failures: a full disk, a read-only mount, an
  unserializable value. A run whose directory refuses this file still
  runs; it simply has no artifact. An error log that can end a session is
  not an error log.
- **It appears only when something failed.** The file is opened on the
  first error, not at session start, so its existence in a run directory
  is itself the answer to "did anything go wrong?" — an empty
  `errors.jsonl` in every run would answer that wrongly.
- **Unbounded**, like `telemetry.jsonl`. The failure it exists to
  document is exactly the one that produces many rows, and truncating
  would discard the middle of an outage; transcripts in the same
  directory are larger by orders of magnitude.

**Never agent-visible** (I1). It sits in the run directory, which no
agent-supplied path can reach (P11), and it is not under `workspace/`,
`reference/` or `journal/`.

### P15. Session journal

`run/journal/sessions.jsonl` — one compact machine-written entry per
session, appended at session end **regardless of what the model wrote for
itself**, and read back to the next session as the fourth session-start
injection (P1.12).

Two failures it answers, both observed:

- **A session that acts and writes nothing makes the agent's own past
  self an unknown actor.** Successors were found attributing their own
  harvest stops, quest completions and item gains to "someone" or
  "something" — reasoning about an adversary that was themselves, one
  session earlier.
- **Elapsed time was imperceptible from inside.** A 17-hour provider
  outage produced not one sentence about elapsed time in any arm; it
  surfaced only later, as starving kamis. Nothing in context said any
  time had passed, because nothing in context ever mentions time between
  sessions.

**The entry.** Facts only, mechanism-only wording, no summary and no
judgement about what happened:

| field | meaning |
|---|---|
| `session` | the session number |
| `started_at`, `ended_at` | UTC, taken from the session's own `session_start` / `session_end` rows |
| `seconds_since_previous_session_end` | the gap. **Absent** when there is no previous entry — zero would claim the sessions were adjacent |
| `tools` | name → count, over the agent's OWN executed calls (`initiator: model`) |
| `tx_hashes` | transaction hashes the session's results carried, in order, deduplicated, capped at 20 with `tx_hashes_total` naming the true count when cut |
| `roster` | the roster the session opened on, verbatim from the brief. Absent on a recovered entry (below) |

**What it must never contain**, and this is the load-bearing half:

- **No `session_end.reason`.** P5's forced endings are silent, and this
  is the one scaffold-written surface the agent reads — an entry naming
  the reason would disclose from behind exactly what that silence
  protects, and a run of them would give away the caps themselves.
- **No accounting of any kind**: no budget, spend, tokens, `llm_calls`,
  caps or `t_max` (I1). Elapsed wall time is *not* apparatus: `get_status`
  already serves `current_time_utc`, so how much time passed is
  world-observable.
- The entry's own key names are scaffold-authored agent-visible strings
  and are leak-scanned as the prompt assets are (I1). They are, at 0.6.0,
  the ONLY agent-visible text this scaffold composes: the last other one
  — the unreachable-daemon record — retired with the direct daemon read
  (X21).

**Tool counts and the inference channel, stated rather than left
implicit.** A per-session executed-call total that lands on the same
number every time would let an agent infer that a per-session tool cap
exists. The counts are recorded anyway, because this is not a new
channel: an agent can already count its own calls within a session from
its own context, so the journal saves it bookkeeping rather than telling
it something otherwise unavailable. The judgement is written down here so
that it is visible and can be reversed on evidence — CI's leak scan runs
over frozen strings, not over runtime content, and will never catch this
one.

**Retention.** The file is rolled to `journal_max_bytes` (default 32768)
by dropping whole oldest entries, newest always kept even if it alone
exceeds the bound. The default is chosen against one number: it is below
`tool_result_max_bytes`, so reading the WHOLE journal in a single call is
never truncated. The agent can always see all of what was kept.

**Derived, never authoritative.** `telemetry.jsonl` remains the source of
truth (P3, I7); this is a view of it maintained for the agent, which is
what makes trimming it for retention legitimate rather than lossy.

**Delivery and discoverability.** `journal/` is a third read-only tree
beside `workspace/` and `reference/` (P11), served by `workspace_read`
and `workspace_list`, rejected by `workspace_write` and
`workspace_delete`, and outside `workspace_quota_bytes`. It is named in
exactly one place: the **file index** in the system prompt (P1.11), as a
listed path with its byte size, the way every workspace file is listed.
No prompt asset mentions it and nothing tells the agent to read it —
a path and a byte count are facts, and advice is not the scaffold's to
give (I3). The session-start injection then puts the last entry in front
of the agent whether or not it goes looking.

**The injection is an ordinary `workspace_read`** carrying that entry's
own byte offset and length, so it is a call the agent could make itself —
as every injection now is (X22) — and it demonstrates the slicing form
without a word about using it. Session 1 has no file and gets the ordinary
not-found result — the same visible shape the plan read has on a fresh
run (P1.12.3, X21).

**Crashed sessions are journaled by recovery** (P3), idempotently, from
the folded telemetry stream. Without this the next entry's elapsed figure
would silently span two sessions, which is the one number the journal
exists to make honest. A recovered entry carries no `roster`: the brief's
answer lived only in a transcript, and a crashed session never wrote one.

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
- **Identity is recorded, not negotiated.** There is no version
  negotiation: the handshake's `schema_version` token is read for exactly
  one decision — the pairing check below — and is otherwise recorded.
  Two artifacts stand in for identity:
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
  unconfirmed transactions, and — from kami-harness 4.0.0 — transactions
  the harness proved will never execute (`not_executed`) on a field. The
  classification is
  observation only: it changes nothing the agent sees, and an
  unrecognized message is recorded as no state rather than guessed at.
- `tx_hash` is extracted best-effort from structured content or JSON
  text, top level or one `result` level down, for telemetry only.
- **Two tools are depended on by name, on DIFFERENT terms.** The balance
  tool (`get_gas_balance`) is depended on and allowed to be absent; the
  roster tool (`lens_roster`) is depended on and **required**. The
  difference is what each absence would produce: a session without gas
  figures still acts, so a missing balance tool degrades visibly every
  session and is warned about at `init` (N10, X21); a session that cannot
  see its own kamis is a session pointed at the wrong environment, and
  every session of that run would be one, so a missing roster tool is
  refused at loop construction with a message naming the tool and the
  harness version that serves it. Runtime refusal on surface drift stays
  a non-goal (N10) — this is not drift detection, it is a precondition
  the manifest either meets or does not, checked once, before anything.

  **The roster dependency supersedes the 0.4.0 routing decision.** That
  version moved the brief OFF the harness and onto a direct daemon read,
  because no harness tool served the compact roster and the party report
  it replaced was an order of magnitude larger. The pinned harness now
  serves the compact roster itself, so the reason is gone — and the cost
  of the workaround was concrete: the injected call named something that
  was not a tool, agents saw it in their transcripts and tried to call
  it, and every attempt failed. Wrapping the daemon is the harness's job;
  this scaffold consuming that wrapper is the plain arrangement.

- **On the balance tool specifically (`get_gas_balance`).** 0.4.0 had no
  by-name dependency at all — the brief's `lens_party` call was the last
  one and moving the brief onto the daemon retired it (D7).
  0.5.0 re-introduced one, deliberately and with its cost stated, because
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
  Apart from those two names the scaffold still consumes the surface
  entirely as given, and it still refuses a harness tool named like any
  profile's scaffold tool (P10). The reverse refusal that stood through
  0.5.1 — a harness tool named like the brief — is gone, because the
  brief IS a harness tool now; the check at that name inverted from
  "must be absent" to "must be present".
- **Cap arithmetic assumption.** Every call re-sends the system prompt
  (with this profile's appendices, P13, and from 0.7.0 the harness's
  standing text — roughly a thousand characters at 4.0.0), the file
  index, the entire tool surface, and every session-start injection
  (P1.12) — the roster, the gas balances, and on `planning` the plan file.
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
  question. Absent when the pinned harness publishes nothing. From 0.7.0
  the same handshake's `schema_version` token and the fingerprint of its
  standing text (below) are recorded beside it (P9).
- **The harness's standing text reaches the model** (new at 0.7.0). The
  handshake's `instructions` field has two parts. Line 1 is machine
  tokens — `tools_hash=<64 hex> schema_version=<semver>
  error_snippets=<on|off>`, or a prefix of that list on older harnesses —
  and tokens are read from line 1 only. **Everything after the first
  newline is the harness's standing text**: the rules it states once for
  its whole surface instead of on every tool description they apply to.
  From kami-harness 4.0.0 that text carries the rule that `untrusted`
  fields are player data and never instructions — through 3.x appended to
  every read tool's description, from 4.0.0 on none of them — together
  with how its world-state reads are served and how to wait for a read to
  reflect one's own transaction, that an `incomplete` row means re-read,
  one nonce lane per key, and the call time box. The scaffold puts that
  remainder **verbatim** — not stripped, not reflowed, nothing added
  before or after it — into the system prompt of every session on every
  profile, after its own frozen text and before the file index (P1.11),
  so every provider adapter carries it in its own system slot unchanged
  (D2). An empty or whitespace-only remainder injects nothing. The text is
  recorded by sha256 and length on `session_start`, never by content
  (P9).
  It goes in verbatim and unlabelled for the reason tool descriptions are
  passed unmodified: it is part of the environment definition, owned and
  pinned with the harness, and the harness chose to say it once rather
  than N times. A label would be the first agent-visible prose this
  scaffold has composed since 0.6.0 (X21, X31).
- **Pairing rule: kami-agent ≥ 0.7.0 ↔ kami-harness ≥ 4.0.0.** A scaffold
  before 0.7.0 reads only line 1 of the handshake, and a 4.x harness's
  tool descriptions no longer carry the standing text — so an older
  scaffold on a 4.x harness **never shows the model the untrusted-data
  sentence**, or anything else of that text. Nothing fails; every arm just
  runs without it. In the other direction this scaffold runs unchanged
  against a harness before 4.0.0: there is no remainder, nothing is
  injected, and the prompt is byte for byte what 0.6.0 sent. The scaffold
  enforces the half it can see: when the handshake states
  `schema_version` MAJOR ≥ 4 and the runner received **no** standing text
  — the field was stripped, the build is broken, or a wrapper around the
  client forwarded the version and dropped the text — it **refuses to
  start** (P1 step 9) with a plain message instead of running degraded,
  and `init`'s harness check refuses the same pairing at bring-up. A
  harness that states no `schema_version` (before 3.0.0) or a MAJOR below
  4 owes no text and passes. Like the roster requirement, this is a
  precondition checked once per session, not drift detection (N10).
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
| `caps.wait_max_seconds` | P10 — upper bound on ONE `wait` call (default 300). Total in-session waiting is `session_tool_cap × this`; size them together |
| `caps.journal_max_bytes` | P15 — rolling bound on the session journal (default 32768, below `tool_result_max_bytes` so a whole-file read never truncates) |
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

New at 0.4.0 for the session-start brief; from 0.6.0 the scaffold
consumes the world-state daemon **directly** for exactly one thing, the
operator-side provenance read described at the end of this section. The
brief moved onto the harness surface (P1.12.1, D1), so **nothing the
agent perceives comes through this socket any more.** Everything else
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
- **One provenance query, operator-side** (new at 0.6.0). Before
  `session_start` is emitted the scaffold sends one `status` query — a
  general query on the daemon's own registry, argument-free, answered
  during bootstrap as readily as when live — and records
  `lens_version`, `lens_upstream_pin`, `lens_enrich` and
  `lens_default_operator` on that event (P9).

  It exists because **a run's live daemon version was recorded nowhere**.
  The manifest pins a lens SHA that nothing verifies, so a host serving a
  different build than its pin claimed was invisible in the record — the
  last run's version scramble had to be caught by a gate on the VM while
  the run was still up, which is not something a record can do afterwards.
  The brief's own envelope cannot answer it: its `meta` carries block
  number, staleness and mode, and no identity at all.

  `lens_enrich` is the daemon half of the `pushed` rung, which changes
  nothing agent-side in this scaffold (X25) — so an arm pinned to `pushed`
  against a daemon running with enrichment off used to be undetectable
  from telemetry. It is now two facts in the record for analysis to
  compare. **Recorded, never asserted**: nothing is checked against the
  manifest and runtime refusal on drift stays a non-goal (N10).

  It is **never an agent channel**: not injected, not a `tool_call` row,
  not in any transcript — `session_start` is telemetry (P9, I1). It
  **degrades to nothing**: one attempt, no retry (X21), every failure
  swallowed, and absence read as *not recorded* rather than as agreement
  with the pin. Nothing about it is validated; a daemon serving a shape
  this scaffold does not recognize contributes whatever fields it did
  serve.

---

## Invariants

| # | claim | enforcement |
|---|---|---|
| I1 | No budget, spend, run-duration, cap, or measurement information reaches the agent through any channel — including the two surfaces added at 0.6.0: the `wait` tool's strings and **the session journal**, whose entries carry no ending reason (the P5 silence contract) and no accounting of any kind (P15) — system prompt, prompt appendices, tool descriptions, tool results, error messages, or `get_status`. **In-world resources are not apparatus**: gas and the ETH that pays it are world facts (P7.4), so "cost(s) gas" is carved out of the vocabulary scan and nothing else is; the wallets' ETH balances reach the agent as world state through a tool result, while the dollar budget stays unreachable and `budget_visible` stays pinned false (X10) | `tests/unit/test_prompts.py::test_no_apparatus_or_policy_leaks` (forbidden-vocabulary scan over all **five** frozen assets, with the `\bcosts? gas\b` carve-out), `::test_the_gas_sentence_states_the_resource_and_where_it_is_shown`, `tests/unit/test_profiles.py::test_no_apparatus_leaks_in_any_profiles_tool_strings` (every profile's surface), `tests/unit/test_scaffold_tools.py::test_no_apparatus_leaks_in_agent_visible_tool_strings`, `::test_get_status_exactly_four_fields`, `tests/unit/test_journal.py::test_the_entry_never_names_the_apparatus`, `tests/unit/test_wait.py::test_the_description_is_mechanism_only`; tri-provider smoke re-scans every agent-visible string of a real session, the harness's standing text included (0.7.0) |
| I2 | Budget and t_max are checked **only** at session boundaries; no in-flight session is ever terminated for either | single `boundary_check` call site in `runner.run_session`; `tests/unit/test_governor.py` (budget, t_max, precedence, overspend), `tests/unit/test_runner.py::test_budget_boundary_completes_run`, `::test_t_max_boundary` |
| I3 | Zero strategy content in the scaffold, the prompts, or the profile appendices — mechanics and rules only. The orientation appendix states what the core loop *is* and never what to do; `search_reference`'s description states what it searches and never when to search | `tests/unit/test_prompts.py::test_frozen_strings_are_exactly_as_reviewed`, `::test_profile_appendices_are_exactly_as_reviewed` (byte-exact; any reword must be re-frozen in the same commit), `tests/unit/test_search.py::test_the_tool_description_is_mechanism_only` + the I1 scans + review discipline on every agent-visible string |
| I4 | Forced endings are silent: no warning message, no final model call, no tool result, no `tool_call` event for the carried wake | `tests/unit/test_loop.py::test_context_guard_trips_post_call_and_is_silent`, `tests/unit/test_repetition.py::test_trip_is_silent_and_ends_like_tool_cap`, the carried-wake suite (`test_token_cap_carries_final_turn_wake_intent` … `test_cap_without_wake_intent_carries_nothing`) |
| I5 | Frozen strings and code defaults cannot silently diverge (wake bounds), and the packaged copies match the repo copies byte-for-byte — all five prompt assets and the telemetry schema | `tests/unit/test_prompts.py::test_wake_bounds_in_frozen_prompt_match_code_defaults`, `::test_packaged_prompts_match_repo_prompts` (five assets), `::test_init_materializes_every_asset`, `tests/unit/test_telemetry.py::test_packaged_schema_matches_repo_schema`, `::test_schema_resolves_inside_the_installed_package` |
| I6 | Telemetry is append-only and crash-consistent: one line per event, validated before write, `write → flush → fsync` before the action it describes is complete; a crash loses at most the event being written | `TelemetryWriter.emit`; `tests/unit/test_telemetry.py::test_append_only_ordering`, `::test_appends_across_writer_instances`, and the rejection suite (`unknown event`, `missing required`, `bad enum`, `wrong type`, `extra field`, `non-UTC ts`) proving invalid events never land |
| I7 | Telemetry is the source of truth for accounting; `state.json` is a cache rebuilt by folding the stream, and a crashed session is closed exactly once | `tests/unit/test_state.py::test_fold_recomputes_accounting_from_the_stream`, `::test_crashed_session_detected`, `tests/unit/test_runner.py::test_crash_recovery_writes_synthetic_end_and_refolds_accounting`, `::test_crash_recovery_is_idempotent` |
| I8 | Every stop reason is enumerated and telemetered — the `session_end.reason` enum is closed and each value has a producing path | schema enum + `tests/unit/test_telemetry.py::test_schema_covers_exactly_the_spec_events`, `::test_bad_enum_value_rejected`; producers covered by `test_loop.py` (agent, token_cap, tool_cap, errors), `test_repetition.py` (repetition), `test_runner.py` (crash, harness-abort errors) |
| I9 | Cron-env parity: a session behaves identically under a cron-like environment and a manual start | the `cron-smoke` CI job — one full `init` + `run-session` under `env -i PATH=/usr/bin:/bin HOME=…`, absolute interpreter path, no provider keys, real exit codes, followed by `tests/cron_smoke/check_telemetry.py` (exactly one `session_start`/`session_end` pair, expected reason, exactly one agent-source `schedule_next`, every event re-validated) |
| I10 | `input_tokens` is the total prompt count and the cache fields are components of it, never additions; `output_tokens` always includes reasoning tokens | per-adapter tests: `test_anthropic_adapter.py::test_usage_folds_cache_components_into_total_input`, `test_openai_adapter.py::test_cached_prompt_tokens_are_a_component_not_an_addition`, `test_google_adapter.py::test_cached_content_tokens_are_a_component_not_an_addition`, `::test_the_reasoning_token_fold`, `test_governor.py::test_cache_zero_reduces_exactly_to_v0_formula` |
| I11 | No agent-supplied path escapes `workspace/`, `reference/` or `journal/`, and run-directory internals — `errors.jsonl` included — are unreachable; **and a bare path and a `workspace/`-prefixed one name the same file through every workspace tool, not only through the resolver** | `tests/unit/test_sandbox.py` — escapes, one-segment stripping, run-dir internals, symlink escape, a Hypothesis property test over arbitrary segments, and the tool-level pair `::test_the_two_spellings_are_one_file_through_every_workspace_tool`, `::test_a_subtree_listing_agrees_across_both_spellings`; `tests/unit/test_error_diagnosability.py::test_the_artifact_is_not_reachable_by_any_agent_path` |
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
| I22 | The post-broadcast terminal states — confirmed, reverted, unconfirmed, and from 0.7.0 proven not executed — plus the pre-signing rejection are recorded as distinct field values, and nothing else is ever recorded as one of them | `tests/unit/test_receipts.py` (per-state classification, MCP-wrapped and bare; batch messages never read as the item states they quote; non-transaction errors classify as nothing; `::test_not_executed_is_neither_unconfirmed_nor_reverted`, `::test_not_executed_lifts_its_own_hash_and_never_the_one_that_consumed_its_nonce`, `::test_a_returned_dropped_row_is_not_executed`, `::test_dropped_rows_inside_a_multi_transaction_payload_are_no_single_state`), `tests/unit/test_shape_tolerance.py::test_a_raised_not_executed_transaction_is_recorded_as_one`, `::test_a_returned_dropped_row_is_recorded_as_not_executed`, `tests/unit/test_telemetry.py::test_every_terminal_state_is_accepted`, `::test_invented_terminal_state_rejected` (closed enum), `tests/unit/test_loop.py::test_scaffold_failures_carry_no_terminal_state`, `::test_reads_carry_no_terminal_state` |
| I23 | The pinned presentation mode reaches the harness child unvalidated and lands on every `session_start`; an unsupported mode is neither normalized nor caught | `tests/unit/test_cli.py::test_presentation_mode_reaches_the_harness_child`, `::test_presentation_mode_is_passed_through_unvalidated`, `::test_unpinned_presentation_mode_sets_nothing`, `::test_explicit_harness_env_still_wins`, `tests/unit/test_runner.py::test_pinned_presentation_mode_lands_on_every_session_start`, `::test_presentation_mode_is_recorded_as_given` |
| I24 | The session-start brief is one call of the harness's own roster tool, executed before the first model call, injected verbatim as a tool result, attempted exactly once, separable in telemetry from what the agent chose — and it bounds nothing the agent does. **A surface without that tool starts no session** | `tests/unit/test_brief.py` — ordering (`test_brief_is_executed_before_the_first_model_call`), whole-message verbatimness (`::test_brief_result_is_injected_verbatim`), no-special-path (`::test_the_brief_names_a_tool_the_agent_can_call_itself`, `::test_full_per_kami_detail_stays_on_the_harness_surface`, `::test_no_arguments_are_sent_so_the_daemon_fills_the_account_in`), the requirement (`::test_a_surface_without_the_roster_tool_is_refused_before_any_model_call`), provenance (`::test_brief_is_telemetered_and_marked_scaffold_initiated_from_the_harness`, `::test_brief_records_the_freshness_of_what_it_injected`, `::test_an_unparseable_roster_costs_nothing`), cap/counter/breaker exclusion (`::test_brief_consumes_no_session_tool_cap`, `::test_a_failed_brief_does_not_advance_the_consecutive_error_counter`, `::test_brief_never_feeds_the_repetition_breaker`), degradation (`::test_a_harness_failure_is_injected_as_the_harness_own_words`, `::test_a_failing_brief_is_attempted_exactly_once`, `::test_no_brief_when_no_harness_is_configured`, `::test_an_oversized_brief_is_capped_like_any_tool_result`); end to end through the real CLI against a stand-in harness in the `cron-smoke` job, and natively per provider in the tri-provider tier |
| I25 | A model request that was sent always leaves a record, whether or not its outcome did — and the write-ahead marker never inflates accounting | `tests/unit/test_loop.py::test_every_model_request_is_written_before_it_is_sent` (asserts the marker is on disk at the moment the request goes out), `::test_each_retry_is_its_own_request`, `::test_write_ahead_markers_never_contribute_to_accounting`, `::test_an_unnormalizable_response_is_recorded_instead_of_escaping`; recovery in `tests/unit/test_runner.py::test_a_request_that_never_completed_is_named_not_lost`, `::test_the_phantom_is_counted_by_the_crash_session_end`, `::test_phantom_recovery_is_idempotent`, `::test_a_completed_request_is_never_called_phantom`; pairing re-asserted per session by `tests/cron_smoke/check_telemetry.py` |
| I26 | Every emitted `tool_call` row is 1:1 with the intent behind it, and a provider that reuses a call id is recorded rather than obeyed or refused | `tests/unit/test_loop.py::test_every_emitted_row_carries_a_monotonic_call_identity`, `::test_skipped_intents_also_get_an_identity`, `::test_provider_call_ids_are_recorded_verbatim`, `::test_a_reused_provider_call_id_is_flagged_and_both_calls_still_execute` (both calls run, in order, with their own arguments, and nothing raises); `check_telemetry.py` asserts `call_seq` is unique and ordered in a real session |
| I27 | Transaction evidence survives into telemetry from both the returned and the raised path, and from every nesting level a multi-transaction payload uses | `tests/unit/test_receipts.py` (`test_raised_revert_and_unconfirmed_yield_their_transaction_hash`, `::test_a_batch_error_yields_no_single_hash`, `::test_a_pre_signing_rejection_has_no_hash_to_report`, `::test_a_hash_quoted_outside_the_contract_clause_is_not_read_as_the_transaction`), `tests/unit/test_harness_client.py::test_per_hop_receipts_are_copied_from_a_top_level_array`, `::test_per_row_receipts_are_copied_from_inside_a_batch_result_list`, `tests/unit/test_loop.py::test_a_reverted_transaction_records_its_hash_on_the_field`, `::test_in_band_receipts_and_error_shaped_results_reach_telemetry` |
| I28 | **The session-start injections run in one fixed order — roster, balances, plan, journal — all before the first model call, each attempted exactly once, each injected verbatim, and none of them bounds anything the agent does** | `tests/unit/test_injections.py::test_the_four_injections_run_in_order_before_the_first_model_call`, `::test_call_seq_covers_the_injections_in_order`, `::test_balances_reach_the_model_verbatim`, `::test_the_balance_call_is_attempted_exactly_once`, `::test_balances_bound_nothing_the_agent_does`, `::test_a_failed_balance_call_does_not_advance_the_error_counter`, `::test_the_plan_injection_bounds_nothing_the_agent_does`; end to end under a cron environment, per profile, in `tests/cron_smoke/check_telemetry.py` |
| I29 | **Every injection degrades visibly rather than vanishing**: a harness that raises is quoted verbatim, a surface without the balance tool yields the ordinary unknown-tool result, a missing plan file yields the ordinary not-found result — and an injection is skipped silently only when its whole source is unconfigured | `tests/unit/test_injections.py::test_a_surface_without_the_balance_tool_degrades_visibly`, `::test_a_failing_balance_call_is_injected_as_the_harness_own_words`, `::test_a_missing_plan_file_is_the_normal_not_found_error`, `::test_no_harness_means_no_balance_injection_and_no_telemetry`, `tests/unit/test_brief.py::test_no_brief_when_no_harness_is_configured`, `tests/unit/test_journal.py::test_the_first_session_gets_the_ordinary_not_found_result` |
| I30 | **The profile selects the surface and the prompt, and both are byte-exact artifacts**: an unknown rung never starts a run, a rung whose asset is missing never starts a session, a profile below `search` cannot execute the search tool, and a profile that adds no tool hashes exactly as 0.4.0 did | `tests/unit/test_profiles.py::test_an_unknown_rung_fails_before_anything_starts`, `::test_the_surface_per_profile`, `::test_profile_added_defs_come_after_the_base_ones`, `::test_control_and_orientation_hash_exactly_as_the_base_surface_does`, `::test_a_profile_that_adds_a_tool_has_its_own_hash_by_design`, `::test_the_system_prompt_gains_one_appendix_per_rung`, `::test_a_missing_appendix_fails_loudly_and_names_the_rung`, `tests/unit/test_search.py::test_a_profile_below_search_cannot_execute_it_by_name`, `tests/unit/test_runner.py::test_the_profile_lands_on_every_session_start`, `::test_a_mis_provisioned_profile_starts_no_session` |
| I31 | **Reference search is deterministic and its hits are re-readable**: the same tree and query give byte-identical output, ties break on path then offset, spans never overlap or leave their file, and `workspace_read` with a hit's own offset and length returns that passage | `tests/unit/test_search.py::test_same_tree_and_query_give_byte_identical_results`, `::test_ordering_breaks_ties_on_path_then_offset`, `::test_chunk_spans_do_not_overlap_and_stay_inside_their_file`, `::test_hits_are_ordered_and_carry_a_re_readable_span`, `::test_k_is_clamped_to_the_allowed_range`, `::test_an_empty_tree_says_so` |
| I32 | **A failed model call records what the provider said, in two places.** The provider's status, type and message land on the `llm_call` row — with explicit nulls where the provider served none — and a longer copy lands in `run/errors.jsonl` at the moment of the failure, which never raises into the session and is unreachable by any agent path | `tests/unit/test_error_diagnosability.py` — per-provider extraction (`::test_anthropic_carries_the_type_and_the_message_without_the_body_echo`, `::test_openai_carries_the_quota_type_the_incident_needed`, `::test_google_uses_its_canonical_status_as_the_type`, `::test_a_transport_failure_has_no_provider_type_and_says_so`), the row (`::test_a_failed_call_records_what_the_provider_said`, `::test_the_three_fields_are_present_as_nulls_when_the_provider_served_nothing`, `::test_a_successful_call_carries_none_of_them`, `::test_an_unnormalized_fault_keeps_its_class_out_of_the_type_field`), the artifact (`::test_the_artifact_is_written_at_the_moment_of_the_error`, `::test_every_retry_leaves_its_own_row`, `::test_the_artifact_never_raises_into_the_session`, `::test_the_artifact_is_not_reachable_by_any_agent_path`) |
| I33 | **`wait` blocks within its bound, is clamped rather than rejected, is recorded as a requested/actual pair, is exempt from the repetition breaker and from nothing else, and still consumes `session_tool_cap`** | `tests/unit/test_wait.py` — surface and wording (`::test_it_is_on_the_base_surface_of_every_profile`, `::test_the_description_is_mechanism_only`), clamping (`::test_a_request_over_the_bound_is_clamped_and_the_clamp_is_visible`, `::test_a_negative_request_clamps_to_zero`, `::test_non_finite_and_non_numeric_requests_are_errors`), telemetry (`::test_both_durations_are_recorded_so_waiting_is_analyzable`, `::test_a_wait_that_never_reached_the_handler_records_no_durations`), and the two consequences (`::test_repeated_waits_never_trip_the_repetition_breaker`, `::test_a_repeated_non_wait_call_still_trips_it`, `::test_waits_still_consume_the_session_tool_cap`, `::test_the_watchdog_gives_wait_its_own_bound`, `::test_a_wait_longer_than_the_tool_timeout_still_completes`) |
| I34 | **Every session is journaled whatever the model writes, the entry names no ending reason and no accounting, the file is bounded so a whole read never truncates, and the last entry reaches the next session as an ordinary re-readable slice** | `tests/unit/test_journal.py` — the entry (`::test_the_entry_carries_the_facts_a_successor_needs`, `::test_no_previous_entry_means_no_elapsed_figure_rather_than_zero`, `::test_the_entry_never_names_the_apparatus`, `::test_a_flood_of_transactions_cannot_crowd_out_every_other_session`), retention (`::test_retention_drops_oldest_entries_and_keeps_the_file_readable_whole`, `::test_the_newest_entry_is_kept_even_when_it_alone_exceeds_the_bound`, `::test_has_session_makes_a_second_write_detectable`), the tree (`::test_the_journal_is_readable_and_not_writable`, `::test_the_file_index_names_the_journal_with_its_size`, `::test_the_journal_does_not_count_against_the_workspace_quota`), the injection (`::test_the_last_entry_is_injected_as_a_readable_byte_slice`, `::test_the_first_session_gets_the_ordinary_not_found_result`, `::test_the_journal_injection_bounds_nothing_the_agent_does`) |
| I35 | **Which lens daemon served a session is recorded on every `session_start`, never asserted against the manifest, and never reaches the agent** | `tests/unit/test_lens_provenance.py::test_the_serving_daemons_identity_lands_on_session_start`, `::test_the_enrichment_flag_makes_a_mis_provisioned_rung_detectable`, `::test_a_daemon_that_cannot_answer_costs_nothing`, `::test_a_daemon_serving_a_shape_we_did_not_expect_records_what_it_can`, `::test_no_daemon_means_no_provenance_and_no_query`, `::test_provenance_is_never_an_agent_visible_channel` |
| I36 | **The harness's standing text reaches the model verbatim on every session, on every profile and through every provider adapter; it is fingerprinted, never copied, on `session_start`; a harness that sends none changes nothing; and a 4.x harness whose text did not arrive starts no session** | `tests/unit/test_standing_text.py` — the field (`::test_line_one_alone_still_parses_and_carries_no_text`, `::test_a_hash_only_handshake_still_parses`, `::test_the_remainder_after_the_first_newline_is_the_standing_text_verbatim`, `::test_the_remainder_is_not_reflowed_or_stripped`, `::test_tokens_are_read_from_line_one_only`, `::test_the_client_exposes_the_handshake_of_a_real_child`), the prompt (`::test_the_standing_text_is_in_the_system_prompt_of_every_profile`, `::test_no_text_means_the_prompt_is_exactly_what_it_was`, `::test_the_text_is_on_every_call_of_the_session`, `::test_the_text_never_enters_the_tool_surface_or_its_hash`), the wire (`::test_every_provider_adapter_sends_it_in_its_system_slot`, three real adapters × five profiles), the record (`::test_session_start_records_the_texts_fingerprint_and_size`, `::test_no_text_records_no_fingerprint_but_still_the_version`, `::test_the_text_is_never_in_telemetry_itself`), the refusal (`::test_a_4x_harness_whose_text_did_not_arrive_starts_no_session`, `::test_a_wrapper_that_drops_the_text_is_refused`, `::test_only_that_combination_is_refused`, `::test_bring_up_refuses_the_same_pairing_with_the_same_plain_message`, `::test_run_session_exits_with_the_plain_message_not_a_traceback`); end to end under a cron environment against a stand-in serving a 4.x handshake (`tests/cron_smoke/check_telemetry.py`); natively per provider in the tri-provider tier once the recorded surface carries the text |
| I37 | **Result shapes new at harness 4.0.0 / lens 1.0.0 neither crash nor mis-record**: incomplete rows, `INCOMPLETE` / `NOT_APPLIED`, time-boxed loop results, a `notice` first key, and the not-executed / lane-blocked / cancelled outcomes reach the model verbatim and are recorded as no terminal state rather than a wrong one | `tests/unit/test_shape_tolerance.py` (classification, hash lifting, receipts and the journal roster on each shape, through a session) |

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
  session proceeds.** One attempt, no retry, no fallback content, no
  abort. The failure text is always someone else's: the harness's own
  words for the roster and the balance reads (or the loop's unknown-tool
  wording when the pinned surface lacks the balance tool), and the
  ordinary not-found result for the plan and journal reads. Retrying
  would make the scaffold do for itself what D1 forbids it to do for the
  agent; substituting placeholder *content* would put scaffold-authored
  prose where world state belongs; aborting would let a read failure end
  sessions that could still act.
  **At 0.5.1 there was one exception and it is gone.** An unreachable
  daemon had no words to quote, so the scaffold composed
  `{"error": {"code": "LENS_UNAVAILABLE", "message": <the OS's own
  text>}}` — one authored token, frozen and leak-scanned like a prompt
  asset. With the roster on the harness surface the harness owns that
  failure text too, so the scaffold now authors **no agent-visible
  string at all**.
- **X22 — RETIRED at 0.6.0. The brief has no special path any more.**
  Kept as a numbered entry because two versions of run records were
  written under it and a reader of those records needs to know what
  changed. Its history in one line each: through 0.3.2 the brief was a
  call to a general harness tool and "no special path" was a contract;
  at 0.4.0 it became a direct daemon read under a name that was not on
  the tool surface, and the exception was real and stated; at 0.6.0 the
  pinned harness serves the compact roster as an ordinary tool, so the
  brief is a scaffold-initiated call of a real tool and the exception is
  gone. **Every session-start injection now names a tool the agent could
  call itself**, which is 0.3.2's symmetry restored without giving up
  the compaction that made 0.4.0 worth doing — the daemon still owns
  compaction, the harness just wraps it. The confusion the exception
  cost is the reason it is not coming back: agents repeatedly tried to
  call the pseudo-tool they saw in their own transcripts, and every one
  of those attempts was a failed call the scaffold had set them up for.
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

- **X26 — the wait tool is exempt from the repetition breaker, and it is
  the only exemption.** Every other tool, including one the agent polls
  productively, is counted (X1, X2). `wait` is not, because its contract
  is to be issued again while nothing has changed: counting it would end a
  session for using the tool exactly as specified. The asymmetry is
  deliberate and bounded — waits still consume `session_tool_cap`, which
  is what keeps a session finite, and the product of that cap and
  `wait_max_seconds` is the real bound on in-session waiting (P10).
- **X27 — the three provider-error fields are emitted with explicit
  nulls, against the omit-when-absent convention every other optional
  `llm_call` field follows.** X18 made the opposite choice for
  `tx_terminal_state`, and for the opposite reason: there, an explicit
  `unknown` would have been indistinguishable from a classification
  failure, so absence forces the honest reading. Here, absence is the
  ambiguous option — during an outage "the provider sent no status" and
  "this row predates the field" are different facts, and omitting the key
  collapses them. Both choices follow the same rule (record the
  distinction the reader will need) and land in different places because
  the readers need different things.
- **X28 — a transcript carries one key that was never sent.** Injected
  session-start pairs are marked `"initiator": "scaffold"` (P12). This
  bends "messages exactly as sent", and the alternative was worse: a
  synthesized assistant turn is byte-identical to a model turn in the
  file, so anything counting model turns over-counted by the number of
  injections, and separating them required joining against telemetry. No
  adapter reads the key and no provider receives it, so the bytes sent are
  unchanged.
- **X29 — the journal records what the agent could already have counted.**
  Per-session tool counts let a successor infer that a per-session cap
  exists if the totals saturate. Accepted, because an agent can already
  count its own calls inside a session from its own context; the journal
  changes the effort, not the availability. Stated in P15 rather than left
  implicit precisely because no automated scan will ever catch it — the
  leak scans run over frozen strings, and this is runtime content.
- **X30 — `journal/` is scaffold-written state the agent reads, which
  0.5.0 had none of.** Until 0.6.0 the only thing surviving between
  sessions and visible to the agent was agent-authored (`workspace/`) or
  pinned at provisioning (`reference/`). The journal is neither: the
  scaffold writes it, every session, unconditionally. It is admitted as a
  scaffold FLOOR rather than a knowledge rung — every profile carries it,
  so it cannot confound the ladder — and it is bounded in the two ways
  that matter: it states facts the world already exposes (times, its own
  calls, its own transactions) and never the apparatus (P15, I1).
- **X31 — the system prompt carries text this scaffold did not write and
  does not freeze.** From 0.7.0 the harness's standing text sits between
  the scaffold's frozen assets and the file index (P1.11, D1). Its wording
  is the harness's: a reword moves `harness_standing_text_sha256`, not
  anything this repository pins, and the byte-exact asset tests (I3, I5)
  do not see it. Accepted because the alternatives are worse — restating
  the rules in the scaffold's own frozen words would duplicate a contract
  that can drift and put scaffold-authored prose about another
  component's surface in front of the model, and labelling the text would
  be the first agent-visible prose this scaffold has composed since 0.6.0.
  It is the arrangement tool descriptions have always had (D1), applied
  to text the harness now says once instead of on every description.

---

## Non-goals

- **N1** Multi-model roles (executor/optimizer splits) — absent from
  every profile at this version. A later profile's question, admitted
  the way any rung is (named, frozen, measured against `control`), not
  a principle.
- **N2** Knowledge packs or calibrated strategy priors in any profile
  shipped at this version. The `orientation` appendix is not one and the
  line is worth stating: it is the world's *rules* (what harvesting does,
  what experience is for), never a policy, a priority, an efficiency
  claim, or a number to aim at. Tactics could only ever arrive as a NEW
  named profile measured against `control` — never as a change to an
  existing rung, never as the default; at this version no such profile
  exists.
- **N3** Mid-session compaction or context summarization. Cross-session
  memory the agent AUTHORS exists only as `workspace/` files; the journal
  (P15) is not a counter-example and must not be read as one — it is a
  mechanical record of what happened, never a summary of what it meant,
  and no consolidation mechanism exists at this version.
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
  and is warned about at `init`, but never refuses to run. The pairing
  refusal (D1, 0.7.0) is not an exception: it checks a precondition the
  handshake states about itself — that a 4.x harness's standing text
  arrived — once per session, as the roster requirement does, and
  compares nothing against a pin.
- **N11** Searching anything but `reference/`. `search_reference` indexes
  the documentation snapshot only — not the agent's own `workspace/`
  notes, not its transcripts, not the run record. Retrieval over the
  agent's own history is a later rung's question, and answering it here
  would quietly change what the `search` arm measures.

---

## Changelog

| version | describes | change |
|---|---|---|
| 2.3 | v0.6.0 | **Diagnosability, a time primitive, a memory of its own sessions, and the roster brief back on the tool surface.** *Cause: the brief's injected call named something that was not a tool.* Through 0.5.1 the scaffold read the compact roster off the world-state daemon's socket itself and injected the answer under a name the tool surface did not carry — a call the agent could see in its own transcript and could never make, which agents repeatedly tried to make anyway. The pinned harness now serves the same compact roster as an ordinary tool, so the brief becomes a **scaffold-initiated call of `lens_roster`**, dispatched through the same path as any intent the agent returns, recorded as a normal `tool_call` with `initiator: scaffold` and `source: harness`, and re-issuable by the agent at will. This **supersedes the 0.4.0 routing decision** that moved the brief off the harness in the first place: that decision existed because no harness tool served the compact roster and the party report it replaced was an order of magnitude larger — wrapping the daemon is the harness's job, and it does it now. Consequences: **X22 is retired** (no injection is a special path any more; every one of the four names a tool the agent could call itself), the reserved-name check at `lens_roster` **inverts** from "the harness must not serve this" to "the harness must" and refuses loop construction otherwise — the second by-name harness dependency (D1), and the first that is required rather than degraded, because a session that cannot see its own kamis is a session pointed at the wrong environment; `source` no longer separates the injections (roster and balances are both `harness`); the daemon socket (D7) survives for the operator-side provenance query alone; and the scaffold now authors **no agent-visible string at all** — the frozen unreachable-daemon record retires with the direct read. *Cause: two runs in a row, the first question after an incident — what did the provider say? — was unanswerable from the record; a 14-hour run-wide outage produced zero diagnosable bytes and 150+ error rows carrying only a request id.* `AdapterError` gains `error_type` / `error_text`, all three adapters extract the provider's own token and message (never the SDK's body-echoing `str(exc)`), the `llm_call` row carries status/type/message with **explicit nulls** where the provider served none (X27), and a persistent **`run/errors.jsonl`** artifact (new **P14**) is written at the moment of failure with a longer message cut, no schema, and a writer that swallows its own failures — because the hosts keep no agent-side journal and a telemetry pull can come too late or not at all. *Cause: agents needed to sit out 85–185 s cooldowns mid-session and had no way to pass wall time, so they burned calls on filler and polled until the repetition breaker — firing on the only waiting strategy the scaffold offered — ended the session.* A new BASE-surface **`wait(seconds)`** tool (P10, every profile) blocks without a model turn, clamped to `caps.wait_max_seconds` (300) on the `set_next_wake` pattern, recorded as a requested/actual pair (P9), exempt from the repetition breaker and from nothing else (P5.1, X26), given its own watchdog bound because `tool_timeout_s` defaults below the clamp, and bounded in total by `session_tool_cap × wait_max_seconds` — an operator sizing obligation, stated. **P5's silence on why sessions end is untouched: the cause is removed, not disclosed.** *Cause: sessions that acted but wrote nothing made the agent's own past self an unknown actor — successors attributed their own harvest stops and item gains to "someone" — and a 17-hour outage was invisible to every arm, with not one sentence about elapsed time anywhere in context.* A machine-written **session journal** (new **P15**): one compact entry per session appended whatever the model writes, carrying session number, start/end, **elapsed since the previous session's end**, the agent's own tool counts, transaction hashes and the opening roster — and carrying **no ending reason and no accounting**, because this is the one scaffold-written surface the agent reads and P5's silence would otherwise leak from behind it. It lives in a third read-only tree `journal/` (P11), is named ONLY in the dynamic file index and in no prompt (I3), rolls to `caps.journal_max_bytes` (32768, below `tool_result_max_bytes` so a whole-file read never truncates), is written for crashed sessions by recovery (P3), and its last entry is injected at session start as an ordinary byte-sliced `workspace_read` — the **fourth** P1.12 injection, appended last so the first three keep their positions and `call_seq` numbers. *Cause: a run's live daemon version was recorded nowhere, and the last run's version scramble needed a VM-side gate to catch.* One operator-side `status` query (D7) records `lens_version`, `lens_upstream_pin`, `lens_enrich` and `lens_default_operator` on every `session_start` — never injected, never a tool_call row; `lens_enrich` makes a `pushed` arm running against a flag-off daemon detectable from telemetry for the first time (X25 partially closed), recorded and never asserted (N10). *Cause: a run-006 analysis concluded one arm had split its notes across two parallel trees; the archived workspace held one tree, and the field it grouped on was the agent's raw argument.* P11's one-segment-stripping claim **was true at 0.5.1 and stays** — no fix was needed, and the SPEC says so plainly; the field, not the behavior, was the defect, so `tool_call.path_resolved` now records what a path actually named beside what the agent typed (P9), and the claim is enforced through all four workspace tools rather than the resolver alone (I11). *Cause: the plan file is re-sent on every call of every session and could grow to the 64 KiB result cap — the one floor term the operator cannot size.* `PLAN_FILE_MAX_BYTES` (8192) bounds the plan injection, stated as a number in `prompts/planning.txt` and pinned to the constant by test, a code constant rather than a manifest knob because a frozen asset cannot quote a number an operator may change (P1.12.3, P13, I5). Three prompt assets move and each is an era difference: `system.txt` loses "You cannot wait or pause within a session" and its how-to-wait sentence on **every** profile (they became false when `wait` landed), `orientation.txt` says skills "improve" rather than "change" a kami's stats (every skill in this world is positive), `planning.txt` states the plan bound. Transcripts mark injected pairs `"initiator": "scaffold"` — one key that was never sent (P12, X28). **Consequences for readers:** `tools_hash` moves on EVERY profile (a base tool was added), so the 0.5.0 "hashes exactly as 0.4.0" claim is retired; floors do not compare across 0.6.0 in either direction; `session_end.tool_calls` does not compare across it either; `initiator=scaffold` now covers three rows per session (four on `planning`) and **two of them are `workspace_read`**, so split on `path`. Telemetry schema 0.5.0 → 0.6.0: nine additive-optional fields, no new event types. New invariants I32–I35; new deviations X26–X30; N3 restated so the journal is not misread as consolidation. |
| 2.2 | v0.5.1 | Docs-only. The contract states what a run measures: the **system** — model, `scaffold_profile`, pinned environment — not the model alone. "Policy free" is restated per configuration (the scaffold never plays for the agent), while structure is a named, frozen, pinned profile; the P13 exclusion paragraph splits into every-profile apparatus/vendor items vs `control`-and-rules-text advice items; N1/N2 restated as version-scoped, not principled. No code, asset, schema, or hash moves. |
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
