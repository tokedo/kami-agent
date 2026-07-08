"""Session runner: full SPEC §3 lifecycle incl. crash/resume and boundary stops."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kami_agent.adapters.base import (
    AdapterResponse,
    SamplingParams,
    StopReason,
    ToolCall,
    Usage,
)
from kami_agent.governor import PriceTable
from kami_agent.harness import HarnessError
from kami_agent.loop import LoopCaps
from kami_agent.runner import (
    ALREADY_COMPLETE,
    LOCK_HELD,
    NOT_DUE,
    RUN_COMPLETED,
    SESSION_ABORTED,
    SESSION_RAN,
    RunConfig,
    run_session,
)
from kami_agent.state import load_state
from kami_agent.supervisor import LOCK_FILENAME
from kami_agent.telemetry import TelemetryWriter, read_events

PRICES = PriceTable(input_usd_per_mtok=3.0, output_usd_per_mtok=15.0)
T0 = datetime(2026, 7, 8, 12, 0, 0, tzinfo=UTC)

SYSTEM_TXT = "You are an agent in a world.\n"
KICKOFF_TXT = "Session start.\n"
CONTINUE_TXT = "Continue. To end this session, call end_session.\n"


class Clock:
    def __init__(self, start=T0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


class ScriptedAdapter:
    def __init__(self, *script):
        self.script = list(script)
        self.requests = []

    def complete(self, system, messages, tools, params):
        self.requests.append({"system": system, "messages": list(messages)})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def response(*tool_calls, tokens=(1000, 100)):
    return AdapterResponse(
        text_blocks=(),
        tool_calls=tuple(tool_calls),
        stop_reason=StopReason.TOOL_USE if tool_calls else StopReason.END_TURN,
        usage=Usage(input_tokens=tokens[0], output_tokens=tokens[1]),
    )


def end_call():
    return ToolCall(id="t-end", name="end_session", args={"reason": "done"})


def wake_call(minutes):
    return ToolCall(id="t-wake", name="set_next_wake", args={"minutes_from_now": minutes})


@pytest.fixture
def run_dir(tmp_path):
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "system.txt").write_text(SYSTEM_TXT)
    (prompts / "kickoff.txt").write_text(KICKOFF_TXT)
    (prompts / "continue.txt").write_text(CONTINUE_TXT)
    reference = tmp_path / "reference"
    reference.mkdir()
    (reference / "gdd.md").write_text("lore")
    return tmp_path


def config_for(run_dir, **overrides):
    return RunConfig(
        run_dir=Path(run_dir),
        run_id="run-001",
        model="test-model",
        prices=PRICES,
        caps=LoopCaps(session_token_cap=100_000),
        params=SamplingParams(max_tokens=4096),
        **overrides,
    )


def events_of(run_dir, kind=None):
    path = Path(run_dir) / "telemetry.jsonl"
    events = list(read_events(path)) if path.exists() else []
    return [e for e in events if kind is None or e["event"] == kind]


# --- full session lifecycle -----------------------------------------------------


def test_full_session_lifecycle(run_dir):
    clock = Clock()
    adapter = ScriptedAdapter(response(wake_call(90), end_call()))
    outcome = run_session(config_for(run_dir), adapter, clock=clock)
    assert outcome == SESSION_RAN

    kinds = [e["event"] for e in events_of(run_dir)]
    assert kinds == [
        "session_start",
        "llm_call",
        "tool_call",
        "tool_call",
        "session_end",
        "schedule_next",
    ]

    start = events_of(run_dir, "session_start")[0]
    assert start["session"] == 1
    assert start["trigger"] == "scheduled"
    assert start["budget_remaining_usd"] == pytest.approx(10.0)
    assert start["wallclock_elapsed_s"] == 0.0
    assert start["tools_hash"].startswith("sha256:")

    end = events_of(run_dir, "session_end")[0]
    assert end["reason"] == "agent"
    assert end["llm_calls"] == 1
    assert end["tool_calls"] == 2

    schedule = events_of(run_dir, "schedule_next")[0]
    assert schedule["source"] == "agent"
    assert schedule["requested_min"] == 90
    assert schedule["clamped_min"] == 90.0
    assert schedule["next_wake_at"] == (T0 + timedelta(minutes=90)).isoformat()

    state = load_state(run_dir / "state.json")
    assert state.session_counter == 1
    assert state.cumulative_usd > 0
    assert state.first_session_at == start["ts"]
    assert state.next_wake_at == schedule["next_wake_at"]
    assert not (run_dir / LOCK_FILENAME).exists()

    transcript = (run_dir / "transcripts" / "session-0001.jsonl").read_text().splitlines()
    roles = [json.loads(line)["role"] for line in transcript]
    assert roles == ["user", "assistant", "tool_result", "tool_result"]
    assert json.loads(transcript[0])["text"] == "Session start."


def test_system_context_is_prompt_plus_file_index(run_dir):
    adapter = ScriptedAdapter(response(end_call()))
    run_session(config_for(run_dir), adapter, clock=Clock())
    system = adapter.requests[0]["system"]
    assert system.startswith("You are an agent in a world.")
    assert "workspace/ (empty)" in system
    assert "reference/ 1 files, 4 bytes, read-only" in system


def test_wake_gating_and_manual_bypass(run_dir):
    clock = Clock()
    run_session(
        config_for(run_dir), ScriptedAdapter(response(wake_call(90), end_call())), clock=clock
    )
    clock.advance(minutes=10)
    assert run_session(config_for(run_dir), ScriptedAdapter(), clock=clock) == NOT_DUE
    # Manual trigger bypasses the gate (§8 trigger: manual).
    adapter = ScriptedAdapter(response(end_call()))
    assert run_session(config_for(run_dir), adapter, trigger="manual", clock=clock) == SESSION_RAN
    starts = events_of(run_dir, "session_start")
    assert [s["session"] for s in starts] == [1, 2]
    assert starts[1]["trigger"] == "manual"
    assert starts[1]["wallclock_elapsed_s"] == pytest.approx(600.0)
    # Once due, scheduled runs proceed.
    clock.advance(hours=2)
    assert (
        run_session(config_for(run_dir), ScriptedAdapter(response(end_call())), clock=clock)
        == SESSION_RAN
    )


def test_default_schedule_when_agent_never_calls_set_next_wake(run_dir):
    clock = Clock()
    run_session(config_for(run_dir), ScriptedAdapter(response(end_call())), clock=clock)
    schedule = events_of(run_dir, "schedule_next")[0]
    assert schedule["source"] == "default"
    assert "requested_min" not in schedule
    assert schedule["clamped_min"] == 60.0
    assert schedule["next_wake_at"] == (T0 + timedelta(minutes=60)).isoformat()


# --- boundary checks (D13) --------------------------------------------------------


def seed_spent_run(run_dir, *, cost=11.0):
    """Pre-seed telemetry with a completed session that spent `cost` USD."""
    with TelemetryWriter(run_dir / "telemetry.jsonl", run_id="run-001", clock=Clock()) as w:
        w.emit(
            "session_start",
            session=1,
            trigger="scheduled",
            budget_remaining_usd=10.0,
            wallclock_elapsed_s=0,
            tools_hash="sha256:seed",
        )
        w.emit(
            "llm_call",
            session=1,
            model="test-model",
            input_tokens=1000,
            output_tokens=100,
            cost_usd=cost,
            cumulative_usd=cost,
            cumulative_tokens=1100,
            latency_ms=5.0,
            stop_reason="end_turn",
            retry_count=0,
        )
        w.emit(
            "session_end",
            session=1,
            reason="agent",
            llm_calls=1,
            tool_calls=0,
            session_cost_usd=cost,
            session_tokens=1100,
        )


def test_budget_boundary_completes_run(run_dir):
    seed_spent_run(run_dir, cost=11.0)
    disabled = []
    outcome = run_session(
        config_for(run_dir),
        ScriptedAdapter(),
        clock=Clock(),
        disable_supervisor=lambda: disabled.append(True),
    )
    assert outcome == RUN_COMPLETED
    complete = events_of(run_dir, "run_complete")[0]
    assert complete["reason"] == "budget"
    assert complete["totals"]["sessions"] == 1
    assert complete["totals"]["cumulative_usd"] == pytest.approx(11.0)
    assert complete["totals"]["overspend_usd"] == pytest.approx(1.0)
    assert disabled == [True]
    assert load_state(run_dir / "state.json").run_status == "complete"
    # Subsequent invocations exit immediately.
    assert run_session(config_for(run_dir), ScriptedAdapter(), clock=Clock()) == ALREADY_COMPLETE


def test_t_max_boundary(run_dir):
    clock = Clock()
    run_session(config_for(run_dir), ScriptedAdapter(response(end_call())), clock=clock)
    clock.advance(days=31)
    outcome = run_session(config_for(run_dir), ScriptedAdapter(), clock=clock)
    assert outcome == RUN_COMPLETED
    assert events_of(run_dir, "run_complete")[0]["reason"] == "t_max"


# --- crash/resume (§3.2, brief step 7 definition of done) ---------------------------


def crash_telemetry(run_dir):
    """A session that died mid-flight: session_start + spend, no session_end."""
    with TelemetryWriter(run_dir / "telemetry.jsonl", run_id="run-001", clock=Clock()) as w:
        w.emit(
            "session_start",
            session=1,
            trigger="scheduled",
            budget_remaining_usd=10.0,
            wallclock_elapsed_s=0,
            tools_hash="sha256:seed",
        )
        w.emit(
            "llm_call",
            session=1,
            model="test-model",
            input_tokens=4000,
            output_tokens=400,
            cost_usd=0.018,
            cumulative_usd=0.018,
            cumulative_tokens=4400,
            latency_ms=5.0,
            stop_reason="tool_use",
            retry_count=0,
        )
        w.emit(
            "tool_call",
            session=1,
            tool="get_state",
            source="harness",
            duration_ms=10.0,
            ok=True,
        )


def test_crash_recovery_writes_synthetic_end_and_refolds_accounting(run_dir):
    crash_telemetry(run_dir)
    clock = Clock(T0 + timedelta(hours=2))
    adapter = ScriptedAdapter(response(end_call(), tokens=(2000, 200)))
    outcome = run_session(config_for(run_dir), adapter, clock=clock)
    assert outcome == SESSION_RAN

    events = events_of(run_dir)
    # The synthetic crash end lands before any new-session event.
    synthetic = events[3]
    assert synthetic["event"] == "session_end"
    assert synthetic["session"] == 1
    assert synthetic["reason"] == "crash"
    assert synthetic["llm_calls"] == 1
    assert synthetic["tool_calls"] == 1
    assert synthetic["session_cost_usd"] == pytest.approx(0.018)
    assert synthetic["session_tokens"] == 4400

    # The crashed session's number is never reused.
    assert [e["session"] for e in events_of(run_dir, "session_start")] == [1, 2]

    # Recomputed accounting equals an independent manual fold of the stream.
    state = load_state(run_dir / "state.json")
    manual_usd = sum(e["cost_usd"] for e in events_of(run_dir, "llm_call"))
    manual_tokens = sum(
        e["input_tokens"] + e["output_tokens"] for e in events_of(run_dir, "llm_call")
    )
    assert state.cumulative_usd == pytest.approx(manual_usd)
    assert state.cumulative_tokens == manual_tokens
    assert state.session_counter == 2


def test_crash_recovery_is_idempotent(run_dir):
    crash_telemetry(run_dir)
    clock = Clock(T0 + timedelta(hours=2))
    run_session(config_for(run_dir), ScriptedAdapter(response(end_call())), clock=clock)
    clock.advance(hours=2)
    run_session(config_for(run_dir), ScriptedAdapter(response(end_call())), clock=clock)
    crash_ends = [e for e in events_of(run_dir, "session_end") if e["reason"] == "crash"]
    assert len(crash_ends) == 1


# --- harness handshake failure (§2) ---------------------------------------------------


def failing_harness():
    raise HarnessError("handshake failed: child exited")


def test_harness_failure_aborts_with_zero_model_calls(run_dir):
    adapter = ScriptedAdapter()  # would raise if ever called
    outcome = run_session(
        config_for(run_dir), adapter, harness_factory=failing_harness, clock=Clock()
    )
    assert outcome == SESSION_ABORTED
    kinds = [e["event"] for e in events_of(run_dir)]
    assert kinds == ["session_start", "session_end", "schedule_next"]
    end = events_of(run_dir, "session_end")[0]
    assert end["reason"] == "errors"
    assert end["llm_calls"] == 0
    schedule = events_of(run_dir, "schedule_next")[0]
    assert schedule["source"] == "default"
    assert schedule["clamped_min"] == 60.0
    # The aborted session still claims its number.
    assert load_state(run_dir / "state.json").session_counter == 1


# --- locking ---------------------------------------------------------------------------


def test_lock_held_exits_without_touching_anything(run_dir):
    lock = run_dir / LOCK_FILENAME
    lock.write_text(json.dumps({"pid": 1, "created": T0.isoformat()}))
    outcome = run_session(config_for(run_dir), ScriptedAdapter(), clock=Clock())
    assert outcome == LOCK_HELD
    assert not (run_dir / "telemetry.jsonl").exists()
    assert lock.exists()  # foreign lock is left in place


def test_stale_lock_is_broken_and_run_proceeds(run_dir):
    lock = run_dir / LOCK_FILENAME
    stale = (T0 - timedelta(hours=3)).isoformat()
    lock.write_text(json.dumps({"pid": 1, "created": stale}))
    adapter = ScriptedAdapter(response(end_call()))
    assert run_session(config_for(run_dir), adapter, clock=Clock()) == SESSION_RAN
    assert not lock.exists()
