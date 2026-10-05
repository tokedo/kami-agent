# Packaging and provisioning (SPEC P12)

One Docker image per study, identical across VMs. Per-run inputs are
injected at provision time and never baked into the image:

- `/srv/run/config.yaml` — the run manifest copy (see
  `manifests/example.yaml`); immutable per run.
- `/srv/run/.env` — provider API key(s), the owner wallet key, and
  `MAINNET_RPC_URL` (the harness refuses to start without it; the scaffold
  passes its environment through to the harness child). `kami-agent init`
  writes nothing here — there is no key path through init. Secrets live
  only in this file, and never in git. kami-harness 4.0.0 reads no
  third-party strategy-service key any more: a `{LABEL}_KAMIBOTS_API_KEY`
  or `{LABEL}_PRIVY_ID` left in an old `.env` is ignored and can be
  removed.
- `/srv/run/reference/` — the pinned GDD snapshot (SPEC D5), read-only via
  the path sandbox.

## Bring-up

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

`GDD_SHA` is the manifest's `pins.gdd_sha`; `manifests/example.yaml`
names the kamigotchi-gdd commit that matches its harness and lens pins.

`kami-agent status --run-dir /srv/run` prints the state.json cache
(operator-facing; never an agent channel — SPEC I1).

## What to pull off a host, and in what order (SPEC P14, P15)

- `/srv/run/telemetry.jsonl` — the source of truth for all accounting.
  Any `session_refused` line in it means sessions are not starting at all
  (SPEC P1 step 9): under the scheduler it repeats every poll until the
  deployment is fixed, so alert on it.
- `/srv/run/errors.jsonl` — **pull this first after any incident.** One
  line per failed provider call, written at the moment of the failure with
  the provider's own status, error type and message. It exists because a
  host may be gone, or its process dead, before anyone thinks to ask what
  the provider actually said; nothing else on the machine records it, and
  the telemetry copy is cut short for bulk reading.
- `/srv/run/transcripts/` — messages exactly as sent. Injected
  session-start turns carry `initiator: scaffold`; everything without that
  key is the model's own.
- `/srv/run/journal/sessions.jsonl` — the scaffold's own per-session
  record. Derived from telemetry rather than authoritative, and rolled to
  a size bound, so it is the agent's view of its past and not an archive.
- `/srv/run/workspace/` — everything the agent wrote for itself.

## Egress allowlist (SPEC I20/N6, enforced at the VM level)

The agent loop gets no web, shell, or network channel of its own. The VM
firewall allows outbound traffic ONLY to:

| destination | why |
|---|---|
| the run's model provider API — `api.anthropic.com`, `api.openai.com`, or `generativelanguage.googleapis.com` | the only per-run variable |
| the chain RPC host (manifest `chain_rpc_url`) | harness reads/writes world state |
| the mainnet RPC host (`.env` `MAINNET_RPC_URL`) | harness bridge tools (`bridge_eth_from_mainnet`, `bridge_status`) |
| `router-api.initia.xyz` | the bridge route the harness's `bridge_eth_from_mainnet` asks for before it signs |
| `api.prod.kamigotchi.io` | Kamiden indexer + Kamigaze snapshot (market/order-book reads, KWOB bootstrap) |

Everything else — including the other two providers — is denied. DNS for
the allowlisted hosts is permitted; nothing agent-visible discloses the
allowlist (SPEC I1).

From kami-harness 4.0.0 there is no `api.kamibots.xyz` row: the
third-party strategy-service tools left the surface, and nothing the
harness serves contacts that host. A firewall that still allows it is
allowing a host nothing in the run uses.

The kami-lens daemon (below) has egress of its own when it runs on the
same VM: the chain's JSON-RPC and WebSocket endpoints, `api.prod.kamigotchi.io`
(snapshot, stream and feeds) and `state.prod.kamigotchi.io` (the world-state
file a first start loads from). Its README is the authority on that list.

Not egress but the same class of bring-up obligation: the harness's
world-state reads are served by a **local kami-lens daemon over a unix
socket**, and every session now opens with one of them — the
session-start brief (SPEC P1.12). A deployment where that daemon is not
running still works, by design: the brief degrades to the daemon's own
unavailability error, injected as-is, and the session proceeds (X21). It
is visible rather than silent — `tool_call` with `initiator: "scaffold"`
and `ok: false` on the first event of every session — so check that
field before concluding a run is healthy.

**Install kami-lens 1.0.3** (the pinned version; **1.0.1 at the least**).
kami-lens 1.0.0 could serve a stale value — a kami's health, a timestamp,
a harvest's state — when several transactions in one block wrote it,
with nothing marked degraded, until it was written again; 1.0.1 fixed
that; 1.0.2 and 1.0.3 keep its projection clock in step with the chain
(1.0.3 may report LIVE up to five seconds later while it does). A
1.0.0 daemon already running needs only an upgrade and a restart: its
saved world never held the stale values, so nothing has to be deleted.
Every `session_start` records the serving daemon's `lens_version`; check
it reads `1.0.1` or later.

**kami-lens host requirements** (its README is the authority; unchanged
from 1.0.0 to 1.0.3). A
first start with no saved state loads the whole world into memory: plan
for **8 GB of RAM**. With under about 7 GB available to the process the
daemon refuses a first load (with the remedy) rather than dying part-way
through, and under 5.5 GB it refuses outright. Once running it holds
about 2.2 GB, plus about 1.6 GB more for roughly ten seconds every ten
minutes while a separate process rewrites its saved world — size the VM
for that next to the harness and this scaffold. It raises its own Node
heap limit to fit, which needs **Node 22.15 or newer**; on Node 20 set
`NODE_OPTIONS=--max-old-space-size=6144` yourself. A restart that resumes
from its saved world needs far less. Its `status` answer reports the heap
limit it ended up with and who chose it. **Keep its data directory's path
short:** the query socket is `<data-dir>/kami-lens.sock`, and lens from
1.0.0 on refuses (`SOCKET_PATH_TOO_LONG`) a socket path the OS would truncate —
over 103 bytes on macOS, 107 on Linux — rather than bind one no client
could find; the harness and this scaffold connect to that same path.

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
behind a gap is not drained until a later send reaches it.

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
