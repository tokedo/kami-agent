# Packaging and provisioning (SPEC §10, D14)

One Docker image per study, identical across VMs. Per-run inputs are
injected at provision time and never baked into the image:

- `/srv/run/config.yaml` — the run manifest copy (see
  `manifests/example.yaml`); immutable per run.
- `/srv/run/.env` — provider API key(s), the owner wallet key, and
  `MAINNET_RPC_URL` (the harness refuses to start without it; the scaffold
  passes its environment through to the harness child). `kami-agent init`
  writes nothing here — there is no key path through init. Secrets live
  only in this file (hard rule 6).
- `/srv/run/reference/` — the pinned GDD snapshot (D14), read-only via
  the path sandbox.

## Bring-up

```sh
docker build -t kami-agent \
  --build-arg HARNESS_SHA=<pinned sha> \
  --build-arg GDD_REPO=<gdd repo url> --build-arg GDD_SHA=<pinned sha> .

kami-agent init --manifest /srv/run/config.yaml --run-dir /srv/run
# connectivity checks: chain RPC + mainnet RPC (eth_chainId == 1) +
# provider API + MCP handshake; emits run_start

# supervisor: fixed-cadence poller (SPEC §2)
python -c "from kami_agent.supervisor import install_cron; \
           install_cron('kami-agent run-session --run-dir /srv/run', 5)"
```

`kami-agent status --run-dir /srv/run` prints the state.json cache
(operator-facing; never an agent channel, D12).

## Egress allowlist (D14, enforced at the VM level)

The agent loop gets no web, shell, or network channel of its own. The VM
firewall allows outbound traffic ONLY to:

| destination | why |
|---|---|
| the run's model provider API — `api.anthropic.com`, `api.openai.com`, or `generativelanguage.googleapis.com` | the only per-run variable |
| the chain RPC host (manifest `chain_rpc_url`) | harness reads/writes world state |
| the mainnet RPC host (`.env` `MAINNET_RPC_URL`) | harness bridge tools (`bridge_eth_from_mainnet`, `bridge_status`) |
| `api.kamibots.xyz` | the harness's game API |
| `api.prod.kamigotchi.io` | Kamiden indexer + Kamigaze snapshot (market/order-book reads, KWOB bootstrap) |

Everything else — including the other two providers — is denied. DNS for
the allowlisted hosts is permitted; nothing agent-visible discloses the
allowlist (D12).
