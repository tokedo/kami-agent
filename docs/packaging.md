# Packaging and provisioning (SPEC P12, D8)

One Docker image per study, identical across hosts: kami-agent, the pinned
kami-harness (spawned per session as a stdio child) and the documentation
snapshot. On the `shell` and `shell_keeper` rungs the run also has an
**environment container** — the agent's own machine, with no network and
no key — and, on `shell_keeper`, a keeper daemon beside the world-state
daemon. That definition lives in [`deploy/environment/`](../deploy/environment/README.md):
the images, the compose files, the boundary, the bring-up and the end of
the run. The rungs below `shell` need none of it.

## Per-run inputs

Injected at provision time, never baked into an image:

- `/srv/run/config.yaml` — the run manifest (see `manifests/example.yaml`);
  immutable per run.
- `/srv/run/.env` — the provider API key, `MAINNET_RPC_URL` (the harness
  refuses to start without it), and, in the owner + operator key shape
  only, the owner key `MAIN_OWNER_KEY`. The scaffold loads this file into
  its own environment and passes that environment to the harness child; it
  is never mounted into, or exported to, the environment container.
  `kami-agent init` writes nothing here — there is no key path through
  init.
- **The keys file**, `KAMI_KEYS_FILE=/srv/keys/.env` (0600). It is the
  harness's keys file: when the agent creates its operator wallet in-run
  (`create_operator_wallet`), the harness writes `MAIN_OPERATOR_KEY` there.
  On `shell_keeper` the keeper container mounts the same directory
  read-only and loads `MAIN_OPERATOR_KEY` from it when the agent grants the
  account — only the label `main` ever crosses the keeper's socket. The
  keeper's own store refuses to load any name that is not an operator key,
  and the owner key is never in this file.
- `/srv/run/reference/` — the pinned documentation snapshot (SPEC D5),
  read-only to the agent (the path sandbox, and on the shell rungs a
  read-only mount at `/reference`).

## The two key shapes (SPEC D6)

Every key lives in a file on the host and reaches no model, ever: not in a
prompt, a tool description, a tool result, the journal, telemetry, or the
environment container (I1).

| shape | where the keys are | what works |
|---|---|---|
| **owner + operator** (the quick start) | `MAIN_OWNER_KEY` in `/srv/run/.env`; the operator key is created in-run by the agent and written by the harness to the keys file | every harness tool |
| **operator only** | `MAIN_OPERATOR_KEY` alone, placed in the keys file at provision time; no owner key anywhere on the host. The account is registered beforehand, from wherever its owner wallet is kept | every game action — from kami-harness 5.0.0 trades, item transfers, auction buys and gacha sign with the operator. The harness's owner-signed tools refuse, naming the missing key: `register_account`, `buy_kami`, `newbie_vendor_buy`, `portal_deposit`, `portal_withdraw(to="owner")`, an owner receipt's `portal_claim` / `portal_cancel`, `fund_operator`, `withdraw_operator`, `bridge_eth_from_mainnet` |

In both shapes the keeper only ever receives the operator key. The harness
resolves a key from its process environment first and the keys file
second, which is how an owner key in `/srv/run/.env` reaches it.

## Bring-up

Rungs below `shell`:

```sh
docker build -t kami-agent \
  --build-arg HARNESS_SHA=<pinned sha> \
  --build-arg GDD_REPO=<gdd repo url> --build-arg GDD_SHA=<pinned sha> .

kami-agent init --manifest /srv/run/config.yaml --run-dir /srv/run
# connectivity checks: chain RPC + mainnet RPC (eth_chainId == 1) +
# provider API + MCP handshake; emits run_start

# supervisor: fixed-cadence poller (SPEC D4)
python -c "from kami_agent.supervisor import install_cron; \
           install_cron('kami-agent run-session --run-dir /srv/run', 5)"
```

The shell rungs: [`deploy/environment/README.md`](../deploy/environment/README.md).
There `init` also asserts the environment matches the rung (a keeper
command on `shell_keeper` and none on `shell`, the keeper image naming
`pins.keeper_sha`, the lens answering from inside), gives the workspace to
the environment's user (uid 1000), and the `agent` service is the poller.

`GDD_SHA` is the manifest's `pins.gdd_sha`; `manifests/example.yaml`
names the kamigotchi-gdd commit that matches its harness and lens pins.

`kami-agent status --run-dir /srv/run` prints the state.json cache
(operator-facing; never an agent channel — SPEC I1).

## What to pull off a host, and in what order (SPEC P14, P15)

- `/srv/run/telemetry.jsonl` — the source of truth for all accounting.
  Any `session_refused` line in it means sessions are not starting at all
  (SPEC P1 step 9): under the scheduler it repeats every poll until the
  deployment is fixed, so alert on it. On the shell rungs `shell_state`
  lines record the environment at every session boundary, and on
  `shell_keeper` `keeper_tx` lines record what the keeper signed.
- `/srv/run/errors.jsonl` — **pull this first after any incident.** One
  line per failed provider call, written at the moment of the failure with
  the provider's own status, error type and message. It exists because a
  host may be gone, or its process dead, before anyone thinks to ask what
  the provider actually said; nothing else on the machine records it, and
  the telemetry copy is cut short for bulk reading.
- `/srv/run/transcripts/` — messages exactly as sent. Injected
  session-start turns carry `initiator: scaffold`; everything without that
  key is the model's own.
- `/srv/run/shell/` (shell rungs) — one log per `shell` call,
  `<session:03d>-<call_seq:04d>.log`: the command, how it ended, and both
  output streams in full (each kept up to 32 MiB per call, and the log
  says where it cut). The model saw at most half of `tool_result_max_bytes`
  of each stream. A run-directory internal: never mounted into the
  environment.
- `/srv/run/journal/sessions.jsonl` — the scaffold's own per-session
  record. Derived from telemetry rather than authoritative, and rolled to
  a size bound, so it is the agent's view of its past and not an archive.
- `/srv/run/workspace/` — everything the agent wrote for itself; on the
  shell rungs its whole home, whatever it installed there included.

Run-directory internals no agent path reaches (SPEC P11, D8): `.env`,
`config.yaml`, `state.json`, `telemetry.jsonl`, `errors.jsonl`, `prompts/`,
`transcripts/`, `shell/`, `harness-lanes/`, `run.lock`.

## Egress allowlist (SPEC I20/N6, enforced at the host level)

The **environment container has no network at all** (`network_mode:
none`): no host below is reachable from it, and the two daemons it can use
are reached over unix sockets. The agent container (the loop and the
harness), the lens and the keeper are host-networked behind the host
firewall, which allows outbound traffic ONLY to:

| destination | why |
|---|---|
| the run's model provider API — `api.anthropic.com`, `api.openai.com`, or `generativelanguage.googleapis.com` — or, with `provider: openai`, the OpenAI-compatible endpoint the manifest's `openai.base_url` names | the only per-run variable |
| the chain RPC host (manifest `chain_rpc_url`) | harness reads/writes world state |
| the mainnet RPC host (`.env` `MAINNET_RPC_URL`) | harness bridge tools (`bridge_eth_from_mainnet`, `bridge_status`) |
| `router-api.initia.xyz` | the bridge route the harness's `bridge_eth_from_mainnet` asks for before it signs |
| `api.prod.kamigotchi.io` | Kamiden indexer + Kamigaze snapshot (market/order-book reads, KWOB bootstrap) |

Everything else — including the other providers — is denied. DNS for
the allowlisted hosts is permitted; nothing agent-visible discloses the
allowlist (SPEC I1).

From kami-harness 5.0.0 the `api.kamibots.xyz` row is gone again: the
strategy-service (OUTSOURCE) tools left the harness, and nothing it serves
contacts that host.

The kami-lens daemon has egress of its own: the chain's JSON-RPC and
WebSocket endpoints, `api.prod.kamigotchi.io` (snapshot, stream and feeds)
and `state.prod.kamigotchi.io` (the world-state file a first start loads
from). Its README is the authority on that list. The keeper daemon
(`shell_keeper`) signs and reads receipts over the chain RPC its own
configuration names; its README is the authority on its list.

Not egress but the same class of bring-up obligation: the harness's
world-state reads are served by a **local kami-lens daemon over a unix
socket**, and every session opens with one of them — the
session-start brief (SPEC P1.12). A deployment where that daemon is not
running still works, by design: the brief degrades to the daemon's own
unavailability error, injected as-is, and the session proceeds (X21). It
is visible rather than silent — `tool_call` with `initiator: "scaffold"`
and `ok: false` on the first event of every session — so check that
field before concluding a run is healthy.

**Install kami-lens 1.1.0** (the pinned version; **1.0.1 at the least**).
kami-lens 1.0.0 could serve a stale value — a kami's health, a timestamp,
a harvest's state — when several transactions in one block wrote it,
with nothing marked degraded, until it was written again; 1.0.1 fixed
that; 1.0.2 and 1.0.3 keep its projection clock in step with the chain
(1.0.3 may report LIVE up to five seconds later while it does). 1.1.0
serves the facts patch 0.4.5 added — a node's yield item, requirements,
level cap and scavenge cost, a kami's `soulboundUntil` — and every value
it checks equal to the chain's own integer arithmetic. Every
`session_start` records the serving daemon's `lens_version`; check it
reads `1.0.1` or later.

**kami-lens host requirements** (its README is the authority). A
first start with no saved state loads the whole world into memory: plan
for **8 GB of RAM**. With under about 7 GB available to the process the
daemon refuses a first load (with the remedy) rather than dying part-way
through, and under 5.5 GB it refuses outright. Once running it holds
about 2.2 GB, plus about 1.6 GB more for roughly ten seconds every ten
minutes while a separate process rewrites its saved world — size the host
for that next to the harness, this scaffold and, on the shell rungs, the
environment container (`mem_limit: 2g`). It raises its own Node
heap limit to fit, which needs **Node 22.15 or newer**; on Node 20 set
`NODE_OPTIONS=--max-old-space-size=6144` yourself. **Keep its data
directory's path short:** the query socket is `<data-dir>/kami-lens.sock`,
and lens from 1.0.0 on refuses (`SOCKET_PATH_TOO_LONG`) a socket path the OS
would truncate — over 103 bytes on macOS, 107 on Linux.

## The environment container (shell rungs; SPEC D8)

- **What the scaffold assumes of it:** named by the manifest
  (`environment.container`, else `KAMI_ENV_CONTAINER`); uid 1000 exists and
  owns `/workspace`; `/workspace` is the run's `workspace/`, `/reference`
  and `/journal` the run's `reference/` and `journal/`, both read-only;
  `/srv/lens/kami-lens.sock` is a relayed lens socket, and on
  `shell_keeper` `/srv/keeper/keeper.sock` a relayed keeper socket; no
  network; docker's `--init` as PID 1, so what the agent detaches is
  re-parented and reaped; `timeout`, `bash` and `sh` on its PATH.
- **The scaffold reaches it only through the Docker Engine API** on the
  socket mounted into the agent container (`environment.docker_socket`,
  else `DOCKER_HOST`, else `/var/run/docker.sock`). The agent image needs no
  docker CLI. The scaffold is trusted code; the model's commands run only
  inside the environment, which has no socket.
- **The workspace quota bounds `workspace_write` only.** What the agent's
  shell writes into `/workspace` is bounded by the host's disk, so give
  `/srv/run/workspace` a filesystem of its own size if a run must not be
  able to fill the host. `shell_state.workspace_bytes` records it at every
  session boundary, and `get_status` reports the bytes in use.

## The harness's nonce ledger needs a persistent volume (kami-harness 4.0.0)

Every transaction the harness signs is written ahead into a small
per-wallet ledger, so that a restarted harness never hands out a nonce
under a transaction it already broadcast. It lives in its own state
directory, never holds key material, and is resolved as: `KAMI_LANE_DIR`
if set, else `$XDG_STATE_HOME/kami-harness/lanes`, else
`~/.kami-harness/lanes` (directory 0700, one JSON file per chain and
wallet address, 0600, written atomically).

**For this scaffold every session is a restart.** The harness is a stdio
child spawned per session (SPEC D1), so the ledger must survive between
sessions as well as across a container being recreated. Put it on the
same persistent volume as the run directory — the packaged image sets
`KAMI_LANE_DIR=/srv/run/harness-lanes`, which is on the `/srv/run` mount;
it is a run-directory internal no agent path can reach (SPEC P11). A
different location can be set in `/srv/run/.env` or the container
environment; the scaffold passes its environment through to the child.

What is lost without it, in the harness's own terms: the ledger starts
empty after every restart, so nonces fall back to the node's own `pending`
count, and transactions the previous process left in flight are invisible
to the new one — a nonce one of them later consumes is reported as taken
by a transaction "not signed by this harness", and a tail it left armed
behind a gap is not drained until a later send reaches it. On
`shell_keeper` the keeper signs on the same operator key from its own
process: the harness sees those transactions as another sender on the
key, which its lane handles as it handles any.

## The call time box and `tool_timeout_s`

From kami-harness 4.0.0 every served call fits a wall-clock box,
`KAMI_CALL_BUDGET_S` seconds (default **90**), a harness setting read at
startup: a loop tool whose box is spent stops at a step boundary and
returns what it did with `time_boxed: true` and `remaining`. The harness
states the box in its handshake's standing text, which every session shows
the model (SPEC D1) — so the value is part of the environment definition,
and changing it changes `session_start.harness_standing_text_sha256`.

The manifest's `caps.tool_timeout_s` (default **120**) is the scaffold's
watchdog on one tool call (SPEC P2). **It must stay above the harness's
box**, with margin for the last transaction's receipt: a watchdog that
fires first records a timeout error to the model while the harness is
still legitimately running the loop — and may still send transactions
after the model has been told the call failed. Raise `tool_timeout_s`
together with `KAMI_CALL_BUDGET_S`, never one without the other.

The `shell` tool and `wait` are exempt from that watchdog: `wait` is
bounded by `caps.wait_max_seconds`, `shell` by `caps.shell_timeout_s`
(default **120**) plus the five-second kill grace of its `timeout`
wrapper.
