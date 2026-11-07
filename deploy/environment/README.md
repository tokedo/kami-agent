# The environment of the shell rungs (SPEC D8)

On the `shell` and `shell_keeper` rungs the agent has a machine of its
own: one container per run, alive across every session, stopped when the
run ends. Its working directory and home is the run's `workspace/` — the
same tree the `workspace_*` tools serve — and what it writes there, what it
installs into it and what it leaves running persist between calls and
sessions until the run ends. It has no network and holds no key. This
directory defines that container and the services around it.

| file | what it is |
|---|---|
| `Dockerfile.env` | the environment image, one per rung: built without `KEEPER_SHA` for `shell` (no keeper client at all, so the plain rung cannot learn that a keeper exists), with it for `shell_keeper` |
| `compose.yaml` | the `shell` rung: `lens`, the `sockets` relay, `env`, `agent` |
| `compose.keeper.yaml` | the `shell_keeper` override: the `keeper` service, and its socket relayed into `env` and `agent` |
| `.env.example` | every variable the three files read |

## The boundary

Everything a model's text can reach is inside `env` — `/workspace`,
`/reference`, `/journal`, the relayed sockets (the lens's, and on
`shell_keeper` the keeper's), the installed toolset, `/tmp`; everything that
signs or pays — the keys file, the provider key, the harness, the keeper
daemon's own data, the docker socket — is outside it, in `agent`, `keeper`
and the host. What is not mounted into `env` does not exist there: not the
run directory's internals (`/srv/run`: `.env`, `state.json`,
`telemetry.jsonl`, `errors.jsonl`, `prompts/`, `transcripts/`, `shell/`,
`harness-lanes/`, `harness-cache/`), not `/srv/keys`, not the docker
socket, not the lens's or the keeper's data directories, and no network
route (`network_mode: none`).
A later loop swap adds a `harness` service reachable from `env` and replaces
`agent`; nothing here changes shape for it.

**Why a relay.** A unix socket bind-mounted as a file goes stale when its
daemon re-binds it, so a lens restart on a long run would cut the
environment off until the container was recreated — and recreating it
kills the processes the agent left running, an infrastructure artefact
inside the measurement. Mounting a daemon's data directory instead would
put the lens's saved world and the keeper's books and ledger in reach of the
agent's shell. The `sockets` service listens on sockets of its own in
root-owned directories (`/srv/sockets/{lens,keeper}`, mode 755: the agent's
user can connect to a mode-666 socket and cannot unlink it), reconnects per
request (both protocols are one request per connection), and survives
either daemon's restart. The keeper checks no peer credentials, so a relayed
peer is an ordinary peer.

## Host layout

Root-owned, mode 755 unless stated:

```
/srv/run                 the run directory (SPEC P12); the agent container's /srv/run
/srv/run/workspace       created by `kami-agent init`, then given to uid 1000
/srv/run/harness-cache   the harness's order-book cache (kwob_trades.json), the agent
                         container's /opt/kami-harness/executor/.cache; seeded by `init`
/srv/keys/.env           the keys file (0600) — KAMI_KEYS_FILE for the harness, read-only for the keeper
/srv/lens                the lens daemon's data directory (its socket: kami-lens.sock)
/srv/keeper              the keeper's data directory and socket (shell_keeper only), at the
                         same path inside the keeper container, so the paths its `status`
                         reports are the ones a client dials
/srv/sockets/lens        the relay's lens socket
/srv/sockets/keeper      the relay's keeper socket (shell_keeper only)
```

Docker Engine with the compose plugin (`docker compose`). The host
firewall is the egress allowlist of `docs/packaging.md`, applied before the
first `up`.

## Bring-up

1. **Images.** Every image is built from a pinned commit:
   - `kami-lens:<LENS_SHA>` from the kami-lens repository's own `Dockerfile`;
   - `kami-keeper:<KEEPER_SHA>` from the kami-keeper repository's own
     `Dockerfile` (it builds from a `git archive` of the pinned commit and
     refuses any other), `shell_keeper` only;
   - `kami-agent-env:<AGENT_VERSION>-<RUNG>` from `Dockerfile.env` here, with
     `LENS_SHA`, and for `shell_keeper` `KEEPER_SHA` (the clients are cloned
     from the public repositories; `LENS_REPO` / `KEEPER_REPO` override them);
   - `kami-agent:<AGENT_VERSION>` from this repository's `Dockerfile`.
2. **Per-run inputs.** `/srv/run/config.yaml` — the manifest, with
   `scaffold_profile: shell` or `shell_keeper`, `environment.container:
   kami-env` (and `environment.keeper_container: kami-keeper`), and
   `pins.keeper_sha` equal to `KEEPER_SHA`. `/srv/run/.env` — the provider
   key, `MAINNET_RPC_URL`, and the owner key only in the owner + operator
   key shape. `/srv/keys/.env` — the operator key (see the two key shapes in
   `docs/packaging.md`). `/srv/run/reference/` — the documentation snapshot.
   `.env` beside the compose files — from `.env.example`; on `shell_keeper`
   it also names the chain the keeper signs on (`KEEPER_RPC_URL`,
   `KEEPER_WS_RPC_URL`, `KEEPER_WORLD_ADDRESS`, `KEEPER_CHAIN_ID`, the
   public Yominet values as shipped). The keeper runs armed, and armed it
   starts its engine only with all of them set: without them the daemon is
   healthy and answers `status`, and nothing runs.
3. **The services the run needs before it exists:**

   ```sh
   docker compose -f compose.yaml up -d lens sockets env                 # shell
   docker compose -f compose.yaml -f compose.keeper.yaml up -d \
       lens keeper sockets env                                           # shell_keeper
   ```

4. **`init`** — validation, the connectivity checks and, at the shell
   rungs, the environment's assertions: the container runs; `command -v
   keeper` fails on `shell` and succeeds on `shell_keeper`; on
   `shell_keeper` `keeper status` answers from inside with the `lab`
   profile, the keeper container's image names `pins.keeper_sha`, and its
   engine has started (`status.engine`: an armed keeper without its chain
   settings refuses to start it, and init prints `keeper engine not
   started:` with the keeper's own reason); `kami-lens` answers from
   inside. A mismatch starts no run. Before them, on every rung, `init`
   seeds the harness's order-book cache when `/srv/run/harness-cache` has
   none (the harness's own `executor/kwob_bootstrap.py`, from the Kamigaze
   snapshot; `orderbook cache seeded (…)`, or `orderbook cache ok (…)` when
   present) — without it `get_item_orderbook` fails on every call — and a
   failed seeding starts no run.

   ```sh
   docker compose -f compose.yaml [-f compose.keeper.yaml] run --rm agent \
       kami-agent init --manifest /srv/run/config.yaml --run-dir /srv/run
   ```

5. **The loop:** `docker compose ... up -d agent`. It invokes `kami-agent
   run-session` every `POLL_SECONDS` (SPEC D4).

`docker compose -f compose.yaml [-f compose.keeper.yaml] config` validates
the definition with the variables of `.env`.

## The end of the run

At `run_complete` the scaffold itself, in order: the keeper's `stop --all`
and `revoke` for the run's account (`shell_keeper`), the last read of the
keeper's ledger, and `docker stop` of `env` with a ten-second grace — no
process the agent left running survives the run. The container is kept,
stopped, for the close-out's forensic read (`docker logs`, its writable
layer). The teardown repeats the revoke before anything is removed:

```sh
docker compose -f compose.yaml -f compose.keeper.yaml exec keeper \
    python -m keeper revoke --account main --socket /srv/keeper/keeper.sock
docker compose -f compose.yaml [-f compose.keeper.yaml] down
```

To end a run before its budget or horizon does, run the same end by
hand inside the agent container (`exec`, so the run lock's process check
sees the loop's own sessions):

```sh
docker compose -f compose.yaml [-f compose.keeper.yaml] exec agent \
    kami-agent end-run --run-dir /srv/run
```

It prints `run_complete manual` (the event's `reason` is `manual`),
`already_complete` on a run that has ended, or `lock_held` with exit 1
while a session is running — then try again after it. Every later poll of
the loop answers `already_complete`.

Pull `/srv/run` (see `docs/packaging.md`), `shell/` included.
