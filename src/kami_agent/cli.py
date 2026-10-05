"""CLI entry points: kami-agent init | run-session | status (SPEC P12).

- ``init``: validate the manifest, write the run directory from it, run
  connectivity checks (chain RPC, mainnet RPC, provider API, MCP
  handshake), emit ``run_start``. init never generates, imports, or
  writes any key — operator-wallet creation is a harness tool
  (``create_operator_wallet``) the agent calls in-run, and the key is
  generated and persisted inside the harness server process.
- ``run-session``: execute one session — what the supervisor's cron
  entry invokes.
- ``status``: print the state.json summary. Operator-facing only; never
  an agent channel (I1).

The manifest is a YAML file copied verbatim into ``run/config.yaml``;
see ``manifests/example.yaml``. Secrets (provider API key, owner wallet
key) plus ``MAINNET_RPC_URL`` live in the run-dir ``.env``, injected at
provision time — never in the manifest, config copy, or telemetry.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import asdict
from importlib import resources
from pathlib import Path
from typing import Any

import httpx
import yaml

from kami_agent.adapters.anthropic import AnthropicAdapter
from kami_agent.adapters.base import ModelAdapter, SamplingParams, UserMessage
from kami_agent.adapters.google import GoogleAdapter
from kami_agent.adapters.openai import OpenAIAdapter
from kami_agent.governor import PriceTable
from kami_agent.harness import HarnessClient, HarnessPairingError, check_pairing
from kami_agent.lens import ROSTER_QUERY, LensClient, LensQueryError, LensUnavailableError
from kami_agent.loop import BALANCE_TOOL, BRIEF_TOOL, LoopCaps
from kami_agent.runner import RunConfig, run_session
from kami_agent.state import load_state
from kami_agent.supervisor import uninstall_cron
from kami_agent.telemetry import TelemetryWriter
from kami_agent.tools.scaffold import DEFAULT_PROFILE, PROFILES

PROVIDERS = ("anthropic", "openai", "google")

# Every pinned prompt asset: the three frozen strings plus the profile
# appendices (SPEC P13). `init` materializes all of them regardless of
# profile, so one run directory can be inspected against any rung and a
# profile can never be selected without its asset on disk.
PROMPT_NAMES = (
    "system.txt",
    "kickoff.txt",
    "continue.txt",
    "orientation.txt",
    "planning.txt",
)


def _prompts_source() -> Any:
    """The frozen prompt files: package data first, dev checkout fallback.

    The canonical copies ship inside the wheel (kami_agent/prompts) so
    ``init`` works under any install, including the Docker image; the
    repo-root ``prompts/`` tree remains for the brief's layout and is
    kept byte-identical by a unit test.
    """
    packaged = resources.files("kami_agent") / "prompts"
    if (packaged / "system.txt").is_file():
        return packaged
    return Path(__file__).resolve().parents[2] / "prompts"


# --- manifest ------------------------------------------------------------------


def load_manifest(path: str | Path) -> dict[str, Any]:
    raw = Path(path).read_bytes()
    manifest = yaml.safe_load(raw)
    manifest["_manifest_hash"] = "sha256:" + hashlib.sha256(raw).hexdigest()
    return manifest


def build_run_config(manifest: dict[str, Any], run_dir: Path) -> RunConfig:
    params = manifest.get("params", {})
    caps = manifest.get("caps", {})
    wake = manifest.get("wake", {})
    prices = manifest["price_table"]
    # Validated here, before any lock, telemetry, or session number: an
    # unknown rung is a manifest error, not a run to start (as with an
    # unknown provider). Absent = control, which is the 0.4.0 scaffold
    # plus gas visibility.
    profile = manifest.get("scaffold_profile", DEFAULT_PROFILE)
    if profile not in PROFILES:
        raise SystemExit(f"unknown scaffold_profile {profile!r}; expected one of {PROFILES}")
    return RunConfig(
        run_dir=run_dir,
        run_id=manifest["run_id"],
        model=manifest["model"],
        prices=PriceTable(
            input_usd_per_mtok=prices["input_usd_per_mtok"],
            output_usd_per_mtok=prices["output_usd_per_mtok"],
            # Absent cache columns → cached tokens bill at the input rate
            # (conservative pre-caching behavior; see PriceTable).
            cache_read_usd_per_mtok=prices.get("cache_read_usd_per_mtok"),
            cache_write_usd_per_mtok=prices.get("cache_write_usd_per_mtok"),
        ),
        caps=LoopCaps(**caps),
        params=SamplingParams(
            max_tokens=params.get("max_tokens", 4096),
            temperature=params.get("temperature"),
            reasoning_effort=params.get("reasoning_effort"),
        ),
        budget_usd=manifest.get("budget_usd", 10.0),
        t_max_days=manifest.get("t_max_days", 30.0),
        wake_min_minutes=wake.get("min_minutes", 5.0),
        wake_max_minutes=wake.get("max_minutes", 24 * 60.0),
        wake_default_minutes=wake.get("default_minutes", 60.0),
        workspace_quota_bytes=manifest.get("workspace_quota_bytes", 10 * 1024 * 1024),
        lock_stale_s=manifest.get("lock_stale_s", 7200.0),
        presentation_mode=manifest.get("presentation_mode"),
        scaffold_profile=profile,
    )


def build_adapter(manifest: dict[str, Any]) -> ModelAdapter:
    provider = manifest["provider"]
    model = manifest["model"]
    if provider == "anthropic":
        return AnthropicAdapter(model)
    if provider == "openai":
        return OpenAIAdapter(model)
    if provider == "google":
        return GoogleAdapter(model, api_key=os.environ.get("GEMINI_API_KEY"))
    raise SystemExit(f"unknown provider {provider!r}; expected one of {PROVIDERS}")


def harness_factory(manifest: dict[str, Any]):
    harness = manifest.get("harness")
    if not harness:
        return None

    # The harness selects its presentation mode from PRESENTATION_MODE in
    # its own environment, read once at server import. The manifest value
    # is passed through UNVALIDATED and UNCAUGHT: a mode the harness does
    # not implement must abort the handshake loudly at bring-up, which is
    # what `init`'s connectivity check surfaces. Normalizing or defaulting
    # it here would turn a misconfigured manifest into a silently
    # different run. An explicit harness.env entry still wins.
    child_env: dict[str, str] = {}
    mode = manifest.get("presentation_mode")
    if mode is not None:
        child_env["PRESENTATION_MODE"] = str(mode)

    def factory() -> HarnessClient:
        # Always pass the scaffold's environment through: the MCP SDK's
        # default child env drops it, and the harness refuses to start
        # without MAINNET_RPC_URL (v1.3.0+). Manifest harness.env wins.
        return HarnessClient(
            harness["command"],
            list(harness.get("args", [])),
            cwd=harness.get("cwd"),
            env={**os.environ, **child_env, **harness.get("env", {})},
            handshake_timeout_s=harness.get("handshake_timeout_s", 60.0),
        )

    return factory


def lens_factory(manifest: dict[str, Any]):
    """Build the world-state daemon client for the scaffold's own reads (D7).

    Those are operator-side only: the per-session provenance query and
    ``init``'s connectivity check. The session-start brief is the
    harness's roster call and never goes through this client.

    Returns a factory unless ``lens.enabled: false``: the socket path
    resolves from the manifest, then the environment, then the platform
    default, and a client opens no connection until it is queried.
    """
    lens = manifest.get("lens") or {}
    if lens.get("enabled") is False:
        return None

    def factory() -> LensClient:
        return LensClient(
            lens.get("socket_path"),
            timeout_s=lens.get("timeout_s", 30.0),
            # Mirrors what the harness sends under the same pinned mode, so
            # this client's reads and the harness's world-state reads ask
            # the daemon for the same composition.
            no_authored=manifest.get("presentation_mode") == "name-free",
        )

    return factory


def load_env_file(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines); existing env vars win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


# --- connectivity checks (init) ---------------------------------------------------


def check_chain_rpc(rpc_url: str) -> str:
    response = httpx.post(
        rpc_url,
        json={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []},
        timeout=30,
    )
    response.raise_for_status()
    block = int(response.json()["result"], 16)
    return f"chain RPC ok (block {block})"


def check_mainnet_rpc(rpc_url: str) -> str:
    """The harness requires MAINNET_RPC_URL at startup; verify it at bring-up."""
    response = httpx.post(
        rpc_url,
        json={"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []},
        timeout=30,
    )
    response.raise_for_status()
    chain_id = int(response.json()["result"], 16)
    if chain_id != 1:
        raise SystemExit(
            f"MAINNET_RPC_URL answered chain id {chain_id}, expected 1 (Ethereum mainnet)"
        )
    return "mainnet RPC ok (chain id 1)"


def check_provider(manifest: dict[str, Any]) -> str:
    adapter = build_adapter(manifest)
    response = adapter.complete(
        "Connectivity check.", [UserMessage(text="ping")], [], SamplingParams(max_tokens=16)
    )
    usage = response.usage
    return f"provider API ok ({usage.input_tokens} in / {usage.output_tokens} out)"


def check_lens(manifest: dict[str, Any]) -> str:
    """Bring-up check: is a world-state daemon serving on that socket (D7)?

    Sends one argument-free roster query straight to the daemon and
    reports one of two states. Neither is fatal:

    - **unreachable** — no daemon on that socket. Every session's brief
      will degrade, because the harness reads the same daemon. Reported,
      not fatal: X21 says an unavailable read-side daemon must not end
      sessions.
    - **serving** — the daemon answered, with a roster or with an error.
      Either answer proves the same thing, so both print the same line.

    What the daemon answered is not reported, because it is not the
    session-start brief. The brief is the harness's own roster call, and
    the harness decides which account a call with no argument reads —
    from kami-harness 4.4.0 the run's own account, which needs nothing
    from the daemon's configuration — while this query goes to the daemon
    directly and is answered for whatever account the daemon itself would
    choose.
    """
    factory = lens_factory(manifest)
    if factory is None:
        return "lens: not configured (skipped)"
    client = factory()
    try:
        client.query(ROSTER_QUERY)
    except LensUnavailableError as exc:
        return f"lens WARNING: {exc.message} — every session-start brief will degrade (D7)"
    except LensQueryError:
        pass
    return f"lens ok (daemon serving at {client.socket_path})"


def check_harness(manifest: dict[str, Any]) -> tuple[str, list[str]]:
    """Bring-up check for the harness surface, including the two tools the
    scaffold depends on by name.

    The session-start gas balances are read from the harness's own balance
    tool (SPEC D1): only the harness knows the run's wallet addresses. That
    coupling is asserted HERE, loudly, at bring-up — never at runtime,
    where a surface without the tool degrades visibly instead of refusing
    to run (N10, X21).

    The roster tool is the other one, and it is stricter: from 0.6.0 the
    session-start brief is a call of it, and a surface without it does not
    degrade — it refuses the session at loop construction, because a
    session that cannot see its own kamis is a session pointed at the
    wrong environment. Reported here so the failure lands at bring-up,
    where an operator is looking, rather than at the first wake.
    """
    factory = harness_factory(manifest)
    if factory is None:
        return "harness: not configured (skipped)", []
    client = factory()
    try:
        # The pairing rule (D1): a harness that states its standing text
        # only in the handshake must have delivered it. Refused here, at
        # bring-up, with the same plain message every session would give.
        try:
            check_pairing(client.harness_schema_version, client.standing_text)
        except HarnessPairingError as exc:
            raise SystemExit(str(exc)) from exc
        names = [t.name for t in client.tool_defs]
        line = f"harness ok ({client.server_name} {client.server_version}, {len(names)} tools)"
        if client.standing_text:
            line += (
                f"; standing text {len(client.standing_text)} chars "
                "(shown in every session's system prompt)"
            )
        if BALANCE_TOOL in names:
            line += f"; {BALANCE_TOOL} present (session-start gas balances)"
        else:
            line += (
                f"; harness WARNING: this surface has no {BALANCE_TOOL} — every "
                "session's gas-balance injection will degrade visibly (D1)"
            )
        if BRIEF_TOOL in names:
            line += f"; {BRIEF_TOOL} present (session-start roster)"
        else:
            line += (
                f"; harness ERROR: this surface has no {BRIEF_TOOL} — this scaffold "
                "requires a harness serving it (kami-harness 3.0.0+) and will "
                "refuse every session against this pin (D1)"
            )
        return line, names
    finally:
        client.close()


# --- commands ----------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    run_dir = Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    load_env_file(run_dir / ".env")

    # No key path through init: the operator wallet is created in-run by
    # the harness tool create_operator_wallet, and the owner key arrives
    # in .env at provision time. init only validates and scaffolds.
    config = {k: v for k, v in manifest.items() if not k.startswith("_")}
    (run_dir / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    # Frozen prompts + agent-owned workspace; reference/ is provisioned by
    # the image (SPEC D5) — warn, don't fail, when absent in dev.
    prompts_dir = run_dir / "prompts"
    if not prompts_dir.exists():
        prompts_dir.mkdir()
        source = _prompts_source()
        for name in PROMPT_NAMES:
            (prompts_dir / name).write_text(
                (source / name).read_text(encoding="utf-8"), encoding="utf-8"
            )
    (run_dir / "workspace").mkdir(exist_ok=True)
    (run_dir / "transcripts").mkdir(exist_ok=True)
    if not (run_dir / "reference").exists():
        print("warning: reference/ missing — provision the GDD snapshot", file=sys.stderr)

    harness_tool_names: list[str] = []
    if args.skip_connectivity:
        print("connectivity checks skipped")
    else:
        rpc_url = manifest.get("chain_rpc_url")
        if rpc_url:
            print(check_chain_rpc(rpc_url))
        mainnet_rpc_url = os.environ.get("MAINNET_RPC_URL")
        if not mainnet_rpc_url:
            raise SystemExit(
                "MAINNET_RPC_URL is not set — the harness refuses to start "
                "without it; set it in the run-dir .env"
            )
        print(check_mainnet_rpc(mainnet_rpc_url))
        print(check_provider(manifest))
        harness_line, harness_tool_names = check_harness(manifest)
        print(harness_line)
        print(check_lens(manifest))

    pins = manifest.get("pins", {})
    with TelemetryWriter(run_dir / "telemetry.jsonl", run_id=manifest["run_id"]) as writer:
        writer.emit(
            "run_start",
            session=0,
            manifest_hash=manifest["_manifest_hash"],
            model=manifest["model"],
            harness_sha=pins.get("harness_sha", ""),
            agent_sha=pins.get("agent_sha", ""),
            gdd_sha=pins.get("gdd_sha", ""),
            harness_tools=harness_tool_names,
            price_table=manifest["price_table"],
        )
    print(f"initialized {run_dir} (run_id {manifest['run_id']})")
    return 0


def cmd_run_session(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    load_env_file(run_dir / ".env")
    manifest = load_manifest(run_dir / "config.yaml")
    config = build_run_config(manifest, run_dir)
    adapter = build_adapter(manifest)
    try:
        outcome = run_session(
            config,
            adapter,
            harness_factory=harness_factory(manifest),
            lens_factory=lens_factory(manifest),
            trigger="manual" if args.manual else "scheduled",
            disable_supervisor=uninstall_cron,
        )
    except HarnessPairingError as exc:
        # A plain message and a non-zero exit, not a traceback: this is an
        # operator's deployment to fix, and nothing ran (D1).
        raise SystemExit(str(exc)) from exc
    print(outcome)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    state = load_state(Path(args.run_dir) / "state.json")
    print(json.dumps(asdict(state), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="kami-agent", description="KamiBench reference agent scaffold (SPEC P12)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="create a run directory from a manifest")
    p_init.add_argument("--manifest", required=True)
    p_init.add_argument("--run-dir", required=True)
    p_init.add_argument(
        "--skip-connectivity",
        action="store_true",
        help="skip the chain RPC / provider API / MCP handshake checks (dev only)",
    )
    p_init.set_defaults(func=cmd_init)

    p_run = sub.add_parser("run-session", help="execute one session (supervisor entry)")
    p_run.add_argument("--run-dir", required=True)
    p_run.add_argument("--manual", action="store_true", help="bypass wake gating")
    p_run.set_defaults(func=cmd_run_session)

    p_status = sub.add_parser("status", help="print the state.json summary")
    p_status.add_argument("--run-dir", required=True)
    p_status.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
