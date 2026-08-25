"""The bounded in-session wait primitive (SPEC P5.1, P10, P2).

Why this tool exists, stated once so the tests below read as what they
are: the world has cooldowns of a couple of minutes, and the scaffold
offered no way to pass wall time inside a session. Agents were left with
filler calls and polling — and polling the same value is five identical
signatures in a row, so the repetition breaker fired on the ONLY waiting
strategy available. The tool removes the cause; the breaker's silence on
forced endings (P5) is untouched.
"""

import pytest

from kami_agent.adapters.base import AdapterResponse, SamplingParams, StopReason, ToolCall, Usage
from kami_agent.governor import PriceTable
from kami_agent.loop import WAIT_TOOL, AgentLoop, LoopCaps
from kami_agent.telemetry import TelemetryWriter, read_events, validate_event
from kami_agent.tools.errors import ToolError
from kami_agent.tools.scaffold import (
    DEFAULT_WAIT_MAX_SECONDS,
    SCAFFOLD_TOOL_DEFS,
    ScaffoldTools,
)

PRICES = PriceTable(input_usd_per_mtok=3.0, output_usd_per_mtok=15.0)
PARAMS = SamplingParams(max_tokens=4096)


@pytest.fixture
def run_dir(tmp_path):
    (tmp_path / "workspace").mkdir()
    return tmp_path


class Sleeper:
    def __init__(self):
        self.slept = []

    def __call__(self, seconds):
        self.slept.append(seconds)


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

    def complete(self, system, messages, tools, params):
        return self._responses.pop(0)


def wait_call(seconds, id_="w1"):
    return ToolCall(id=id_, name=WAIT_TOOL, args={"seconds": seconds})


def end_call():
    return ToolCall(id="t-end", name="end_session", args={"reason": "done"})


def make_loop(run_dir, adapter, *, sleep=None, **cap_overrides):
    caps = LoopCaps(session_token_cap=100_000, **cap_overrides)
    return AgentLoop(
        adapter=adapter,
        model="test-model",
        system="s",
        kickoff_text="Session start.",
        continuation_text="Continue.",
        scaffold=ScaffoldTools(
            run_dir,
            session_number=1,
            wait_max_seconds=caps.wait_max_seconds,
            sleep=sleep or (lambda s: None),
        ),
        game=None,
        telemetry=TelemetryWriter(run_dir / "telemetry.jsonl", run_id="r"),
        session=1,
        params=PARAMS,
        prices=PRICES,
        caps=caps,
        sleep=lambda s: None,
    )


def wait_rows(run_dir):
    return [
        e
        for e in read_events(run_dir / "telemetry.jsonl")
        if e["event"] == "tool_call" and e["tool"] == WAIT_TOOL
    ]


# --- the tool itself ----------------------------------------------------------


def test_it_is_on_the_base_surface_of_every_profile():
    """Treatment symmetry: an ablation ladder cannot vary its time primitive."""
    from kami_agent.tools.scaffold import PROFILES, scaffold_tool_names

    for profile in PROFILES:
        assert WAIT_TOOL in scaffold_tool_names(profile)


def test_the_description_is_mechanism_only():
    """What it does and that values are clamped — never when to use it (I3)."""
    definition = next(t for t in SCAFFOLD_TOOL_DEFS if t.name == WAIT_TOOL)
    text = definition.description.lower()
    for advice in ("should", "useful", "when the", "cooldown", "strategy", "prefer"):
        assert advice not in text
    for apparatus in ("budget", "cost", "token", "cap ", "capped", "limit"):
        assert apparatus not in text


def test_it_sleeps_for_what_was_asked_and_says_so(run_dir):
    sleeper = Sleeper()
    tools = ScaffoldTools(run_dir, wait_max_seconds=300.0, sleep=sleeper)
    assert tools.execute(WAIT_TOOL, {"seconds": 90}) == "Waited 90 seconds."
    assert sleeper.slept == [90.0]


def test_a_request_over_the_bound_is_clamped_and_the_clamp_is_visible(run_dir):
    """The set_next_wake precedent: clamp, then name the value actually used."""
    sleeper = Sleeper()
    tools = ScaffoldTools(run_dir, wait_max_seconds=300.0, sleep=sleeper)
    assert tools.execute(WAIT_TOOL, {"seconds": 4000}) == "Waited 300 seconds."
    assert sleeper.slept == [300.0]


def test_a_negative_request_clamps_to_zero(run_dir):
    """ "Do not wait" is a coherent thing to have asked for."""
    sleeper = Sleeper()
    tools = ScaffoldTools(run_dir, wait_max_seconds=300.0, sleep=sleeper)
    assert tools.execute(WAIT_TOOL, {"seconds": -5}) == "Waited 0 seconds."
    assert sleeper.slept == [0.0]


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), "soon"])
def test_non_finite_and_non_numeric_requests_are_errors(run_dir, bad):
    tools = ScaffoldTools(run_dir, sleep=lambda s: None)
    with pytest.raises(ToolError):
        tools.execute(WAIT_TOOL, {"seconds": bad})


def test_the_default_bound_covers_the_worlds_cooldowns():
    """Sized as a cooldown primitive: the observed range tops out near 185 s."""
    assert DEFAULT_WAIT_MAX_SECONDS >= 185
    # And stays far below any plausible session length — it is not a
    # substitute for set_next_wake, which remains the between-session
    # mechanism (P6).
    assert DEFAULT_WAIT_MAX_SECONDS <= 15 * 60


# --- telemetry (P9) -----------------------------------------------------------


def test_both_durations_are_recorded_so_waiting_is_analyzable(run_dir):
    adapter = ScriptedAdapter(response(wait_call(30)), response(end_call()))
    make_loop(run_dir, adapter).run()
    row = wait_rows(run_dir)[0]
    validate_event(row)
    assert row["wait_requested_s"] == 30.0
    assert row["wait_actual_s"] >= 0.0
    assert row["ok"] is True


def test_a_clamp_is_visible_only_in_the_pair(run_dir):
    adapter = ScriptedAdapter(response(wait_call(4000)), response(end_call()))
    make_loop(run_dir, adapter, wait_max_seconds=10.0).run()
    row = wait_rows(run_dir)[0]
    assert row["wait_requested_s"] == 4000.0
    assert row["wait_actual_s"] < 4000.0


def test_a_wait_that_never_reached_the_handler_records_no_durations(run_dir):
    """Clear-then-read-back: a bad call cannot inherit the previous wait's numbers."""
    adapter = ScriptedAdapter(
        response(wait_call(5, id_="ok"), ToolCall(id="bad", name=WAIT_TOOL, args={})),
        response(end_call()),
    )
    make_loop(run_dir, adapter).run()
    good, bad = wait_rows(run_dir)
    assert good["wait_requested_s"] == 5.0
    assert bad["ok"] is False
    assert "wait_requested_s" not in bad


# --- the two consequences the tool has on existing machinery ------------------


def test_repeated_waits_never_trip_the_repetition_breaker(run_dir):
    """The exclusion, and the reason for it, in one test.

    Sitting out a three-minute cooldown in clamped chunks is N identical
    signatures in a row. Counting them would end the session for using the
    tool exactly as specified — the failure this tool was added to remove.
    """
    waits = [wait_call(60, id_=f"w{i}") for i in range(8)]
    adapter = ScriptedAdapter(response(*waits), response(end_call()))
    result = make_loop(run_dir, adapter, repetition_identical_cap=3).run()
    assert result.reason == "agent"
    assert len(wait_rows(run_dir)) == 8


def test_a_repeated_non_wait_call_still_trips_it(run_dir):
    """The exclusion is the wait tool and nothing else."""
    polls = [ToolCall(id=f"p{i}", name="get_status", args={}) for i in range(8)]
    adapter = ScriptedAdapter(response(*polls), response(end_call()))
    result = make_loop(run_dir, adapter, repetition_identical_cap=3).run()
    assert result.reason == "repetition"


def test_waits_still_consume_the_session_tool_cap(run_dir):
    """Excluded from the breaker, bounded by the cap: a session stays finite."""
    waits = [wait_call(60, id_=f"w{i}") for i in range(5)]
    adapter = ScriptedAdapter(response(*waits))
    result = make_loop(run_dir, adapter, session_tool_cap=2).run()
    assert result.reason == "tool_cap"


def test_the_watchdog_gives_wait_its_own_bound(run_dir):
    """Otherwise a wait longer than tool_timeout_s times out while working.

    At the defaults that is 300 s against 120 s — the tool would have been
    killed by the timeout meant to catch a hung harness call, counted as a
    consecutive error, and left a sleeping thread behind.
    """
    loop = make_loop(run_dir, ScriptedAdapter(response(end_call())))
    assert loop._watchdog_s(WAIT_TOOL) > loop._caps.tool_timeout_s
    assert loop._watchdog_s(WAIT_TOOL) >= loop._caps.wait_max_seconds
    assert loop._watchdog_s("get_status") == loop._caps.tool_timeout_s


def test_a_wait_longer_than_the_tool_timeout_still_completes(run_dir):
    """End to end, against a real watchdog thread and a real (short) sleep."""
    import time

    adapter = ScriptedAdapter(response(wait_call(0.05)), response(end_call()))
    result = make_loop(
        run_dir,
        adapter,
        sleep=time.sleep,
        tool_timeout_s=0.001,
        wait_max_seconds=5.0,
    ).run()
    assert result.reason == "agent"
    row = wait_rows(run_dir)[0]
    assert row["ok"] is True
    assert row["wait_actual_s"] >= 0.05
