"""The `shell` tool: one command in the run's environment container (SPEC P10, D8; 0.8.0).

Against a stand-in Docker Engine API (``fake_docker.py``): the scaffold's
own HTTP client, its exec request, the multiplexed stream, the result
text, the log, the clamp, and how a call counts in the loop. What a real
container does with the request is the smoke tier's
(``tests/smoke/test_environment_docker.py``).
"""

import re
from pathlib import Path

import pytest
import yaml
from fake_docker import ExecReply, FakeContainer, FakeDocker, scripted

from kami_agent import environment as env_module
from kami_agent.adapters.base import AdapterResponse, SamplingParams, StopReason, ToolCall, Usage
from kami_agent.docker_api import DockerClient
from kami_agent.environment import Environment
from kami_agent.governor import PriceTable
from kami_agent.loop import AgentLoop, LoopCaps
from kami_agent.telemetry import TelemetryWriter, read_events, validate_event
from kami_agent.tools.errors import ToolError
from kami_agent.tools.scaffold import ScaffoldTools

REPO = Path(__file__).parents[2]
PRICES = PriceTable(input_usd_per_mtok=3.0, output_usd_per_mtok=15.0)
PARAMS = SamplingParams(max_tokens=4096)


def response(*tool_calls):
    return AdapterResponse(
        text_blocks=(),
        tool_calls=tuple(tool_calls),
        stop_reason=StopReason.TOOL_USE if tool_calls else StopReason.END_TURN,
        usage=Usage(input_tokens=10, output_tokens=5),
    )


class ScriptedAdapter:
    def __init__(self, *responses):
        self._responses = list(responses)
        self.requests = []

    def complete(self, system, messages, tools, params):
        self.requests.append({"system": system, "messages": list(messages), "tools": tools})
        return self._responses.pop(0)


def shell_call(command, id_="s1", **extra):
    return ToolCall(id=id_, name="shell", args={"command": command, **extra})


def end_call():
    return ToolCall(id="t-end", name="end_session", args={"reason": "done"})


@pytest.fixture
def run_dir(tmp_path):
    (tmp_path / "workspace").mkdir()
    return tmp_path


def scaffold_for(run_dir, docker, *, profile="shell", **kwargs):
    environment = Environment(DockerClient(docker.socket_path), "kami-env", run_dir)
    return ScaffoldTools(
        run_dir,
        session_number=1,
        profile=profile,
        environment=environment,
        **kwargs,
    )


def make_loop(run_dir, docker, adapter, **cap_overrides):
    caps = LoopCaps(session_token_cap=100_000, **cap_overrides)
    return AgentLoop(
        adapter=adapter,
        model="test-model",
        system="s",
        kickoff_text="Session start.",
        continuation_text="Continue.",
        scaffold=scaffold_for(
            run_dir,
            docker,
            shell_timeout_s=caps.shell_timeout_s,
            tool_result_max_bytes=caps.tool_result_max_bytes,
        ),
        game=None,
        telemetry=TelemetryWriter(run_dir / "telemetry.jsonl", run_id="r"),
        session=1,
        params=PARAMS,
        prices=PRICES,
        caps=caps,
        sleep=lambda s: None,
    )


def tool_rows(run_dir, name="shell"):
    return [
        e
        for e in read_events(run_dir / "telemetry.jsonl")
        if e["event"] == "tool_call" and e["tool"] == name
    ]


# --- the request (D8): who runs what, where ------------------------------------------


def test_one_call_is_a_docker_exec_as_the_environment_user_in_the_workspace(run_dir):
    container = FakeContainer("kami-env", on_exec=scripted({"ls": ExecReply(stdout=b"a\n")}))
    with FakeDocker(container) as docker:
        scaffold_for(run_dir, docker).execute("shell", {"command": "ls -la"})
        (call,) = docker.execs
    assert call.cmd == ["timeout", "-k", "5", "120", "bash", "-c", "ls -la"]
    assert call.user == "1000:1000"
    assert call.workdir == "/workspace"
    assert call.env == ["HOME=/workspace"]
    assert call.body["Privileged"] is False
    assert call.body["AttachStdin"] is False
    assert call.body["Tty"] is False


@pytest.mark.parametrize(
    ("requested", "expected"),
    [(None, "120"), (30, "30"), (0.2, "1"), (-5, "1"), (999, "120"), (2.5, "2.5")],
)
def test_the_timeout_is_clamped_into_the_allowed_range(run_dir, requested, expected):
    container = FakeContainer("kami-env", on_exec=scripted({"true": ExecReply()}))
    args = {"command": "true"}
    if requested is not None:
        args["timeout_s"] = requested
    with FakeDocker(container) as docker:
        scaffold_for(run_dir, docker, shell_timeout_s=120).execute("shell", args)
        assert docker.execs[0].cmd[3] == expected


def test_a_non_finite_timeout_is_an_error(run_dir):
    with FakeDocker(FakeContainer("kami-env")) as docker:
        with pytest.raises(ToolError, match="timeout_s must be a finite number"):
            scaffold_for(run_dir, docker).execute(
                "shell", {"command": "true", "timeout_s": float("nan")}
            )
        assert docker.execs == []


# --- the result (A.4) and the log ------------------------------------------------------


def test_the_result_text_is_the_frozen_shape(run_dir):
    container = FakeContainer(
        "kami-env",
        on_exec=scripted({"x": ExecReply(stdout=b"out\n", stderr=b"err\n", exit_code=2)}),
    )
    with FakeDocker(container) as docker:
        text = scaffold_for(run_dir, docker).execute("shell", {"command": "x"})
    assert re.fullmatch(
        r"exit_code: 2\ntimed_out: false\nduration_s: \d+\.\d\n"
        r"--- stdout \(4 bytes\) ---\nout\n\n--- stderr \(4 bytes\) ---\nerr\n",
        text,
    ), text


def test_each_stream_is_capped_separately_with_the_standard_marker(run_dir):
    big = b"o" * 50_000
    container = FakeContainer(
        "kami-env", on_exec=scripted({"x": ExecReply(stdout=big, stderr=b"e" * 40_000)})
    )
    with FakeDocker(container) as docker:
        scaffold = scaffold_for(run_dir, docker, tool_result_max_bytes=65536)
        text = scaffold.execute("shell", {"command": "x"})
    half = (65536 - env_module.SHELL_RESULT_RESERVE_BYTES) // 2
    assert f"[truncated: showing the first {half} bytes of 50000.]" in text
    assert f"[truncated: showing the first {half} bytes of 40000.]" in text
    assert "--- stderr (40000 bytes) ---" in text
    # The two halves and the header never meet the loop's own cap.
    assert len(text.encode("utf-8")) <= 65536


def test_the_whole_output_lands_in_the_run_dir_log(run_dir):
    big = b"0123456789" * 10_000
    container = FakeContainer("kami-env", on_exec=scripted({"x": ExecReply(stdout=big)}))
    with FakeDocker(container) as docker:
        scaffold = scaffold_for(run_dir, docker, tool_result_max_bytes=4096)
        scaffold.shell_call_seq = 7
        scaffold.execute("shell", {"command": "x"})
    log = run_dir / "shell" / "001-0007.log"
    data = log.read_bytes()
    assert big in data
    assert b"--- command (timeout_s 120) ---\nx\n" in data
    assert b"--- stdout (100000 bytes) ---" in data
    assert scaffold.last_shell.log_rel == "shell/001-0007.log"
    # Nothing half-written is left beside it.
    assert sorted(p.name for p in (run_dir / "shell").iterdir()) == ["001-0007.log"]


def test_the_log_is_bounded_and_says_where_it_cut(run_dir, monkeypatch):
    monkeypatch.setattr(env_module, "SHELL_LOG_MAX_BYTES", 1000)
    container = FakeContainer("kami-env", on_exec=scripted({"x": ExecReply(stdout=b"y" * 5000)}))
    with FakeDocker(container) as docker:
        scaffold = scaffold_for(run_dir, docker)
        scaffold.execute("shell", {"command": "x"})
    data = (run_dir / "shell" / "001-0000.log").read_bytes()
    assert b"--- stdout (5000 bytes, the first 1000 kept) ---" in data
    body = data.split(b"kept) ---\n", 1)[1].split(b"\n--- stderr", 1)[0]
    assert body == b"y" * 1000
    assert scaffold.last_shell.stdout_bytes == 5000


def test_timed_out_needs_the_wrapper_code_at_or_past_the_timeout(run_dir):
    container = FakeContainer(
        "kami-env",
        on_exec=scripted(
            {
                "own124": ExecReply(exit_code=124),
                "slow": ExecReply(exit_code=124, delay_s=1.05),
                "killed": ExecReply(exit_code=137, delay_s=1.05),
            }
        ),
    )
    with FakeDocker(container) as docker:
        scaffold = scaffold_for(run_dir, docker)
        scaffold.execute("shell", {"command": "own124", "timeout_s": 1})
        assert scaffold.last_shell.timed_out is False
        text = scaffold.execute("shell", {"command": "slow", "timeout_s": 1})
        assert scaffold.last_shell.timed_out is True
        assert "timed_out: true" in text
        scaffold.execute("shell", {"command": "killed", "timeout_s": 1})
        assert scaffold.last_shell.timed_out is True


# --- in the loop (P2, P5, P5.1) --------------------------------------------------------


def test_a_non_zero_exit_is_a_result_not_an_error(run_dir):
    container = FakeContainer("kami-env", on_exec=scripted({"false": ExecReply(exit_code=1)}))
    # Distinct commands, so the repetition breaker stays out of it.
    calls = [shell_call(f"false {i}", id_=f"s{i}") for i in range(6)]
    adapter = ScriptedAdapter(*(response(c) for c in calls), response(end_call()))
    with FakeDocker(container) as docker:
        result = make_loop(run_dir, docker, adapter, max_consecutive_errors=3).run()
    rows = tool_rows(run_dir)
    assert result.reason == "agent"
    assert len(rows) == 6
    assert all(r["ok"] is True and r["exit_code"] == 1 for r in rows)


def test_a_docker_failure_is_a_tool_error_with_the_daemons_words(run_dir):
    container = FakeContainer("kami-env", running=False)
    adapter = ScriptedAdapter(response(shell_call("ls")), response(end_call()))
    with FakeDocker(container) as docker:
        make_loop(run_dir, docker, adapter).run()
    (row,) = tool_rows(run_dir)
    assert row["ok"] is False
    assert row["error"] == (
        "the command could not be run in the environment: Container kami-env is not running"
    )
    for field in ("exit_code", "timed_out", "stdout_bytes", "stderr_bytes", "shell_log"):
        assert field not in row
    result_message = adapter.requests[1]["messages"][-1]
    assert result_message.is_error is True
    assert result_message.content == row["error"]


def test_the_shell_tool_is_exempt_from_the_tool_watchdog(run_dir):
    container = FakeContainer("kami-env", on_exec=scripted({"slow": ExecReply(delay_s=0.6)}))
    adapter = ScriptedAdapter(response(shell_call("slow")), response(end_call()))
    with FakeDocker(container) as docker:
        make_loop(run_dir, docker, adapter, tool_timeout_s=0.2).run()
    (row,) = tool_rows(run_dir)
    assert row["ok"] is True
    assert row["exit_code"] == 0


def test_shell_calls_count_against_the_session_tool_cap(run_dir):
    container = FakeContainer("kami-env", on_exec=scripted({"echo": ExecReply()}))
    calls = [shell_call(f"echo {i}", id_=f"s{i}") for i in range(5)]
    adapter = ScriptedAdapter(*(response(c) for c in calls))
    with FakeDocker(container) as docker:
        result = make_loop(run_dir, docker, adapter, session_tool_cap=3).run()
    assert result.reason == "tool_cap"
    assert len(tool_rows(run_dir)) == 3


def test_the_repetition_signature_is_the_command_text(run_dir):
    """The same command with another timeout is the same command (P5.1)."""
    container = FakeContainer("kami-env", on_exec=scripted({"pgrep": ExecReply(exit_code=1)}))
    calls = [shell_call("pgrep sleep", id_=f"s{i}", timeout_s=10 + i) for i in range(5)]
    adapter = ScriptedAdapter(*(response(c) for c in calls))
    with FakeDocker(container) as docker:
        result = make_loop(run_dir, docker, adapter).run()
    assert result.reason == "repetition"
    assert result.repetition.rule == "identical_call"


def test_shell_rows_carry_their_record_and_validate(run_dir):
    container = FakeContainer(
        "kami-env", on_exec=scripted({"x": ExecReply(stdout=b"abc", stderr=b"de", exit_code=0)})
    )
    adapter = ScriptedAdapter(response(shell_call("x")), response(end_call()))
    with FakeDocker(container) as docker:
        make_loop(run_dir, docker, adapter).run()
    (row,) = tool_rows(run_dir)
    validate_event(row)
    assert row["source"] == "scaffold"
    assert row["initiator"] == "model"
    assert row["exit_code"] == 0
    assert row["timed_out"] is False
    assert row["stdout_bytes"] == 3
    assert row["stderr_bytes"] == 2
    assert row["shell_log"] == f"shell/001-{row['call_seq']:04d}.log"
    assert (run_dir / row["shell_log"]).is_file()


def test_the_docker_failure_wording_carries_no_apparatus(run_dir):
    wording = "the command could not be run in the environment: "
    for word in ("budget", "spend", "usd", "cost", "token", "horizon", "cap ", "study"):
        assert word not in wording


# --- E.1 (P15) in the unit tier: the construction --------------------------------------
#
# What is NOT mounted into the environment does not exist there. The unit
# tier proves the definition says so and that the scaffold asks for nothing
# more; the smoke tier probes a real container.


def _env_service(*files):
    services: dict = {}
    for name in files:
        doc = yaml.safe_load((REPO / "deploy" / "environment" / name).read_text())
        for service, spec in doc["services"].items():
            merged = services.setdefault(service, {})
            for key, value in spec.items():
                if key == "volumes":
                    merged.setdefault("volumes", []).extend(value)
                else:
                    merged[key] = value
    return services["env"]


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        (
            ("compose.yaml",),
            {
                "/srv/run/workspace:/workspace",
                "/srv/run/reference:/reference:ro",
                "/srv/run/journal:/journal:ro",
                "/srv/sockets/lens:/srv/lens",
            },
        ),
        (
            ("compose.yaml", "compose.keeper.yaml"),
            {
                "/srv/run/workspace:/workspace",
                "/srv/run/reference:/reference:ro",
                "/srv/run/journal:/journal:ro",
                "/srv/sockets/lens:/srv/lens",
                "/srv/sockets/keeper:/srv/keeper",
            },
        ),
    ],
    ids=["shell", "shell_keeper"],
)
def test_the_environment_mounts_nothing_of_the_run_the_keys_or_docker(files, expected):
    env = _env_service(*files)
    assert set(env["volumes"]) == expected
    sources = [v.split(":")[0] for v in env["volumes"]]
    assert "/srv/run" not in sources
    assert not any(s.startswith("/srv/keys") for s in sources)
    assert not any("docker.sock" in s for s in sources)
    # The daemons' DATA directories are never mounted: only the relay's sockets.
    assert "/srv/lens" not in sources and "/srv/keeper" not in sources
    assert env["network_mode"] == "none"
    assert env["user"] == "1000:1000"
    assert env["init"] is True
    assert env["read_only"] is True
    for key in ("privileged", "cap_add", "devices", "pid", "ipc"):
        assert key not in env


def _service(file, name):
    doc = yaml.safe_load((REPO / "deploy" / "environment" / file).read_text())
    return doc["services"][name]


def test_the_lens_healthcheck_waits_out_a_cold_boot():
    """`kami-lens health` answers 0 only when LIVE, and a first start loads the
    whole world for more than ten minutes: without a start period the relay,
    the environment and the keeper (all `service_healthy` on the lens) would
    fail on a slow cold boot. The keeper starts in seconds and needs none."""
    lens = _service("compose.yaml", "lens")["healthcheck"]
    assert lens["test"] == ["CMD", "kami-lens", "health"]
    assert lens["start_period"] == "30m"
    keeper = _service("compose.keeper.yaml", "keeper")["healthcheck"]
    assert "start_period" not in keeper
    for file, name in (("compose.yaml", "sockets"), ("compose.keeper.yaml", "keeper")):
        assert _service(file, name)["depends_on"]["lens"]["condition"] == "service_healthy"


def test_one_filesystem_seen_two_ways(run_dir):
    """A file the environment's shell writes to /workspace is the workspace
    tools' file: the definition mounts run_dir/workspace there."""
    with FakeDocker(FakeContainer("kami-env")) as docker:
        scaffold = scaffold_for(run_dir, docker)
        (run_dir / "workspace" / "made_by_shell.txt").write_text("hello")
        assert scaffold.execute("workspace_read", {"path": "made_by_shell.txt"}) == "hello"
        assert "workspace/made_by_shell.txt 5" in scaffold.execute("workspace_list", {})


def test_the_scaffold_hands_what_it_writes_to_the_environments_user(run_dir, monkeypatch):
    owned = []
    monkeypatch.setattr("os.chown", lambda path, uid, gid: owned.append((str(path), uid, gid)))
    tools = ScaffoldTools(run_dir, profile="shell", workspace_owner=(1000, 1000))
    tools.execute("workspace_write", {"path": "notes/a/plan.md", "content": "x"})
    ws = str((run_dir / "workspace").resolve())
    assert owned == [
        (f"{ws}/notes", 1000, 1000),
        (f"{ws}/notes/a", 1000, 1000),
        (f"{ws}/notes/a/plan.md", 1000, 1000),
    ]
    owned.clear()
    ScaffoldTools(run_dir, profile="planning").execute(
        "workspace_write", {"path": "b.md", "content": "y"}
    )
    assert owned == []
