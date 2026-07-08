"""State cache + telemetry-fold recovery (SPEC §3.2, §7.1)."""

import json

import pytest

from kami_agent.state import (
    RunState,
    crashed_session,
    fold_telemetry,
    load_state,
    save_state,
    session_totals,
)


def _llm_call(session, cost, tokens_in, tokens_out):
    return {
        "event": "llm_call",
        "session": session,
        "cost_usd": cost,
        "input_tokens": tokens_in,
        "output_tokens": tokens_out,
    }


EVENTS = [
    {"event": "run_start", "session": 0, "ts": "2026-07-08T00:00:00+00:00"},
    {"event": "session_start", "session": 1, "ts": "2026-07-08T01:00:00+00:00"},
    _llm_call(1, 0.01, 1000, 100),
    {"event": "tool_call", "session": 1},
    _llm_call(1, 0.02, 2000, 200),
    {
        "event": "schedule_next",
        "session": 1,
        "next_wake_at": "2026-07-08T02:00:00+00:00",
    },
    {"event": "session_end", "session": 1},
    {"event": "session_start", "session": 2, "ts": "2026-07-08T02:00:00+00:00"},
    _llm_call(2, 0.04, 3000, 300),
    {"event": "tool_call", "session": 2},
    {"event": "tool_call", "session": 2},
]


def test_fold_recomputes_accounting_from_the_stream():
    state = fold_telemetry(EVENTS)
    assert state.session_counter == 2
    assert state.cumulative_usd == pytest.approx(0.07)
    assert state.cumulative_tokens == 6600
    assert state.first_session_at == "2026-07-08T01:00:00+00:00"
    assert state.next_wake_at == "2026-07-08T02:00:00+00:00"
    assert state.run_status == "active"


def test_fold_empty_stream_is_fresh_state():
    assert fold_telemetry([]) == RunState()


def test_fold_marks_run_complete():
    events = [
        *EVENTS,
        {"event": "session_end", "session": 2},
        {"event": "run_complete", "session": 2},
    ]
    assert fold_telemetry(events).run_status == "complete"


def test_crashed_session_detected():
    # Session 2 opened but never closed → crash (SPEC §3 step 2).
    assert crashed_session(EVENTS) == 2


def test_no_crash_when_all_sessions_closed():
    events = [*EVENTS, {"event": "session_end", "session": 2}]
    assert crashed_session(events) is None
    assert crashed_session([]) is None


def test_session_totals_for_synthetic_crash_event():
    totals = session_totals(EVENTS, 2)
    assert totals == {
        "llm_calls": 1,
        "tool_calls": 2,
        "session_cost_usd": pytest.approx(0.04),
        "session_tokens": 3300,
    }
    assert session_totals(EVENTS, 1)["llm_calls"] == 2
    assert session_totals(EVENTS, 99) == {
        "llm_calls": 0,
        "tool_calls": 0,
        "session_cost_usd": 0.0,
        "session_tokens": 0,
    }


def test_save_load_round_trip(tmp_path):
    state = RunState(
        session_counter=5,
        cumulative_usd=1.23,
        cumulative_tokens=456,
        next_wake_at="2026-07-08T03:00:00+00:00",
        run_status="active",
        first_session_at="2026-07-08T01:00:00+00:00",
    )
    path = tmp_path / "run" / "state.json"
    save_state(state, path)
    assert load_state(path) == state
    # Atomic write leaves no temp file behind.
    assert list(path.parent.iterdir()) == [path]


def test_load_missing_file_is_fresh_run(tmp_path):
    assert load_state(tmp_path / "state.json") == RunState()


def test_saved_file_is_plain_json(tmp_path):
    path = tmp_path / "state.json"
    save_state(RunState(session_counter=3), path)
    data = json.loads(path.read_text())
    assert data["session_counter"] == 3
    assert set(data) == {
        "session_counter",
        "cumulative_usd",
        "cumulative_tokens",
        "next_wake_at",
        "run_status",
        "first_session_at",
    }
