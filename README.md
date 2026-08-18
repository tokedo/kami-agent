# kami-agent

The model-agnostic reference agent scaffold for
[KamiBench](https://www.kamibench.xyz) — a behavioral benchmark that drops
frontier models into Kamigotchi, a live on-chain world, and measures what
they do under controlled conditions.

**Status: v0.5.1.** [SPEC.md](SPEC.md) is the contract registry — what the
scaffold provides, what it depends on, the invariants and how each one is
enforced, and the behaviors that are accepted by design.

## What this is

kami-agent turns a stateless model API into a persistent actor in the
Kamigotchi world. It is deliberately minimal: its value comes from being
boring. The scaffold fixes *how* an agent can act, remember, and schedule
itself; it never plays for the agent — within any one configuration,
no strategy, priorities, or targets are supplied. What the scaffold
*offers* the model — which files are read back to it, which prompt
appendices it gets, which retrieval tools exist — is itself a
manifest-pinned variable (`scaffold_profile`). The unit a run measures
is the whole system: model plus scaffold configuration plus pinned
environment. A comparison across models holds the other two equal; a
comparison across configurations holds the model and environment equal.

Design principles behind the contract:

1. **Mechanism fixed, policy free — per configuration.** No strategy,
   priorities, efficiency claims, or numbers to aim at anywhere the
   model can see. Structure (memory surfaces, appendices, retrieval,
   session-start injections) is a named, byte-frozen, manifest-pinned
   profile — never a default that drifts, never advice.
2. **Model-agnostic by construction.** One loop, N provider adapters,
   native tool calling per provider, no vendor idioms in prompts or loop
   logic.
3. **Session-based, not daemon-based.** Persistence is state on disk plus
   a scheduler; recovery reconstructs accounting from the event stream,
   never from memory.
4. **Everything is logged.** If it isn't in telemetry or on-chain, it
   didn't happen.
5. **No compaction.** Cross-session memory exists only as agent-written
   workspace files, so "memory" stays fully inspectable.
6. **The agent is blind to the apparatus.** No budget, spend, run-duration,
   or cap information reaches it through any channel; forced endings are
   silent.
7. **Closed world.** The agent's only channels are the harness tools, the
   bundled read-only `reference/` tree, and its own `workspace/`.

Every session opens with **session-start injections** (SPEC P1.12):
before the first model call the scaffold performs a few reads and puts
each into context as a completed tool call and its result, so a session
starts already knowing where it stands rather than spending turns
rediscovering it. In order: the compact **roster** of the account's kamis,
read straight from the world-state daemon; the wallets' **gas balances**,
read from the harness's own balance tool, because gas is the resource
every action spends; and, on the profile that carries it, the agent's own
**plan file**. They are state, not advice — passed through verbatim, one
attempt each, degrading visibly rather than silently — and they bound
nothing the agent does: no cap, no error counter, no breaker. Telemetry
marks each with `initiator: scaffold`, so any measure of what the *agent*
chose excludes them.

**The scaffold itself is configurable as an experimental variable.** A
manifest `scaffold_profile` selects one of five cumulative rungs —
`control`, `orientation`, `search`, `pushed`, `planning` — which decide
whether the system prompt carries a pinned paragraph about the world's
core loop, whether a deterministic `search_reference` tool over the
documentation snapshot is on the surface, and whether the agent has a
plan file re-read to it each session. One version serves every rung
(variants are flags, never branches), the rung is recorded on every
`session_start`, and because a rung can add a tool, `tools_hash` differs
between rungs by design while the harness surface stays identical. What
does *not* vary: the objective, the world, the harness, and the absence of
strategy content anywhere the model can see.

## The four-layer stack

| Layer | Repo | Varies per run? |
|---|---|---|
| Model backend | (provider APIs) | **yes — the only variable** |
| Reference scaffold | `kami-agent` (this repo) | no (pinned SHA) |
| Environment interface | [`kami-harness`](https://github.com/tokedo/kami-harness) | no (pinned SHA) |
| World | Kamigotchi on-chain | shared, live |

## Setup

Bring-up, the Docker image, and per-run injection of `config.yaml`,
`.env`, and the `reference/` snapshot: [docs/packaging.md](docs/packaging.md).
A manifest template lives in [`manifests/example.yaml`](manifests/example.yaml).

```sh
uv sync
uv run kami-agent init --manifest manifests/example.yaml --run-dir /srv/run
uv run kami-agent run-session --run-dir /srv/run --manual
```

## CLI

- `kami-agent init` — manifest copy, run-dir scaffolding, connectivity
  checks (chain RPC, mainnet RPC, provider API, MCP handshake), emits
  `run_start`. Handles no key material.
- `kami-agent run-session` — execute one session (what the scheduler
  invokes).
- `kami-agent status` — operator-facing state summary.

Contract: [SPEC.md](SPEC.md) P12.

## Verification

Four tiers, all named as enforcement in the SPEC's invariant table:

1. **Unit** (`uv run pytest tests/unit`, per PR, no network) — adapter
   normalization against recorded provider fixtures, loop and lifecycle
   semantics, sandbox properties, telemetry schema validation, leak scans.
2. **Cron-env smoke** (per PR) — one full `init` + `run-session` under a
   cron-like environment (minimal `PATH`, explicit `HOME`, no provider
   keys), judged by real exit codes and explicit telemetry assertions.
   Runs once per **end of the scaffold ladder** (`control` and
   `planning`), which between them exercise every session-start injection
   and every prompt asset under cron conditions.
3. **Tri-provider recorded-surface** (`uv run pytest tests/smoke`, per PR)
   — one canned session per adapter against real provider APIs with a fake
   harness serving the *recorded* tool surface of the pinned kami-harness
   and a fixture daemon serving the brief. **It bills real provider
   calls**, so it is run deliberately, not as part of a local test sweep —
   and note that `tests/smoke/conftest.py` loads the repo-root `.env`, so
   `pytest tests/smoke` bills even when the shell has no keys exported.
   It is also the only tier that proves three provider APIs accept the two
   or three consecutive synthesized assistant turns the session-start
   injections put in front of the model.
   Fork PRs skip cleanly, since repo secrets are not exposed to them.
   This tier also reports the observed per-call fixed context floor that
   the SPEC D1 cap-arithmetic assumption needs, on a fixed measurement
   convention: the canned kickoff calls one keyless read-only harness
   tool, `list_accounts`. Floors are only comparable across pins that
   share that convention — when the named tool has to change because the
   pinned surface no longer serves it, the convention delta is measured
   on one surface rather than assumed away.

   The floor also depends on the **Python minor version the harness runs
   under**, which is why the recorded fixture names its own. Harness tool
   descriptions are docstrings, and CPython 3.13 strips their common
   leading indentation at compile time where 3.12 retains it: the same
   harness commit therefore serves the same 101 tools with materially
   different description bytes, a different hash, and a different floor.
   The packaged image (`Dockerfile`) is `python:3.13-slim`, so 3.13 is
   the reference — every tier that spawns a harness must match it, and
   the live tier asserts the surface it gets equals the recorded one.

   Since the session-start brief (SPEC P1.12, D7) lands in call-1
   context, it is part of the floor, and its size is linear in how many
   kamis the account owns — the one term in the floor that **moves
   during a run**. Against the recorded surface the brief is served from
   a committed fixture (`tests/smoke/fixtures/roster_brief.json`:
   synthetic roster, real envelope shape), so the floor is reproducible;
   the report quotes the roster size next to the floor, because one is
   not interpretable without the other. `KAMI_SMOKE_BRIEF_KAMIS=N`
   re-measures the floor at any roster size, which is how the sizing
   headroom in a manifest's `session_token_cap` is checked against the
   roster a run will grow to rather than the one it starts with.

   **Floors do not compare across 0.4.0.** The brief changed from a full
   party report to a compact roster, and from the harness's
   pretty-printed serialization to the scaffold's compact one. Both cut
   it; together they cut it by roughly an order of magnitude at a given
   roster size and flatten its slope in roster size by about as much
   again. The fixture that measured the old shape also measured it
   wrongly — compact bytes for a path that pretty-printed, and an
   envelope `meta` block the daemon never served — so pre-0.4.0 floors
   describe a shape no model ever saw.

   **Floors do not compare across profiles either, from 0.5.0.** Call-1
   context now depends on the rung: every profile adds the gas-balance
   injection, rungs at or above `orientation` add a pinned prompt
   appendix, and `planning` adds a second appendix plus the plan file —
   whose size the *agent* decides, bounded only by
   `tool_result_max_bytes`. The report prints each term separately
   (`system_chars`, `orientation_chars`, `planning_chars`,
   `balance_chars`, `plan_file_chars`) and names the profile, because a
   floor without its profile is not a floor. `KAMI_SMOKE_PROFILE=<rung>`
   measures any of them.
4. **Live-harness** (scheduled and on demand, never gates PRs) — the same
   canned session against a real kami-harness checkout at the pinned SHA
   with live read-only RPC. Non-gating by design: chain-RPC flakiness must
   not block unrelated PRs. This tier is also where the brief's real
   failure path is observed: it queries the daemon socket directly, so
   with no kami-lens daemon running it degrades to its unavailability
   record, which is injected as-is and reported — the floor this tier
   prints is then a *failed*-brief floor, and is not comparable to
   tier 3's.

The telemetry event schema ([`schema/telemetry.json`](schema/telemetry.json),
versioned independently) is the contract for downstream analysis.

## License

[MIT](LICENSE)
