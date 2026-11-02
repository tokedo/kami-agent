"""The environment's lifecycle in the scaffold (SPEC D8, P1, P12; 0.8.0).

`init`'s assertions at ≥ shell, the `shell_state` record at every session
boundary, and the end of the run — the keeper's books stopped and its
grant revoked, the environment stopped — against a stand-in Docker Engine
API and a stand-in keeper socket.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fake_docker import ExecReply, FakeContainer, FakeDocker
from fake_keeper import FakeKeeper, Ledger, Refuse, tx_row

from kami_agent import cli
from kami_agent.adapters.base import AdapterResponse, SamplingParams, StopReason, ToolCall, Usage
from kami_agent.docker_api import DockerClient
from kami_agent.environment import Environment, EnvironmentCheckError, image_matches_pin
from kami_agent.governor import PriceTable
from kami_agent.keeper import KeeperClient
from kami_agent.loop import LoopCaps
from kami_agent.runner import RUN_COMPLETED, SESSION_RAN, RunConfig, run_session
from kami_agent.telemetry import read_events, validate_event

PRICES = PriceTable(input_usd_per_mtok=3.0, output_usd_per_mtok=15.0)
T0 = datetime(2026, 10, 23, 12, 0, 0, tzinfo=UTC)
KEEPER_PIN = "bbe3929"


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


def response(*calls):
    return AdapterResponse(
        text_blocks=(),
        tool_calls=tuple(calls),
        stop_reason=StopReason.TOOL_USE if calls else StopReason.END_TURN,
        usage=Usage(input_tokens=10, output_tokens=5),
    )


class ScriptedAdapter:
    def __init__(self, *responses):
        self._responses = list(responses)
        self.requests = []

    def complete(self, system, messages, tools, params):
        self.requests.append({"system": system, "messages": list(messages), "tools": tools})
        return self._responses.pop(0)


def end_call():
    return ToolCall(id="t-end", name="end_session", args={"reason": "done"})


def shell_call(command, id_="s1"):
    return ToolCall(id=id_, name="shell", args={"command": command})


class Machine:
    """A container that keeps a process table, enough to watch persistence."""

    def __init__(self, *, keeper=False, lens_state="LIVE", keeper_exit=0, keeper_profile="lab"):
        self.keeper = keeper
        self.lens_state = lens_state
        self.keeper_exit = keeper_exit
        self.keeper_profile = keeper_profile
        self.next_pid = 500

    def __call__(self, container, call):
        _, command = call.wrapped
        cmd = call.cmd
        if command is None:
            text = " ".join(cmd)
            if "command -v keeper" in text:
                return (
                    ExecReply(stdout=b"/usr/local/bin/keeper\n", exit_code=0)
                    if self.keeper
                    else ExecReply(exit_code=1)
                )
            if "kami-lens health" in text:
                return ExecReply(
                    stdout=f"{self.lens_state}\n".encode(),
                    exit_code=0 if self.lens_state == "LIVE" else 1,
                )
            if text.endswith("keeper status"):
                if not self.keeper:
                    return ExecReply(stderr=b"keeper: not found\n", exit_code=127)
                body = (
                    '{"id": 1, "ok": true, "data": {"grants": []}, '
                    f'"meta": {{"profile": "{self.keeper_profile}", "keeperVersion": "1.0.0"}}}}'
                ).encode()
                return ExecReply(stdout=body, exit_code=self.keeper_exit)
            return ExecReply(exit_code=0)
        if command.startswith("nohup sleep"):
            self.next_pid += 1
            container.processes.append([str(self.next_pid), "sleep", "sleep 600"])
            return ExecReply()
        if command == "pgrep sleep":
            pids = [p[0] for p in container.processes if p[1] == "sleep" and p[2] == "sleep 600"]
            return ExecReply(
                stdout="".join(f"{p}\n" for p in pids).encode(), exit_code=0 if pids else 1
            )
        return ExecReply()


def materialize(run_dir):
    prompts = run_dir / "prompts"
    prompts.mkdir(parents=True, exist_ok=True)
    source = cli._prompts_source()
    for name in cli.PROMPT_NAMES:
        (prompts / name).write_text((source / name).read_text(encoding="utf-8"), encoding="utf-8")
    (run_dir / "workspace").mkdir(exist_ok=True)


@pytest.fixture
def run_dir(tmp_path):
    materialize(tmp_path)
    return tmp_path


def config_for(run_dir, profile="shell", **overrides):
    return RunConfig(
        run_dir=Path(run_dir),
        run_id="run-080",
        model="test-model",
        prices=PRICES,
        caps=LoopCaps(session_token_cap=100_000),
        params=SamplingParams(max_tokens=4096),
        scaffold_profile=profile,
        **overrides,
    )


def env_factory(docker, run_dir, *, keeper=False):
    return lambda: Environment(
        DockerClient(docker.socket_path),
        "kami-env",
        run_dir,
        keeper_rung=keeper,
        keeper_container="kami-keeper",
    )


def events(run_dir, kind=None):
    path = Path(run_dir) / "telemetry.jsonl"
    found = list(read_events(path)) if path.exists() else []
    return [e for e in found if kind is None or e["event"] == kind]


# --- init (D.2, P12) ------------------------------------------------------------------


def manifest(docker, profile="shell", **extra):
    base = {
        "run_id": "r",
        "provider": "anthropic",
        "model": "m",
        "price_table": {"input_usd_per_mtok": 1.0, "output_usd_per_mtok": 5.0},
        "caps": {"session_token_cap": 1000},
        "scaffold_profile": profile,
        "environment": {
            "container": "kami-env",
            "keeper_container": "kami-keeper",
            "docker_socket": docker.socket_path,
        },
        "pins": {"keeper_sha": KEEPER_PIN},
    }
    base.update(extra)
    return base


def test_init_passes_a_shell_environment_that_matches_its_rung(run_dir):
    with FakeDocker(FakeContainer("kami-env", on_exec=Machine())) as docker:
        lines = cli.check_environment(manifest(docker), run_dir)
    assert lines[0] == "environment ok (kami-env, image kami-agent-env:0.8.0-shell)"
    assert "environment: no keeper command (rung shell)" in lines
    assert "environment: kami-lens answers from inside" in lines


def test_init_refuses_a_shell_rung_whose_environment_has_a_keeper(run_dir):
    with FakeDocker(FakeContainer("kami-env", on_exec=Machine(keeper=True))) as docker:
        with pytest.raises(SystemExit, match="shell must not have a keeper command"):
            cli.check_environment(manifest(docker), run_dir)


def test_init_refuses_a_keeper_rung_without_a_keeper(run_dir):
    with FakeDocker(FakeContainer("kami-env", on_exec=Machine())) as docker:
        with pytest.raises(SystemExit, match="shell_keeper needs the keeper command"):
            cli.check_environment(manifest(docker, "shell_keeper"), run_dir)


def test_init_passes_a_keeper_rung_whose_keeper_image_names_the_pin(run_dir):
    keeper = FakeContainer("kami-keeper", image="kami-keeper:bbe3929aa11")
    env = FakeContainer(
        "kami-env", on_exec=Machine(keeper=True), image="kami-agent-env:0.8.0-shell_keeper"
    )
    with FakeDocker(env, keeper) as docker:
        lines = cli.check_environment(manifest(docker, "shell_keeper"), run_dir)
    assert "keeper ok (profile lab, version 1.0.0, image kami-keeper:bbe3929aa11)" in lines


def test_init_refuses_a_keeper_image_that_does_not_name_the_pin(run_dir):
    keeper = FakeContainer("kami-keeper", image="kami-keeper:0123456789")
    env = FakeContainer("kami-env", on_exec=Machine(keeper=True))
    with FakeDocker(env, keeper) as docker:
        with pytest.raises(SystemExit, match="does not name pins.keeper_sha 'bbe3929'"):
            cli.check_environment(manifest(docker, "shell_keeper"), run_dir)


def test_init_refuses_a_keeper_that_does_not_answer_from_inside(run_dir):
    keeper = FakeContainer("kami-keeper", image="kami-keeper:bbe3929")
    env = FakeContainer("kami-env", on_exec=Machine(keeper=True, keeper_exit=3))
    with FakeDocker(env, keeper) as docker:
        with pytest.raises(SystemExit, match="keeper status from inside the environment"):
            cli.check_environment(manifest(docker, "shell_keeper"), run_dir)


def test_init_refuses_a_keeper_that_runs_another_profile(run_dir):
    """shell_keeper needs the keeper's `lab` profile: a keeper that answers
    from inside with any other is refused, by name, and nothing else about
    it (its image names the pin, the lens answers) rescues the bring-up."""
    keeper = FakeContainer("kami-keeper", image=f"kami-keeper:{KEEPER_PIN}")
    env = FakeContainer("kami-env", on_exec=Machine(keeper=True, keeper_profile="fleet"))
    with FakeDocker(env, keeper) as docker:
        environment = Environment(
            DockerClient(docker.socket_path),
            "kami-env",
            run_dir,
            keeper_rung=True,
            keeper_container="kami-keeper",
        )
        with pytest.raises(EnvironmentCheckError) as refused:
            environment.bring_up_checks(keeper_sha=KEEPER_PIN)
        with pytest.raises(SystemExit, match="'fleet'"):
            cli.check_environment(manifest(docker, "shell_keeper"), run_dir)
    assert "'fleet'" in str(refused.value)
    assert "'lab'" in str(refused.value)


def test_init_refuses_an_environment_that_is_not_running_or_has_no_lens(run_dir):
    with FakeDocker(FakeContainer("kami-env", running=False)) as docker:
        with pytest.raises(SystemExit, match="is not running"):
            cli.check_environment(manifest(docker), run_dir)
    with FakeDocker(FakeContainer("kami-env", on_exec=Machine(lens_state="NO_DAEMON"))) as docker:
        with pytest.raises(SystemExit, match="kami-lens did not answer"):
            cli.check_environment(manifest(docker), run_dir)


def test_init_checks_nothing_below_the_shell_rungs(run_dir):
    with FakeDocker() as docker:
        assert cli.check_environment(manifest(docker, "planning"), run_dir) == []
        assert docker.requests == []


@pytest.mark.parametrize(
    ("image", "pin", "matches"),
    [
        ("kami-keeper:bbe3929", "bbe3929", True),
        ("kami-keeper:bbe39290f00d", "bbe3929", True),
        ("kami-keeper:bbe3929", "bbe39290f00d", True),
        ("registry.example:5000/kami-keeper:bbe3929@sha256:" + "a" * 64, "bbe3929", True),
        ("kami-keeper:latest", "bbe3929", False),
        ("kami-keeper:bbe39", "bbe39", False),
        ("kami-keeper", "bbe3929", False),
    ],
)
def test_the_keeper_image_names_its_pin(image, pin, matches):
    assert image_matches_pin(image, pin) is matches


# --- session boundaries (D.5, P9 shell_state) -------------------------------------------


def test_every_session_records_the_environment_at_both_ends(run_dir):
    container = FakeContainer("kami-env", on_exec=Machine())
    adapter = ScriptedAdapter(response(end_call()))
    with FakeDocker(container) as docker:
        outcome = run_session(
            config_for(run_dir),
            adapter,
            environment_factory=env_factory(docker, run_dir),
            clock=Clock(),
        )
    assert outcome == SESSION_RAN
    kinds = [e["event"] for e in events(run_dir)]
    assert kinds.index("shell_state") == kinds.index("session_start") + 1
    assert kinds[-3:] == ["shell_state", "session_end", "schedule_next"]
    start, end = events(run_dir, "shell_state")
    for row in (start, end):
        validate_event(row)
        assert row["container"] == "kami-env"
        assert row["running"] is True
        assert row["restarted"] is False
        assert row["process_count"] == 2
        assert row["lens_socket_ok"] is True
        assert "keeper_socket_ok" not in row
    assert (start["phase"], end["phase"]) == ("session_start", "session_end")
    session_start = events(run_dir, "session_start")[0]
    assert session_start["environment_image"] == "kami-agent-env:0.8.0-shell"
    assert "keeper_version" not in session_start


def test_a_stopped_environment_is_started_and_recorded_as_restarted(run_dir):
    container = FakeContainer("kami-env", running=False, on_exec=Machine())
    with FakeDocker(container) as docker:
        run_session(
            config_for(run_dir),
            ScriptedAdapter(response(end_call())),
            environment_factory=env_factory(docker, run_dir),
            clock=Clock(),
        )
    assert container.starts == 1
    start = events(run_dir, "shell_state")[0]
    assert start["running"] is True
    assert start["restarted"] is True


def test_a_restart_count_that_moved_between_sessions_is_recorded(run_dir):
    container = FakeContainer("kami-env", restart_count=0, on_exec=Machine())
    clock = Clock()
    with FakeDocker(container) as docker:
        factory = env_factory(docker, run_dir)
        run_session(
            config_for(run_dir),
            ScriptedAdapter(response(end_call())),
            environment_factory=factory,
            clock=clock,
        )
        container.restart_count = 1
        clock.now += timedelta(hours=2)
        run_session(
            config_for(run_dir),
            ScriptedAdapter(response(end_call())),
            environment_factory=factory,
            clock=clock,
        )
    states = events(run_dir, "shell_state")
    assert [s["restarted"] for s in states] == [False, False, True, False]
    assert [s["restart_count"] for s in states] == [0, 0, 1, 1]


@pytest.mark.parametrize(
    ("lens_state", "ok"),
    [("LIVE", True), ("BOOTSTRAP", True), ("NO_DAEMON", False), ("TIMEOUT", False)],
)
def test_the_lens_probe_records_whether_the_daemon_answered(run_dir, lens_state, ok):
    """E.3 in the unit tier: an answer in any state is an answer."""
    container = FakeContainer("kami-env", on_exec=Machine(lens_state=lens_state))
    with FakeDocker(container) as docker:
        snap = env_factory(docker, run_dir)().snapshot(last_restart_count=None)
        probe = [c for c in docker.execs if "kami-lens health" in " ".join(c.cmd)][0]
    assert snap.lens_socket_ok is ok
    assert probe.user == "1000:1000"
    assert probe.cmd[-3:] == ["sh", "-c", "test -S /srv/lens/kami-lens.sock && kami-lens health"]


@pytest.mark.parametrize(("code", "ok"), [(0, True), (1, True), (3, False)])
def test_the_keeper_probe_records_whether_a_frame_came_back(run_dir, code, ok):
    container = FakeContainer("kami-env", on_exec=Machine(keeper=True, keeper_exit=code))
    with FakeDocker(container) as docker:
        snap = Environment(
            DockerClient(docker.socket_path), "kami-env", run_dir, keeper_rung=True
        ).snapshot(last_restart_count=None)
    assert snap.keeper_socket_ok is ok


# --- E.2 (P16) in the unit tier: what the agent leaves running -----------------------------


def test_a_background_process_persists_and_is_gone_after_run_complete(run_dir):
    container = FakeContainer("kami-env", on_exec=Machine())
    clock = Clock()
    with FakeDocker(container) as docker:
        factory = env_factory(docker, run_dir)
        adapter = ScriptedAdapter(
            response(shell_call("nohup sleep 600 >/dev/null 2>&1 &", "s1")),
            response(shell_call("pgrep sleep", "s2")),
            response(end_call()),
        )
        run_session(config_for(run_dir), adapter, environment_factory=factory, clock=clock)
        pgrep = adapter.requests[2]["messages"][-1].content
        clock.now += timedelta(hours=2)
        run_session(
            config_for(run_dir),
            ScriptedAdapter(response(end_call())),
            environment_factory=factory,
            clock=clock,
        )
        clock.now += timedelta(hours=2)
        outcome = run_session(
            config_for(run_dir, budget_usd=0.0),
            ScriptedAdapter(),
            environment_factory=factory,
            clock=clock,
        )
    assert "exit_code: 0" in pgrep and "\n501\n" in pgrep
    states = events(run_dir, "shell_state")
    second_session_start = states[2]
    assert {"pid": 501, "comm": "sleep", "args": "sleep 600"} in second_session_start["processes"]
    assert outcome == RUN_COMPLETED
    final = states[-1]
    assert final["phase"] == "run_complete"
    assert final["running"] is False
    assert final["process_count"] == 0
    assert final["processes"] == []
    assert "lens_socket_ok" not in final
    (complete,) = events(run_dir, "run_complete")
    assert complete["environment_stopped"] is True
    assert container.stops == 1


# --- the end of the run on shell_keeper (E.5, P8) ----------------------------------------


def keeper_answers(cmd, args):
    if cmd == "stop":
        return {"stopped": ["auto-1"], "quiescence": {"quiescent": True}}
    if cmd == "revoke":
        return {"revoked": True, "stopped": [], "quiescence": {"quiescent": True}}
    if cmd == "status":
        return {"grants": []}
    if cmd == "books":
        return {"books": []}
    return {}


def run_to_completion(run_dir, docker, keeper_client_factory):
    return run_session(
        config_for(run_dir, "shell_keeper", budget_usd=0.0),
        ScriptedAdapter(),
        environment_factory=env_factory(docker, run_dir, keeper=True),
        keeper_factory=keeper_client_factory,
        clock=Clock(),
        disable_supervisor=lambda: None,
    )


def test_run_complete_stops_the_books_then_revokes_then_stops_the_environment(run_dir):
    container = FakeContainer("kami-env", on_exec=Machine(keeper=True))
    with FakeDocker(container) as docker, FakeKeeper(keeper_answers) as keeper:
        outcome = run_to_completion(run_dir, docker, lambda: KeeperClient(keeper.socket_path))
        commands = keeper.commands
        requests = keeper.requests
    assert outcome == RUN_COMPLETED
    assert commands[:2] == ["stop", "revoke"]
    assert requests[0] == ("stop", {"all": True, "account": "main"})
    assert requests[1] == ("revoke", {"account": "main"})
    stop_row, revoke_row = events(run_dir, "tool_call")
    for row in (stop_row, revoke_row):
        validate_event(row)
        assert row["source"] == "keeper"
        assert row["initiator"] == "scaffold"
        assert row["ok"] is True
    assert stop_row["tool"] == "keeper_stop_all"
    assert stop_row["keeper_books_stopped"] == 1
    assert revoke_row["tool"] == "keeper_revoke"
    assert revoke_row["keeper_grants_after"] == 0
    (complete,) = events(run_dir, "run_complete")
    assert complete["keeper_grants_after"] == 0
    assert complete["environment_stopped"] is True
    kinds = [e["event"] for e in events(run_dir)]
    assert kinds == ["tool_call", "tool_call", "shell_state", "run_complete"]


def test_a_silent_keeper_is_recorded_and_the_run_still_completes(run_dir):
    container = FakeContainer("kami-env", on_exec=Machine(keeper=True))
    with FakeDocker(container) as docker, FakeKeeper(silent=True) as keeper:
        outcome = run_to_completion(
            run_dir, docker, lambda: KeeperClient(keeper.socket_path, timeout_s=0.3)
        )
    assert outcome == RUN_COMPLETED
    stop_row, revoke_row = events(run_dir, "tool_call")
    assert stop_row["ok"] is False and revoke_row["ok"] is False
    assert "did not answer" in revoke_row["error"]
    assert "keeper_grants_after" not in revoke_row
    (complete,) = events(run_dir, "run_complete")
    assert "keeper_grants_after" not in complete
    assert complete["environment_stopped"] is True
    assert container.stops == 1


def test_a_refused_revoke_is_ok_false_with_the_keepers_words(run_dir):
    def answers(cmd, args):
        if cmd == "revoke":
            return Refuse("INTERNAL", "signer busy")
        return keeper_answers(cmd, args)

    container = FakeContainer("kami-env", on_exec=Machine(keeper=True))
    with FakeDocker(container) as docker, FakeKeeper(answers) as keeper:
        run_to_completion(run_dir, docker, lambda: KeeperClient(keeper.socket_path))
    _, revoke_row = events(run_dir, "tool_call")
    assert revoke_row["ok"] is False
    assert revoke_row["error"] == "INTERNAL: signer busy"


def test_sessions_never_revoke(run_dir):
    container = FakeContainer("kami-env", on_exec=Machine(keeper=True))
    ledger = Ledger({"auto-1": []})
    with FakeDocker(container) as docker, FakeKeeper(ledger) as keeper:
        run_session(
            config_for(run_dir, "shell_keeper"),
            ScriptedAdapter(response(end_call())),
            environment_factory=env_factory(docker, run_dir, keeper=True),
            keeper_factory=lambda: KeeperClient(keeper.socket_path),
            clock=Clock(),
        )
        commands = keeper.commands
    assert "revoke" not in commands and "stop" not in commands
    assert commands[0] == "books"
    session_start = events(run_dir, "session_start")[0]
    assert session_start["keeper_version"] == "1.0.0"
    assert session_start["scaffold_profile"] == "shell_keeper"
    assert events(run_dir, "shell_state")[0]["keeper_socket_ok"] is True


def test_the_final_ledger_read_lands_before_run_complete(run_dir):
    rows = [
        tx_row(
            "auto-1", "0x" + "ab" * 32, stage="receipt", status="success", lens_block=100, block=101
        )
    ]
    ledger = Ledger({"auto-1": rows})

    def answers(cmd, args):
        if cmd in ("books", "ledger"):
            return ledger(cmd, args)
        return keeper_answers(cmd, args)

    container = FakeContainer("kami-env", on_exec=Machine(keeper=True))
    with FakeDocker(container) as docker, FakeKeeper(answers) as keeper:
        run_to_completion(run_dir, docker, lambda: KeeperClient(keeper.socket_path))
    kinds = [e["event"] for e in events(run_dir)]
    assert kinds.index("keeper_tx") < kinds.index("run_complete")
    assert kinds.index("keeper_tx") > kinds.index("tool_call")


def test_below_the_shell_rungs_the_end_of_the_run_is_what_it_was(run_dir):
    run_session(config_for(run_dir, "planning", budget_usd=0.0), ScriptedAdapter(), clock=Clock())
    (complete,) = events(run_dir, "run_complete")
    assert "environment_stopped" not in complete
    assert "keeper_grants_after" not in complete
    assert [e["event"] for e in events(run_dir)] == ["run_complete"]


# --- init's own record (P12) ------------------------------------------------------------


def init_manifest(tmp_path, profile, **extra):
    import yaml

    body = {
        "run_id": "init-080",
        "provider": "anthropic",
        "model": "m",
        "price_table": {"input_usd_per_mtok": 1.0, "output_usd_per_mtok": 5.0},
        "caps": {"session_token_cap": 1000},
        "scaffold_profile": profile,
        "pins": {"harness_sha": "h", "keeper_sha": KEEPER_PIN},
        **extra,
    }
    path = tmp_path / f"{profile}.yaml"
    path.write_text(yaml.safe_dump(body))
    return path


def test_init_refuses_a_shell_rung_that_names_no_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("KAMI_ENV_CONTAINER", raising=False)
    path = init_manifest(tmp_path, "shell")
    with pytest.raises(SystemExit, match="needs environment.container"):
        cli.main(
            [
                "init",
                "--manifest",
                str(path),
                "--run-dir",
                str(tmp_path / "run"),
                "--skip-connectivity",
            ]
        )
    assert not (tmp_path / "run" / "telemetry.jsonl").exists()


def test_init_refuses_an_unknown_rung(tmp_path):
    path = init_manifest(tmp_path, "shell_plus")
    with pytest.raises(SystemExit, match="unknown scaffold_profile 'shell_plus'"):
        cli.main(
            [
                "init",
                "--manifest",
                str(path),
                "--run-dir",
                str(tmp_path / "run"),
                "--skip-connectivity",
            ]
        )


@pytest.mark.parametrize(
    ("profile", "keeper_sha"), [("shell", ""), ("shell_keeper", KEEPER_PIN), ("planning", "")]
)
def test_run_start_records_the_keeper_pin_on_its_rung_only(tmp_path, profile, keeper_sha):
    path = init_manifest(tmp_path, profile, environment={"container": "kami-env"})
    run = tmp_path / "run"
    assert (
        cli.main(["init", "--manifest", str(path), "--run-dir", str(run), "--skip-connectivity"])
        == 0
    )
    (start,) = events(run, "run_start")
    validate_event(start)
    assert start["keeper_sha"] == keeper_sha
    for name in ("shell.txt", "shell_keeper.txt"):
        assert (run / "prompts" / name).is_file()
