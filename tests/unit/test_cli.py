"""CLI: manifest → config builders, init layout + run_start, status (SPEC P12)."""

import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
from pathlib import Path

import pytest
import yaml
from fake_docker import FakeContainer, FakeDocker
from fake_keeper import FakeKeeper, Ledger, status_data, tx_row

from kami_agent import cli
from kami_agent.adapters.anthropic import AnthropicAdapter
from kami_agent.adapters.base import AdapterResponse, StopReason, ToolCall, Usage
from kami_agent.adapters.google import GoogleAdapter
from kami_agent.adapters.openai import OpenAIAdapter
from kami_agent.harness import HARNESS_DEV_PIN_SHA
from kami_agent.telemetry import read_events, validate_event

EXAMPLE = Path(__file__).parents[2] / "manifests" / "example.yaml"


@pytest.fixture
def manifest_path(tmp_path):
    manifest = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    manifest.pop("harness", None)  # no MCP child in unit tests
    manifest.pop("chain_rpc_url", None)
    # Pin the daemon socket at a path inside the test's own tmp dir. Left
    # unset it would resolve to the platform default, and the assertions
    # below would then depend on whether the machine running the suite
    # happens to have a lens daemon up.
    manifest["lens"] = {"socket_path": str(tmp_path / "absent-lens.sock"), "timeout_s": 1}
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    return path


def test_example_manifest_builds_run_config(tmp_path):
    manifest = cli.load_manifest(EXAMPLE)
    config = cli.build_run_config(manifest, tmp_path)
    assert config.model == "claude-haiku-4-5"
    assert config.prices.input_usd_per_mtok == 1.0
    assert config.caps.session_token_cap == 120_000
    assert config.caps.session_tool_cap == 50
    assert config.params.max_tokens == 4096
    assert config.params.temperature is None
    assert config.budget_usd == 10.0
    assert config.wake_min_minutes == 5
    assert config.wake_max_minutes == 1440
    assert manifest["_manifest_hash"].startswith("sha256:")


def test_build_adapter_per_provider(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    assert isinstance(cli.build_adapter({"provider": "anthropic", "model": "m"}), AnthropicAdapter)
    assert isinstance(cli.build_adapter({"provider": "openai", "model": "m"}), OpenAIAdapter)
    assert isinstance(cli.build_adapter({"provider": "google", "model": "m"}), GoogleAdapter)
    with pytest.raises(SystemExit):
        cli.build_adapter({"provider": "azure", "model": "m"})


def test_init_creates_run_layout_and_run_start(tmp_path, manifest_path, capsys):
    run_dir = tmp_path / "run"
    rc = cli.main(
        [
            "init",
            "--manifest",
            str(manifest_path),
            "--run-dir",
            str(run_dir),
            "--skip-connectivity",
        ]
    )
    assert rc == 0

    # Layout (SPEC P12): config copy, prompts, workspace, transcripts.
    config = yaml.safe_load((run_dir / "config.yaml").read_text(encoding="utf-8"))
    assert config["run_id"] == "dev-smoke-001"
    assert "_manifest_hash" not in config
    assert (run_dir / "prompts" / "system.txt").exists()
    assert (run_dir / "workspace").is_dir()
    assert (run_dir / "transcripts").is_dir()

    # No key path through init (SPEC P12): no wallet in the config
    # copy, and init writes no .env — operator creation is a harness tool.
    assert "wallet_address" not in config
    assert not (run_dir / ".env").exists()

    # run_start emitted and schema-valid; manifest_hash matches the file.
    events = list(read_events(run_dir / "telemetry.jsonl"))
    assert [e["event"] for e in events] == ["run_start"]
    validate_event(events[0])
    assert events[0]["manifest_hash"] == cli.load_manifest(manifest_path)["_manifest_hash"]
    # The example manifest's pin and the client's dev pin name one harness.
    assert events[0]["harness_sha"].startswith(HARNESS_DEV_PIN_SHA)
    out = capsys.readouterr().out
    assert "initialized" in out


def test_init_leaves_existing_env_untouched(tmp_path, manifest_path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    env_before = "ANTHROPIC_API_KEY=k\nMAINNET_RPC_URL=http://example.test\n"
    (run_dir / ".env").write_text(env_before, encoding="utf-8")
    # Pre-set via monkeypatch so load_env_file's setdefault can't leak
    # these values into other tests.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("MAINNET_RPC_URL", "http://example.test")
    cli.main(
        ["init", "--manifest", str(manifest_path), "--run-dir", str(run_dir), "--skip-connectivity"]
    )
    assert (run_dir / ".env").read_text(encoding="utf-8") == env_before


def test_init_without_mainnet_rpc_url_fails_at_bring_up(tmp_path, manifest_path, monkeypatch):
    monkeypatch.delenv("MAINNET_RPC_URL", raising=False)
    with pytest.raises(SystemExit, match="MAINNET_RPC_URL"):
        cli.main(["init", "--manifest", str(manifest_path), "--run-dir", str(tmp_path / "run")])


class FakeRpcResponse:
    def __init__(self, result):
        self._result = result

    def raise_for_status(self):
        pass

    def json(self):
        return {"result": self._result}


def test_check_mainnet_rpc_requires_chain_id_1(monkeypatch):
    monkeypatch.setattr(cli.httpx, "post", lambda url, **kwargs: FakeRpcResponse("0x1"))
    assert cli.check_mainnet_rpc("http://rpc.test") == "mainnet RPC ok (chain id 1)"
    monkeypatch.setattr(cli.httpx, "post", lambda url, **kwargs: FakeRpcResponse("0x89"))
    with pytest.raises(SystemExit, match="chain id 137"):
        cli.check_mainnet_rpc("http://rpc.test")


# --- init's lens line (SPEC D7) ----------------------------------------------------
#
# The check's job is one question — is a world-state daemon serving on that
# socket? — reported and never fatal. Its own argument-free roster query goes
# to the daemon directly, while the session-start brief is the harness's call,
# and from kami-harness 4.4.0 the harness resolves the brief's account itself.
# So what the daemon answers is not the brief, and no line may send an operator
# to configure the daemon's default operator.


class OneAnswerDaemon:
    """A unix-socket stand-in answering every request with one fixed reply."""

    def __init__(self, reply):
        # mkdtemp, not tmp_path: a unix socket path is capped near 104 bytes.
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "kami-lens.sock")
        self.requests = []
        self._reply = reply
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self.path)
        self._server.listen(4)
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            with conn:
                buffer = b""
                while b"\n" not in buffer:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buffer += chunk
                if not buffer:
                    continue
                request = json.loads(buffer.split(b"\n", 1)[0])
                self.requests.append(request)
                line = json.dumps({"id": request.get("id"), **self._reply}) + "\n"
                conn.sendall(line.encode())

    def close(self):
        self._server.close()
        shutil.rmtree(self.dir, ignore_errors=True)


DAEMON_ANSWERS = {
    "a-roster": {
        "ok": True,
        "data": {"account": {"index": 4271, "roomIndex": 11}, "kamis": [{"index": 1041}]},
        "untrusted": [],
        "meta": {"blockNumber": 8814052, "stale": False, "mode": "daemon"},
    },
    # A daemon with no default operator configured — the state a new
    # deployment's daemon is in, and one the pinned harness no longer needs
    # changed.
    "no-default-operator": {
        "ok": False,
        "error": {"code": "BAD_ARGS", "message": "account index must be a non-negative integer"},
    },
    "another-error": {
        "ok": False,
        "error": {"code": "NOT_FOUND", "message": "no such account"},
    },
}


@pytest.fixture
def lens_daemon():
    made = []

    def build(reply):
        d = OneAnswerDaemon(reply)
        made.append(d)
        return d

    yield build
    for d in made:
        d.close()


def lens_manifest(socket_path):
    return {"lens": {"socket_path": socket_path, "timeout_s": 1}}


@pytest.mark.parametrize("answer", sorted(DAEMON_ANSWERS))
def test_init_reports_a_serving_daemon_the_same_whatever_it_answers(lens_daemon, answer):
    """A roster and an error both prove a daemon is serving; neither is the brief."""
    d = lens_daemon(DAEMON_ANSWERS[answer])
    line = cli.check_lens(lens_manifest(d.path))
    assert line == f"lens ok (daemon serving at {d.path})"
    # The check's own query is unchanged: one argument-free roster query.
    assert d.requests == [{"id": 1, "query": "roster"}]


def test_init_warns_without_failing_when_no_daemon_serves(tmp_path):
    line = cli.check_lens(lens_manifest(str(tmp_path / "absent.sock")))
    assert line.startswith("lens WARNING: ")
    assert line.endswith(" — every session-start brief will degrade (D7)")


def test_init_skips_the_lens_check_when_lens_is_disabled():
    assert cli.check_lens({"lens": {"enabled": False}}) == "lens: not configured (skipped)"


def test_no_lens_line_sends_an_operator_to_set_a_default_operator(lens_daemon):
    lines = [
        cli.check_lens(lens_manifest(lens_daemon(reply).path)) for reply in DAEMON_ANSWERS.values()
    ]
    lines.append(cli.check_lens(lens_manifest("/nonexistent/kami-lens.sock")))
    lines.append(cli.check_lens({"lens": {"enabled": False}}))
    for line in lines:
        assert "operator" not in line.lower()
        assert "expected until" not in line
        assert "BAD_ARGS" not in line


def test_harness_factory_passes_environment_through(monkeypatch):
    """The harness child needs the scaffold's env (MAINNET_RPC_URL et al.)."""
    captured = {}

    class RecordingClient:
        def __init__(self, command, args, *, cwd=None, env=None, handshake_timeout_s=60.0):
            captured["env"] = env

    monkeypatch.setattr(cli, "HarnessClient", RecordingClient)
    monkeypatch.setenv("MAINNET_RPC_URL", "http://example.test")
    cli.harness_factory({"harness": {"command": "python3", "env": {"EXTRA": "1"}}})()
    assert captured["env"]["MAINNET_RPC_URL"] == "http://example.test"
    assert captured["env"]["EXTRA"] == "1"
    cli.harness_factory({"harness": {"command": "python3"}})()
    assert captured["env"]["MAINNET_RPC_URL"] == "http://example.test"


# --- presentation_mode: manifest → harness child → telemetry --------------------


@pytest.fixture
def recording_client(monkeypatch):
    captured = {}

    class RecordingClient:
        def __init__(self, command, args, *, cwd=None, env=None, handshake_timeout_s=60.0):
            captured["env"] = env

    monkeypatch.setattr(cli, "HarnessClient", RecordingClient)
    return captured


def test_presentation_mode_reaches_the_harness_child(recording_client):
    cli.harness_factory({"presentation_mode": "envelope", "harness": {"command": "python3"}})()
    assert recording_client["env"]["PRESENTATION_MODE"] == "envelope"


def test_unpinned_presentation_mode_sets_nothing(recording_client):
    """No mode pinned → the harness applies its own default; we assert none."""
    cli.harness_factory({"harness": {"command": "python3"}})()
    assert "PRESENTATION_MODE" not in recording_client["env"]


def test_presentation_mode_is_passed_through_unvalidated(recording_client):
    """A mode the harness loud-fails on must REACH the harness to fail there.

    Rejecting or normalizing it here would convert a misconfigured
    manifest into a silently different run, which is the failure mode
    this pass-through exists to prevent.
    """
    cli.harness_factory({"presentation_mode": "inline-tags", "harness": {"command": "python3"}})()
    assert recording_client["env"]["PRESENTATION_MODE"] == "inline-tags"
    cli.harness_factory({"presentation_mode": "nonsense", "harness": {"command": "python3"}})()
    assert recording_client["env"]["PRESENTATION_MODE"] == "nonsense"


def test_explicit_harness_env_still_wins(recording_client):
    cli.harness_factory(
        {
            "presentation_mode": "envelope",
            "harness": {"command": "python3", "env": {"PRESENTATION_MODE": "name-free"}},
        }
    )()
    assert recording_client["env"]["PRESENTATION_MODE"] == "name-free"


def test_example_manifest_pins_a_presentation_mode(tmp_path):
    manifest = cli.load_manifest(EXAMPLE)
    assert manifest["presentation_mode"] == "envelope"
    assert cli.build_run_config(manifest, tmp_path).presentation_mode == "envelope"


# kami-harness 4.0.0's default call box (KAMI_CALL_BUDGET_S): every served call
# returns within this many seconds of wall clock.
HARNESS_CALL_BOX_S = 90


def test_the_example_watchdog_stays_above_the_harness_call_box(tmp_path):
    """A watchdog that fires first tells the model a call failed while the
    harness is still running it (docs/packaging.md). The sample must not
    teach that configuration."""
    caps = cli.build_run_config(cli.load_manifest(EXAMPLE), tmp_path).caps
    assert caps.tool_timeout_s > HARNESS_CALL_BOX_S


class ScriptedAdapter:
    def complete(self, system, messages, tools, params):
        return AdapterResponse(
            text_blocks=(),
            tool_calls=(ToolCall(id="t-end", name="end_session", args={"reason": "done"}),),
            stop_reason=StopReason.TOOL_USE,
            usage=Usage(input_tokens=100, output_tokens=10),
        )


def test_run_session_command_end_to_end(tmp_path, manifest_path, monkeypatch, capsys):
    run_dir = tmp_path / "run"
    (tmp_path / "run" / "reference").mkdir(parents=True)
    (tmp_path / "run" / "reference" / "gdd.md").write_text("lore")
    cli.main(
        ["init", "--manifest", str(manifest_path), "--run-dir", str(run_dir), "--skip-connectivity"]
    )
    monkeypatch.setattr(cli, "build_adapter", lambda manifest: ScriptedAdapter())
    rc = cli.main(["run-session", "--run-dir", str(run_dir), "--manual"])
    assert rc == 0
    assert "session_ran" in capsys.readouterr().out
    records = list(read_events(run_dir / "telemetry.jsonl"))
    assert [e["event"] for e in records] == [
        "run_start",
        "session_start",
        # No harness is configured in this manifest, so the roster and
        # balance injections are skipped entirely — with no surface there
        # is nothing to ask (SPEC X20). The journal read remains: it needs
        # neither a daemon nor a harness (P15).
        "tool_call",
        "llm_request",
        "llm_call",
        "tool_call",
        "session_end",
        "schedule_next",
    ]
    journal_read = records[2]
    assert journal_read["source"] == "scaffold"
    assert journal_read["initiator"] == "scaffold"
    # Session 1 has no journal yet: the ordinary not-found result, which
    # is the visible shape a fresh run is supposed to have (P15, X21).
    assert journal_read["ok"] is False


def test_status_prints_state(tmp_path, manifest_path, capsys):
    run_dir = tmp_path / "run"
    cli.main(
        ["init", "--manifest", str(manifest_path), "--run-dir", str(run_dir), "--skip-connectivity"]
    )
    capsys.readouterr()  # drop init output
    rc = cli.main(["status", "--run-dir", str(run_dir)])
    assert rc == 0
    status = json.loads(capsys.readouterr().out)
    assert status["session_counter"] == 0
    assert status["run_status"] == "active"


def test_load_env_file_existing_env_wins(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("A_KEY=from_file\n# comment\nB_KEY=b\n")
    monkeypatch.setenv("A_KEY", "from_env")
    monkeypatch.delenv("B_KEY", raising=False)
    cli.load_env_file(env)
    import os

    assert os.environ["A_KEY"] == "from_env"
    assert os.environ["B_KEY"] == "b"
    monkeypatch.delenv("B_KEY")


# --- 0.8.2 (F-TS7): init seeds the harness's order-book cache ------------------------------


@pytest.fixture
def harness_tree(tmp_path):
    """A harness checkout as the manifest names it: cwd + executor/server.py."""
    root = tmp_path / "kami-harness"
    executor = root / "executor"
    executor.mkdir(parents=True)
    (executor / "server.py").write_text("")
    (executor / "kwob_bootstrap.py").write_text("")
    manifest = {
        "harness": {"command": "python3", "args": ["executor/server.py"], "cwd": str(root)},
    }
    return manifest, executor / ".cache" / "kwob_trades.json"


def write_cache(path, n, block):
    path.parent.mkdir(parents=True, exist_ok=True)
    trade_ids = [hex(0x10 + i) for i in range(n)]
    path.write_text(json.dumps({"block": block, "trade_ids": trade_ids}))


def test_a_present_order_book_cache_is_reported_and_not_reseeded(harness_tree, monkeypatch):
    manifest, cache = harness_tree
    write_cache(cache, 2, 123)
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append((a, k)))
    assert cli.check_orderbook_cache(manifest) == "orderbook cache ok (2 trade ids, block 123)"
    assert calls == []


def test_a_missing_order_book_cache_is_seeded_by_the_harness_script(harness_tree, monkeypatch):
    manifest, cache = harness_tree
    calls = []

    def bootstrap(cmd, **kwargs):
        calls.append((cmd, kwargs))
        write_cache(cache, 3, 456)
        return subprocess.CompletedProcess(cmd, 0, stdout="wrote it\n", stderr="")

    monkeypatch.setattr(subprocess, "run", bootstrap)
    assert cli.check_orderbook_cache(manifest) == "orderbook cache seeded (3 trade ids, block 456)"
    ((cmd, kwargs),) = calls
    executor = cache.parent.parent
    assert cmd == ["python3", str(executor / "kwob_bootstrap.py")]
    assert kwargs["cwd"] == manifest["harness"]["cwd"]
    assert kwargs["timeout"] == 300


def test_a_failed_seeding_fails_init_with_the_scripts_error(harness_tree, monkeypatch):
    manifest, cache = harness_tree

    def bootstrap(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 1, stdout="", stderr="Traceback ...\nhttpx.ConnectError: name resolution failed\n"
        )

    monkeypatch.setattr(subprocess, "run", bootstrap)
    with pytest.raises(SystemExit) as failed:
        cli.check_orderbook_cache(manifest)
    assert "orderbook cache seeding failed (exit 1)" in str(failed.value)
    assert "httpx.ConnectError: name resolution failed" in str(failed.value)
    assert not cache.exists()


def test_a_seeding_that_times_out_fails_init(harness_tree, monkeypatch):
    manifest, _ = harness_tree

    def bootstrap(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", bootstrap)
    with pytest.raises(SystemExit, match="timed out after 300 s"):
        cli.check_orderbook_cache(manifest)


def test_init_fails_before_run_start_when_seeding_fails(
    tmp_path, manifest_path, harness_tree, monkeypatch
):
    """An operator's problem, found at bring-up: no run exists afterwards."""
    harness_manifest, _ = harness_tree
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest.update(harness_manifest)
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    monkeypatch.setenv("MAINNET_RPC_URL", "http://example.test")
    monkeypatch.setattr(cli, "check_mainnet_rpc", lambda url: "mainnet RPC ok (chain id 1)")
    monkeypatch.setattr(cli, "check_provider", lambda m: "provider API ok")
    monkeypatch.setattr(cli, "check_harness", lambda m: ("harness ok", []))
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **k: subprocess.CompletedProcess(cmd, 2, stdout="", stderr="boom"),
    )
    run_dir = tmp_path / "run"
    with pytest.raises(SystemExit, match="orderbook cache seeding failed"):
        cli.main(["init", "--manifest", str(manifest_path), "--run-dir", str(run_dir)])
    assert not (run_dir / "telemetry.jsonl").exists()


def test_init_skips_the_order_book_cache_check_without_connectivity_checks(
    tmp_path, manifest_path, capsys, monkeypatch
):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("ran the bootstrap"))
    cli.main(
        [
            "init",
            "--manifest",
            str(manifest_path),
            "--run-dir",
            str(tmp_path / "run"),
            "--skip-connectivity",
        ]
    )
    assert "orderbook cache check skipped" in capsys.readouterr().out.splitlines()


# --- 0.8.2 (F-TS5): the operator-side run end --------------------------------------------


class OneBookKeeper:
    """A keeper holding one grant and one running book with one final receipt."""

    def __init__(self):
        self.grants = ["main"]
        self.state = "running"
        self.ledger = Ledger(
            {
                "auto-1": [
                    tx_row(
                        "auto-1",
                        "0x" + "7" * 64,
                        stage="receipt",
                        status="success",
                        lens_block=100,
                        block=101,
                    )
                ]
            }
        )

    def __call__(self, cmd, args):
        if cmd == "stop":
            stopped = ["auto-1"] if self.state == "running" else []
            self.state = "stopped"
            return {"stopped": stopped, "quiescence": {"quiescent": True}}
        if cmd == "revoke":
            had = bool(self.grants)
            self.grants = []
            return {"revoked": had, "stopped": [], "quiescence": {"quiescent": True}}
        if cmd == "status":
            return status_data(grants=self.grants)
        return self.ledger(cmd, args)


@pytest.fixture
def keeper_run(tmp_path, manifest_path, monkeypatch):
    """A shell_keeper run directory initialized against a fake docker and keeper."""
    disabled = []
    monkeypatch.setattr(cli, "uninstall_cron", lambda: disabled.append(True))
    container = FakeContainer("kami-env", image="kami-agent-env:0.8.2-shell_keeper")
    with FakeDocker(container) as docker, FakeKeeper(OneBookKeeper()) as keeper:
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        manifest["scaffold_profile"] = "shell_keeper"
        manifest["environment"] = {
            "container": "kami-env",
            "keeper_container": "kami-keeper",
            "docker_socket": docker.socket_path,
            "keeper_socket": keeper.socket_path,
        }
        manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
        run_dir = tmp_path / "run"
        assert (
            cli.main(
                [
                    "init",
                    "--manifest",
                    str(manifest_path),
                    "--run-dir",
                    str(run_dir),
                    "--skip-connectivity",
                ]
            )
            == 0
        )
        yield run_dir, container, keeper, disabled


def test_end_run_ends_the_run_as_the_boundary_does_with_reason_manual(keeper_run, capsys):
    run_dir, container, keeper, disabled = keeper_run
    capsys.readouterr()
    assert cli.main(["end-run", "--run-dir", str(run_dir)]) == 0
    assert capsys.readouterr().out.splitlines() == ["run_complete manual"]
    events = list(read_events(run_dir / "telemetry.jsonl"))
    for event in events:
        validate_event(event)
    assert [e["event"] for e in events] == [
        "run_start",
        "tool_call",
        "tool_call",
        "keeper_tx",
        "shell_state",
        "run_complete",
    ]
    stop_row, revoke_row = events[1], events[2]
    assert (stop_row["tool"], stop_row["source"], stop_row["initiator"]) == (
        "keeper_stop_all",
        "keeper",
        "scaffold",
    )
    assert stop_row["keeper_books_stopped"] == 1
    assert revoke_row["tool"] == "keeper_revoke"
    assert revoke_row["keeper_grants_after"] == 0
    assert keeper.commands[:2] == ["stop", "revoke"]
    assert events[3]["tx_hash"] == "0x" + "7" * 64
    assert events[4]["phase"] == "run_complete"
    complete = events[5]
    assert complete["reason"] == "manual"
    assert complete["environment_stopped"] is True
    assert complete["keeper_grants_after"] == 0
    assert complete["totals"] == {
        "sessions": 0,
        "llm_calls": 0,
        "cumulative_usd": 0.0,
        "cumulative_tokens": 0,
        "overspend_usd": 0.0,
    }
    assert container.stops == 1
    assert json.loads((run_dir / "state.json").read_text())["run_status"] == "complete"
    assert not (run_dir / "run.lock").exists()
    assert disabled == [True]


def test_end_run_twice_is_already_complete_and_writes_nothing(keeper_run, capsys):
    run_dir, container, keeper, _ = keeper_run
    assert cli.main(["end-run", "--run-dir", str(run_dir)]) == 0
    before = (run_dir / "telemetry.jsonl").read_bytes()
    asked = len(keeper.requests)
    capsys.readouterr()
    assert cli.main(["end-run", "--run-dir", str(run_dir)]) == 0
    assert capsys.readouterr().out.splitlines() == ["already_complete"]
    assert (run_dir / "telemetry.jsonl").read_bytes() == before
    assert len(keeper.requests) == asked
    assert container.stops == 1
    # And a scheduled poll after it finds the run complete, as after a boundary.
    assert cli.main(["run-session", "--run-dir", str(run_dir)]) == 0
    assert capsys.readouterr().out.splitlines() == ["already_complete"]


def test_end_run_refuses_while_a_session_holds_the_lock(keeper_run, capsys):
    run_dir, container, keeper, _ = keeper_run
    lock = run_dir / "run.lock"
    lock.write_text(json.dumps({"pid": os.getpid(), "created": "2999-01-01T00:00:00+00:00"}))
    capsys.readouterr()
    assert cli.main(["end-run", "--run-dir", str(run_dir)]) == 1
    assert capsys.readouterr().out.splitlines() == ["lock_held"]
    assert [e["event"] for e in read_events(run_dir / "telemetry.jsonl")] == ["run_start"]
    assert keeper.requests == []
    assert lock.exists()
