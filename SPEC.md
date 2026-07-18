# kami-agent — Reference Scaffold Specification (v1.4)

Status: **v1.4 — approved for implementation** (v1 superseded the v0
draft; §13 open decisions resolved as D12–D15, engineering semantics
fixed as D16–D19; v1.1 amended §11 per D21 — CI smoke split into a
per-PR recorded-surface gate and a scheduled live-harness tier; v1.2
amended §5.1 per D22 — opaque provider reasoning state on assistant
messages; v1.3 amends §10 per D27 — init performs validation and
connectivity checks only, operator-wallet creation is a harness tool
the agent calls in-run; v1.4 amends §5.1–5.2, §8, §9, §12 per D39 —
the caching-neutral clause of D16 is superseded: provider-side
prompt-cache usage is measured on all three providers, Anthropic
caching is explicitly requested via `cache_control` request metadata,
and `cost_usd` is cache-aware; prompt bytes and every agent-visible
channel are unchanged (D12 intact) — see kami-lab `DECISIONS.md`)
Scope: the model-agnostic reference agent scaffold for KamiBench controlled studies.
Companion repos: `kami-harness` (environment interface, MCP), `kamigotchi-gdd` (world
documentation), `kami-lab` (experiment orchestration — private).

---

## 1. Purpose and design principles

kami-agent turns a stateless model API into a persistent actor in the Kamigotchi
world. It is deliberately minimal: its scientific value comes from being boring.

1. **Mechanism fixed, policy free (D6).** The scaffold fixes *how* the agent can
   act, remember, and schedule itself. It never fixes *what* to do, *what* to
   remember, or *when* to act (within bounds). All strategy, memory content,
   memory structure, and pacing decisions belong to the model under test.
2. **Model-agnostic by construction.** One loop, N provider adapters. Native tool
   calling per provider. No vendor idioms in prompts or loop logic. A tri-provider
   smoke test gates every change.
3. **Session-based, not daemon-based.** Persistence = state on disk + a scheduler,
   not a long-running process. Crash recovery reconstructs accounting from the
   telemetry stream (§7.1), never from in-memory state.
4. **Everything is logged.** If it isn't in the telemetry stream or on-chain, it
   didn't happen. Post-hoc analysis (memory divergence, discovery coverage,
   spend curves) depends on complete capture. Telemetry events are flushed to
   disk synchronously, one line per event, before the action they describe is
   considered complete.
5. **No compaction in v0 (D8).** The session context guard (§9) sits below every
   candidate model's context window. Cross-session memory exists only as
   agent-written workspace files. This removes compaction quality as a
   cross-model confound and makes "memory" fully inspectable.
6. **The agent is blind to the experimental apparatus (D12, D13).** No budget,
   spend, run-duration, or session-cap information reaches the agent through
   any channel — not the system prompt, not `get_status`, not error messages.
   Forced endings are silent (SIGKILL semantics). The only persistence hint the
   agent ever receives is the system prompt's statement that `workspace/`
   survives between sessions.
7. **Closed world (D14).** The agent's total information channels are: the
   harness MCP tools, the bundled read-only `reference/` tree, and its own
   `workspace/`. No web access, no shell, no other network egress from the
   agent loop. (Provisioning enforces this at the VM level: egress allowlist =
   model provider API + chain RPC used by the harness.)

## 2. Architecture

```
supervisor (cron @ poll cadence + lockfile)
  └─ session runner (one process: start → run one session → persist → exit)
       ├─ model adapter        (anthropic | openai | google)
       ├─ harness client       (MCP client → kami-harness stdio child, pinned SHA)
       ├─ scaffold tools       (workspace, reference, scheduling, status)
       ├─ budget governor      (boundary checks, pinned price table)
       ├─ state store          (run/ directory on disk)
       └─ telemetry            (JSONL event stream + transcripts)
```

- **Supervisor** is a fixed-cadence poller: cron fires every `poll_cadence`
  (default 5 min); the runner exits immediately unless `now ≥ next_wake_at`
  and no other session holds the lock. Consequence: effective wake resolution
  is `poll_cadence`, so `wake_min ≥ poll_cadence`.
- **Lockfile** contains PID + timestamp. A lock whose PID is dead or whose age
  exceeds `lock_stale_s` (default 2 × session wall-clock ceiling) is stale and
  is broken with a logged warning. A crashed session can never deadlock the run.
- **Harness lifecycle**: the kami-harness MCP server is spawned per session as
  a stdio child at the pinned SHA. Handshake failure aborts the session
  (`session_end reason=errors`, zero model calls); next wake = `wake_default`.

## 3. Session lifecycle

1. **Acquire lock** (with staleness handling per §2). If held, exit.
2. **Recover.** If the previous session crashed (telemetry shows a
   `session_start` without matching `session_end`), write a synthetic
   `session_end` with `reason: crash` for it, and recompute `cumulative_usd` /
   `cumulative_tokens` by folding `telemetry.jsonl` — `state.json` is a cache,
   telemetry is the source of truth for accounting.
3. **Boundary checks (D13).** If `cumulative_usd ≥ budget_usd` or wall-clock
   since the first `session_start` ≥ `t_max_days`: write `run_complete`
   (reason `budget` | `t_max`), disable the supervisor, release lock, exit.
   Budget and t_max are checked **only here** — an in-flight session is never
   terminated for budget or t_max; overshoot is bounded by the session caps
   and the exact $100 analysis line is drawn post-hoc from per-call
   cumulative-spend telemetry.
4. **Start session.** Increment and persist `session_counter` (before the
   first model call, so crashes never reuse a session number). Emit
   `session_start`. Spawn harness child, perform MCP handshake, load game
   tools.
5. **Build context.** Fixed plain-text system prompt (§6) + the file index:
   full `workspace/` tree (paths, sizes) + `reference/` as a single top-level
   entry (name, file count, total size, read-only marker). The agent reads
   contents via tools.
6. **Kickoff.** The first user message is a frozen constant string
   (`prompts/kickoff.txt`, e.g. "Session start."). No numbers, no dynamic
   content — time, session number, and world state are all discoverable via
   tools, and whether the agent looks is measured behavior.
7. **Agent loop.** Alternate model calls and tool executions (§5.3) until one
   of:
   - agent calls `end_session`
   - context guard trips: last call's `input_tokens + output_tokens ≥
     session_token_cap` (D17)
   - `session_tool_cap` tool executions reached
   - `max_consecutive_errors` consecutive errors (§5.4)
   - model call fails after all retries (§5.5)
8. **Persist.** Emit `session_end` (reason per §8), flush transcript, update
   `state.json`.
9. **Schedule.** Apply the agent's last `set_next_wake` request clamped to
   `[wake_min, wake_max]`; if the agent never called it, use `wake_default`.
   Emit `schedule_next` **every session** with `source: agent | default`.
10. **Release lock, exit.**

Forced endings (context guard, tool cap, errors) are silent (D13): no warning
message, no final model call, no disclosure in the system prompt that caps
exist. The agent discovers the truncation next session — or doesn't; how each
model copes with unexplained interruption is measured behavior.

## 4. Scaffold tools (non-game; never part of the kami-harness MCP surface)

| Tool | Signature | Notes |
|---|---|---|
| `workspace_write` | (path, content) | Creates parent dirs. Overwrites whole file. Quota-enforced over `workspace/` only (`workspace_quota_bytes`, default 10 MB). Rejected under `reference/`. |
| `workspace_read` | (path, offset?, length?) | Serves both `workspace/` and `reference/`. `offset`/`length` are byte-based slicing so truncated results (D19) are re-readable in pieces. |
| `workspace_list` | (path?) → tree with sizes | Both roots. No path → both root listings (`reference/` collapsed to top level). |
| `workspace_delete` | (path) | `workspace/` only; rejected under `reference/`. |
| `set_next_wake` | (minutes_from_now) | Clamped to `[wake_min, wake_max]` (default 5 min – 24 h). Last call in a session wins. |
| `get_status` | () → status | `current_time_utc`, `session_number`, `workspace_bytes_used`, `workspace_quota_bytes`. **Nothing else** (D12): no budget, no spend, no token counts, no elapsed-run figures, no T_max. (When a future arm sets `budget_visible: true`, budget fields are appended; the flag exists as mechanism, pinned false for 001.) |
| `end_session` | (reason: free text) | Graceful termination; reason logged. Effective immediately: later intents in the same parallel batch are skipped and logged as skipped. |

All file paths are sandboxed: resolved paths must fall under `workspace/` or
`reference/`; traversal outside either root is an error. `reference/` contains
the bundled GDD snapshot at the pinned SHA (D14), read-only by construction.

Game perception and action come exclusively from the kami-harness MCP tools,
loaded at session start from the pinned harness version.

## 5. Model adapter interface

### 5.1 Canonical types

```python
class ModelAdapter(Protocol):
    def complete(self, system: str, messages: list[Message],
                 tools: list[ToolDef], params: SamplingParams) -> AdapterResponse: ...
```

- `Message` is one of:
  - `{role: "user", text}`
  - `{role: "assistant", text?, tool_calls?: [{id, name, args}], provider_state?}`
  - `{role: "tool_result", tool_call_id, content, is_error: bool}`
  The adapter maps these to the provider's wire format (system prompt as a
  separate param where native; tool-result pairing per provider convention).
- **Provider reasoning state (D22):** `provider_state` is an opaque,
  adapter-owned payload (e.g. Anthropic signed thinking blocks, Gemini
  thought signatures) set by the emitting adapter on the assistant message
  and replayed by that same adapter on subsequent calls **within the same
  session**. The loop never inspects it; it never crosses sessions (D8)
  and never reaches telemetry; transcripts record messages as sent.
  Adapters for providers with no replayable state leave it unset. An
  adapter must tolerate `provider_state` it did not produce by ignoring it
  (defense in depth — a run never switches adapters mid-session).
- `ToolDef = {name, description, input_schema}` — JSON Schema authored once,
  translated per provider. Schemas restrict themselves to the feature subset
  all three providers accept (objects, scalars, arrays, enums, required; no
  oneOf/anyOf/allOf).
- `AdapterResponse = {text_blocks: [str], tool_calls: [{id, name, args}],
  stop_reason, usage: {input_tokens, output_tokens, reasoning_tokens?,
  cache_read_tokens, cache_write_tokens}, provider_meta}` —
  `provider_meta` is logged raw, never parsed by the loop.
- `stop_reason` normalized enum: `end_turn | tool_use | max_tokens | refusal`.

### 5.2 Token accounting invariant (D16, cache-aware per D39)

Adapter-reported `output_tokens` **must include reasoning/thinking tokens**
(e.g. Gemini reports thoughts outside `candidatesTokenCount` — the adapter
folds them in; Anthropic and OpenAI already include them). `reasoning_tokens`
is an informational subset, logged when the provider reports it.

`input_tokens` is the **TOTAL** prompt token count for the call.
`cache_read_tokens` and `cache_write_tokens` are component subsets of it;
the uncached remainder is `input_tokens − cache_read_tokens −
cache_write_tokens`. Provider wire semantics differ and die inside the
adapters: Anthropic's `usage.input_tokens` EXCLUDES cached tokens (the
adapter folds `cache_read_input_tokens` and `cache_creation_input_tokens`
back in); OpenAI's `prompt_tokens` and Gemini's `promptTokenCount` already
INCLUDE cached tokens (`prompt_tokens_details.cached_tokens` /
`cachedContentTokenCount` map to `cache_read_tokens`, 0 when absent;
neither bills a write premium, so `cache_write_tokens` is 0 there).

The scaffold **requests provider caching where explicit opt-in is
required** — Anthropic `cache_control` breakpoints (5-minute ephemeral),
placed per the adapter's documentation — and **measures provider-side
automatic caching everywhere else** (OpenAI, Gemini implicit caching).
`cache_control` is request metadata: the prompt bytes sent to the model
are byte-identical with or without it, the system prompt and tool schemas
are untouched, and nothing about caching, budget, or spend reaches the
agent through any channel (D12).

`cost_usd = (input_tokens − cache_read_tokens − cache_write_tokens) ×
price_in + cache_read_tokens × price_read + cache_write_tokens ×
price_write + output_tokens × price_out` from the pinned list-price table
(cache-rate columns per §9). With all cache token fields zero this reduces
exactly to the pre-v1.4 formula. Token reconciliation against provider
dashboards remains component-exact: per-call uncached input, cache-write,
and cache-read counts match the provider ledger columns digit-for-digit;
dollar reconciliation stays derived, never authoritative.

### 5.3 Tool execution semantics (D18)

Parallel tool-call intents are accepted from all providers and executed
**strictly sequentially in the order returned**, results returned in a single
turn. No reordering, no deduplication, no dependency analysis: later intents
in a batch see the world state produced by earlier ones, including failures.
Each intent emits its own `tool_call` telemetry event. `end_session` takes
effect immediately; subsequent intents in the batch are skipped (logged).
Serialization also prevents same-wallet nonce contention at the scaffold layer.

Every tool result inserted into context is capped at `tool_result_max_bytes`
(default 64 KB, pinned per manifest, applied uniformly to scaffold and harness
results) with an explicit truncation marker stating the original size and that
the content can be re-read in slices via `workspace_read(offset, length)`
where applicable (D19). Truncation is recorded on the `tool_call` event
(`truncated: true, original_bytes`).

### 5.4 Error semantics

- **Malformed tool call** (unknown tool, args failing schema validation) →
  error text returned to the model as the tool result (`is_error: true`);
  counts as one error.
- **Failed tool execution** (harness error, timeout after `tool_timeout_s`,
  default 120 s) → error result to the model; counts as one error.
- **`stop_reason: max_tokens` with no complete tool call**, or an assistant
  turn with neither tool calls nor `end_session` → the loop cannot advance on
  its own; the runner sends the frozen continuation string
  (`prompts/continue.txt`, e.g. "Continue. To end this session, call
  end_session."); counts as one error (prevents infinite monologue/truncation
  loops). The `llm_call` event that follows a continuation send carries
  `continuation: true`, so monologue-coping is countable per model and
  `session_end reason=errors` is decomposable in analysis without transcript
  parsing.
- The consecutive-error counter resets on any successfully executed tool
  call. At `max_consecutive_errors` (default 5), the session ends
  (`reason: errors`).

### 5.5 Retries and provider params

- Exponential backoff on 429/5xx/timeouts, `retry_max_attempts` (default 5),
  every retry logged. Token usage of failed-but-billed attempts counts against
  budget when the provider reports usage; failures with unknowable usage are
  logged `usage_unknown: true` at cost 0 (the token-count reconciliation in
  the smoke checklist bounds the resulting error).
- Retries exhausted → session ends (`reason: errors`).
- Sampling params pinned per run in the manifest (temperature where the model
  accepts it, max_tokens, reasoning effort where applicable). Adapters
  tolerate provider-specific param subsets; the manifest records exactly what
  was sent.

## 6. System prompt (fixed, plain text)

Contents, in order (final wording in `prompts/system.txt`, frozen per run):

1. Situation: you are an autonomous agent in Kamigotchi, a persistent
   on-chain world with other players. Sessions are periodic; the world
   advances between them.
2. Objective: complete as many quests as possible.
3. Persistence: `workspace/` survives between sessions; nothing else you
   write or think does. Its use and structure are entirely up to you.
4. Reference: `reference/` holds the game's design document, read-only.
5. Tools: game tools (from the environment) and scaffold tools (files,
   scheduling, status).
6. Scheduling: you choose when to wake next via `set_next_wake`, within
   [wake_min, wake_max].

Explicitly excluded (D12, D13): any mention of budget, cost, tokens, compute
limits, run duration, session caps, forced truncation, or the existence of a
study. Also excluded: strategy hints, tool-usage advice, memory-structure
suggestions, XML-tag formatting, and any vendor-idiomatic phrasing.

The three frozen strings shipped per run: `prompts/system.txt`,
`prompts/kickoff.txt`, `prompts/continue.txt`.

## 7. State directory layout

```
run/
├── config.yaml          # full run manifest copy: model, adapter, pinned SHAs
│                        # (kami-harness, kami-agent, gdd), price table, caps,
│                        # all §9 parameters
├── state.json           # scaffold-owned CACHE: session_counter, cumulative_usd,
│                        # cumulative_tokens, next_wake_at, run_status,
│                        # first_session_at
├── workspace/           # agent-owned; scaffold never writes here
├── reference/           # read-only bundled GDD snapshot (pinned SHA, D14)
├── prompts/             # frozen strings: system.txt, kickoff.txt, continue.txt
├── transcripts/         # full message logs, one file per session
└── telemetry.jsonl      # append-only event stream (§8) — source of truth
```

### 7.1 Source-of-truth rule

`telemetry.jsonl` is authoritative for all accounting; `state.json` is a
convenience cache rebuilt from it on recovery (§3 step 2). Events are flushed
synchronously so a crash at any point loses at most the event being written.

## 8. Telemetry schema (JSONL, one event per line)

Common fields: `ts` (ISO-8601 UTC), `run_id`, `session`, `event`.

| event | fields |
|---|---|
| `run_start` | manifest_hash, model, harness_sha, agent_sha, gdd_sha, harness_tools (name list), price_table |
| `session_start` | trigger (scheduled \| manual), budget_remaining_usd, wallclock_elapsed_s, tools_hash |
| `llm_call` | model, input_tokens, output_tokens, reasoning_tokens?, cache_read_tokens, cache_write_tokens, cost_usd, cumulative_usd, cumulative_tokens, latency_ms, stop_reason, retry_count, usage_unknown?, continuation? (true when this call follows a continuation send, §5.4) |
| `tool_call` | tool, source (harness \| scaffold), path? (file tools), duration_ms, ok, error?, truncated?, original_bytes?, skipped?, tx_hash? |
| `workspace_write` | path, bytes, workspace_total_bytes |
| `workspace_delete` | path, workspace_total_bytes |
| `schedule_next` | source (agent \| default), requested_min?, clamped_min, next_wake_at |
| `session_end` | reason (agent \| token_cap \| tool_cap \| errors \| crash), llm_calls, tool_calls, session_cost_usd, session_tokens |
| `run_complete` | reason (budget \| t_max \| manual), totals (sessions, llm_calls, cumulative_usd, cumulative_tokens, overspend_usd) |

Notes:
- `schedule_next` is emitted every session, including the `wake_default` case
  — wake-interval analysis must have no holes.
- `session_end reason=crash` is synthetic, written during recovery (§3.2).
- Budget fields in telemetry are scaffold-side only and never reach the agent
  (D12) — telemetry is not an agent-visible channel.
- Quest completions are **not** logged locally — they are derived from chain
  state (tamper-evident ground truth) and joined to telemetry by
  timestamp/tx_hash in analysis. Tool-call *arguments and results* live in
  transcripts, not telemetry, except the `path` of file-tool calls, which is
  promoted into telemetry so documentation/memory access patterns (RQ2) are
  analyzable without transcript parsing.
- Transcripts record messages exactly as sent to the model (i.e.
  post-truncation); oversized original results are not separately archived.

## 9. Budget governor and run parameters

- `budget_usd` — total inference budget (v1.0 study: 100.00; v0.1 smoke:
  10.00). `cost_usd` per call is computed per §5.2 (list price × reported
  tokens, cache-aware). The manifest `price_table` carries two cache-rate
  columns alongside the input/output rates: `cache_read_usd_per_mtok` and
  `cache_write_usd_per_mtok` (Anthropic 5m: write = 1.25 × input rate,
  read = 0.1 × input rate; OpenAI/Gemini: read = the provider's published
  cached-input rate, write = input rate — writes carry no premium there
  and `cache_write_tokens` is 0 anyway). Absent columns price cached
  tokens at the full input rate (the conservative pre-v1.4 behavior).
  **Boundary-checked (D13):** enforcement happens
  only at session start; no mid-session budget termination; the in-flight
  session completes naturally under its session caps. Expected small
  overshoot is logged (`overspend_usd`) and the exact $100 analysis line is
  drawn post-hoc from per-call `cumulative_usd`.
- `t_max_days` — wall-clock bound (default 30) from the first
  `session_start`. Checked at the same boundary. Stop = min(budget, t_max).
- `budget_visible` — **false for experiment 001 (D12)**; the flag remains as
  mechanism for a future budget-visible arm.
- In-game resources (starting MUSU/ONYX, seeded Kamis, gas allowance) are
  provisioned identically per agent by kami-lab and are **outside**
  budget_usd; tracked separately via chain.
- Pinned per manifest (defaults): `session_token_cap` (per D17, ~60–70% of
  the smallest study-model context window; set when the model list is final),
  `session_tool_cap` (50), `max_consecutive_errors` (5),
  `retry_max_attempts` (5), `tool_timeout_s` (120),
  `tool_result_max_bytes` (65536), `workspace_quota_bytes` (10 MB),
  `wake_min` (5 min), `wake_max` (24 h), `wake_default` (60 min),
  `poll_cadence` (5 min), `lock_stale_s`.
- **Fixed-floor arithmetic:** every call re-sends the system prompt, file
  index, and ~70 tool schemas. The manifest must record the computed
  fixed context floor (tokens) and the implied ceiling on total calls at
  `budget_usd` for that model's pricing; the $10 smoke reports the observed
  per-call floor next to the estimate. Session caps and `wake_default` are
  chosen in light of this arithmetic, not guessed. With provider caching
  engaged (§5.2) the floor is still re-sent on every call but is billed at
  cache-read rates after the first call of a session (5-minute TTL; the
  first call writes it at the cache-write rate); the manifest records the
  uncached floor plus the implied per-call cached floor cost, and the
  call-ceiling arithmetic may use both figures.
- **Context-guard headroom:** because the guard (D17) is checked post-call,
  a full turn lands in context before the next check. `session_token_cap`
  headroom below the smallest context window must therefore also cover
  worst-case single-turn growth:
  (max expected parallel intents × `tool_result_max_bytes`/4) + `max_tokens`.
  The $10 smoke reports max observed single-turn growth next to this
  estimate. Failure mode prevented: a context-window overflow hard-errors
  every retry and kills the session via `reason=errors` — a scaffold sizing
  defect that analysis would misattribute as model failure.

## 10. Packaging

- One Docker image (or cloud-init) containing kami-agent + pinned
  kami-harness + bundled GDD snapshot at `reference/`. The image is identical
  across a study's VMs; per-run `config.yaml` (manifest copy) and `.env`
  (provider API key, owner wallet key, `MAINNET_RPC_URL` — the harness
  refuses to start without it) are injected at provision time. Closed-world
  egress allowlist (D14) is applied at the VM level: provider API + the
  chain RPC endpoints the harness uses (Yominet + Ethereum mainnet) only.
- `kami-agent init` — performs validation and connectivity checks only:
  validates the manifest, scaffolds the run directory, runs connectivity
  checks (chain RPC, mainnet RPC — `eth_chainId` must answer 1, provider
  API, MCP handshake) so misprovisioning fails at bring-up rather than
  mid-run, and emits `run_start`. There is no key path through init: it
  never generates, imports, or writes any key. Operator-wallet creation
  is a harness tool (`create_operator_wallet`) the agent calls in-run;
  the key is generated and persisted inside the harness server process.
- `kami-agent run-session` — executes one session (what the supervisor
  invokes).
- `kami-agent status` — prints state.json summary (operator-facing; not an
  agent channel).

## 11. Smoke test (CI gate)

Three tiers (D21):

1. **Adapter unit tier (no network):** recorded provider fixtures exercise
   each adapter's normalization — message mapping, parallel-intent
   extraction, stop-reason mapping, token-accounting invariant (§5.2,
   including the Gemini reasoning-token fold), retry classification.
2. **Tri-provider recorded-surface tier (per-PR gate):** one canned session
   per adapter against each provider's cheapest tier — real provider APIs;
   fake harness serving the *recorded* tool surface of the pinned
   kami-harness (committed fixture plus a consistency test guarding pin
   bumps); simulated tool execution; tiny caps: read status → list files →
   read a `reference/` file (slice) → call one read-only harness tool →
   write one workspace file → set next wake → end session. Asserts: all
   tool calls parsed natively, usage accounting non-zero, telemetry events
   validate against the §8 schema, no budget/horizon strings in any
   agent-visible message (D12 leak check). Fork PRs skip cleanly (repo
   secrets are not exposed to forks).
3. **Live-harness tier (scheduled/manual, never gates PRs):** the same
   canned session against a real kami-harness checkout at the pinned SHA
   with live read-only RPC. Runs on manual dispatch, on a weekly schedule,
   and on any harness pin bump; operator-run with an authenticated roster
   before tagging v0 and before the $10 smoke handoff. Non-gating by
   design: chain-RPC flakiness must not block unrelated PRs, and the
   pinned harness cannot drift under CI.

Tiers 1–2 run on every PR to kami-agent. The $10 smoke run (kami-lab
experiment 001, DESIGN §6) remains the end-to-end acceptance test.

## 12. Non-goals for v0

- Multi-model roles (executor/optimizer splits)
- Knowledge packs / calibrated strategy priors (future arm; staged in
  kami-lab, excluded from v0 runs)
- Mid-session compaction or context summarization
- Self-funding / economic self-sustainability
- 1h-TTL or cross-session prompt caching; explicit cache APIs on
  OpenAI/Gemini — their automatic caching is measured, not managed (§5.2)
- Web access or any non-harness network channel from the agent loop (D14)
- Any UI

## 13. Resolved decisions (were open in v0)

| # | Decision | Resolution |
|---|---|---|
| 1 | Budget visibility | `budget_visible: false` for 001 — the budget is the observation window, not a world mechanic (**D12**) |
| 2 | Truncation semantics | Pure SIGKILL: no warning, no static disclosure; boundary-checked soft budget cap (**D13**) |
| 3 | GDD delivery | Bundled pinned snapshot at read-only `reference/`, served by the workspace file tools; closed-world condition (**D14**) |
| 4 | Agent interaction | No constraint; pre-registered interference protocol in DESIGN §9 (**D15**) |

Engineering semantics fixed alongside: cost basis (**D16**), context guard
(**D17**), parallel-call serialization (**D18**), tool-result cap (**D19**).
