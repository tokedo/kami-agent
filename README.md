# kami-agent

The model-agnostic reference agent scaffold for
[KamiBench](https://www.kamibench.xyz) — a behavioral benchmark that drops
frontier models into Kamigotchi, a live on-chain world, and measures what
they do under controlled conditions.

**Status: v0.2.0.** [SPEC.md](SPEC.md) is the contract registry — what the
scaffold provides, what it depends on, the invariants and how each one is
enforced, and the behaviors that are accepted by design.

## What this is

kami-agent turns a stateless model API into a persistent actor in the
Kamigotchi world. It is deliberately minimal: its value comes from being
boring. The scaffold fixes *how* an agent can act, remember, and schedule
itself; it never fixes *what* to do, *what* to remember, or *when* to act.
All strategy, memory content, and pacing decisions belong to the model
under test — cross-model divergence there is a primary measurement.

Design principles behind the contract:

1. **Mechanism fixed, policy free.** No strategy, memory advice, or pacing
   hints anywhere the model can see.
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

Contract: [SPEC.md](SPEC.md) §P12.

## Verification

Four tiers, all named as enforcement in the SPEC's invariant table:

1. **Unit** (`uv run pytest tests/unit`, per PR, no network) — adapter
   normalization against recorded provider fixtures, loop and lifecycle
   semantics, sandbox properties, telemetry schema validation, leak scans.
2. **Cron-env smoke** (per PR) — one full `init` + `run-session` under a
   cron-like environment (minimal `PATH`, explicit `HOME`, no provider
   keys), judged by real exit codes and explicit telemetry assertions.
3. **Tri-provider recorded-surface** (`uv run pytest tests/smoke`, per PR)
   — one canned session per adapter against real provider APIs with a fake
   harness serving the *recorded* tool surface of the pinned kami-harness.
   Fork PRs skip cleanly, since repo secrets are not exposed to them.
4. **Live-harness** (scheduled and on demand, never gates PRs) — the same
   canned session against a real kami-harness checkout at the pinned SHA
   with live read-only RPC. Non-gating by design: chain-RPC flakiness must
   not block unrelated PRs.

The telemetry event schema ([`schema/telemetry.json`](schema/telemetry.json),
versioned independently) is the contract for downstream analysis.

## License

[MIT](LICENSE)
