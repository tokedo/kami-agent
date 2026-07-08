# kami-agent

The model-agnostic reference agent scaffold for
[KamiBench](https://www.kamibench.xyz) — a behavioral benchmark that drops
frontier models into Kamigotchi, a live on-chain world, and measures what
they do under controlled conditions.

**Status: pre-v0, under construction.** The specification is final
([SPEC.md](SPEC.md)); implementation is in progress.

## What this is

kami-agent turns a stateless model API into a persistent actor in the
Kamigotchi world. It is deliberately minimal: its scientific value comes from
being boring. The scaffold fixes *how* an agent can act, remember, and
schedule itself; it never fixes *what* to do, *what* to remember, or *when*
to act. All strategy, memory content, and pacing decisions belong to the
model under test — cross-model divergence there is a primary measurement.

One loop, N provider adapters, native tool calling per provider. No vendor
idioms in prompts or loop logic. See [SPEC.md](SPEC.md) for the full contract.

## The four-layer stack

| Layer | Repo | Varies per run? |
|---|---|---|
| Model backend | (provider APIs) | **yes — the only variable** |
| Reference scaffold | `kami-agent` (this repo) | no (pinned SHA) |
| Environment interface | [`kami-harness`](https://github.com/tokedo/kami-harness) | no (pinned SHA) |
| World | Kamigotchi on-chain | shared, live |

## Setup

Coming in v0.

## CLI

Coming in v0. Planned commands (SPEC §10):

- `kami-agent init` — wallet generation, config from a run manifest,
  connectivity check
- `kami-agent run-session` — execute one session (what the supervisor invokes)
- `kami-agent status` — operator-facing state summary

## License

[MIT](LICENSE)
