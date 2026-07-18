"""Repetition breaker: the four 001 pathology fixtures, trip points, and the negative fixture.

Each pathology observed in the 001 telemetry is encoded as a synthetic
call sequence through the real AgentLoop; the assertions pin which rule
trips and at exactly which executed call. A productive session must
never trip. Tripping is silent (D13): session_end exactly as tool_cap,
no warning to the model.
"""

import json

import pytest

from kami_agent.adapters.base import (
    AdapterResponse,
    SamplingParams,
    StopReason,
    ToolCall,
    ToolDef,
    Usage,
)
from kami_agent.governor import PriceTable
from kami_agent.loop import AgentLoop, GameToolResult, LoopCaps
from kami_agent.repetition import (
    RepetitionTracker,
    is_error_or_revert,
    signature,
)
from kami_agent.telemetry import TelemetryWriter, read_events
from kami_agent.tools.scaffold import ScaffoldTools

PRICES = PriceTable(input_usd_per_mtok=3.0, output_usd_per_mtok=15.0)
PARAMS = SamplingParams(max_tokens=4096)

REVERTED = json.dumps(
    {"tx_hash": "0xfeed", "status": "reverted", "block": 1, "gas_used": 1_499_999}
)
SUCCESS = json.dumps({"tx_hash": "0xfeed", "status": "success", "block": 1, "gas_used": 90_000})


class ScriptedAdapter:
    def __init__(self, *script):
        self.script = list(script)

    def complete(self, system, messages, tools, params):
        return self.script.pop(0)


class ScriptedGame:
    """Harness stand-in whose result content is scripted per tool."""

    def __init__(self, results):
        self._results = results
        self.tool_defs = [
            ToolDef(
                name=name,
                description="d",
                input_schema={"type": "object", "properties": {}, "additionalProperties": True},
            )
            for name in results
        ]

    def execute(self, name, args):
        return GameToolResult(content=self._results[name], tx_hash=None)


def response(*tool_calls):
    return AdapterResponse(
        text_blocks=(),
        tool_calls=tuple(tool_calls),
        stop_reason=StopReason.TOOL_USE,
        usage=Usage(input_tokens=1000, output_tokens=100),
    )


def call(name, args, id_):
    return ToolCall(id=id_, name=name, args=args)


@pytest.fixture
def run_dir(tmp_path):
    (tmp_path / "reference").mkdir()
    (tmp_path / "reference" / "gdd.md").write_text("lore")
    return tmp_path


def run_loop(run_dir, adapter, game, **cap_overrides):
    caps = LoopCaps(
        session_token_cap=cap_overrides.pop("session_token_cap", 1_000_000), **cap_overrides
    )
    scaffold = ScaffoldTools(run_dir, session_number=1)
    telemetry = TelemetryWriter(run_dir / "telemetry.jsonl", run_id="test-run")
    loop = AgentLoop(
        adapter=adapter,
        model="test-model",
        system="s",
        kickoff_text="Session start.",
        continuation_text="Continue. To end this session, call end_session.",
        scaffold=scaffold,
        game=game,
        telemetry=telemetry,
        session=1,
        params=PARAMS,
        prices=PRICES,
        caps=caps,
        sleep=lambda s: None,
    )
    return loop.run()


def tool_events(run_dir):
    return [e for e in read_events(run_dir / "telemetry.jsonl") if e["event"] == "tool_call"]


# --- 001 pathology 1: identical reverted retries (45x in 001) -------------------


def test_identical_reverted_retries_trip_identical_call_at_5(run_dir):
    game = ScriptedGame({"quest_accept": REVERTED})
    intents = [call("quest_accept", {"quest_index": 3}, f"q{i}") for i in range(45)]
    adapter = ScriptedAdapter(*[response(intent) for intent in intents])
    result = run_loop(run_dir, adapter, game)
    assert result.reason == "repetition"
    assert result.repetition.rule == "identical_call"
    # Trip point: the 5th consecutive identical execution, well before
    # tool_cap (50). 001's storms were consecutive (D43).
    assert len(tool_events(run_dir)) == 5
    assert result.repetition.fields["repetition_count"] == 5
    expected_sig = signature("quest_accept", {"quest_index": 3})
    assert result.repetition.fields["repetition_signature"] == expected_sig
    # Reverts came back success-shaped (ok=true), so the §5.4 error cap
    # (5 consecutive errors) never fired — exactly the 001 gap.
    assert all(e["ok"] for e in tool_events(run_dir))


# --- 001 pathology 2: identical successful polls (48-49x get_status in 001) ------


def test_identical_successful_polls_trip_identical_call_at_5(run_dir):
    intents = [call("get_status", {}, f"s{i}") for i in range(48)]
    adapter = ScriptedAdapter(*[response(intent) for intent in intents])
    result = run_loop(run_dir, adapter, game=None)
    assert result.reason == "repetition"
    assert result.repetition.rule == "identical_call"
    assert len(tool_events(run_dir)) == 5
    assert result.repetition.fields["repetition_count"] == 5


# --- 001 pathology 3: rotating poll loop (small read-set cycle) ------------------

CYCLE = [
    ("get_status", {}),
    ("get_active_quests", {}),
    ("get_account_kamis", {}),
    ("set_next_wake", {"minutes_from_now": 30}),
]


def cycle_intents(n):
    return [
        call(name, args, f"c{i}") for i, (name, args) in enumerate(CYCLE * (n // len(CYCLE) + 1))
    ][:n]


def test_rotating_poll_loop_trips_window_diversity_at_defaults(run_dir):
    # Consecutive identical counting (D43) never fires on a rotating cycle
    # (identical streak resets every call); the window rule is the designed
    # catch — full window (30) holding only 4 distinct signatures.
    game = ScriptedGame({"get_active_quests": SUCCESS, "get_account_kamis": SUCCESS})
    adapter = ScriptedAdapter(*[response(intent) for intent in cycle_intents(48)])
    result = run_loop(run_dir, adapter, game)
    assert result.reason == "repetition"
    assert result.repetition.rule == "window_diversity"
    assert len(tool_events(run_dir)) == 30
    assert result.repetition.fields["repetition_window"] == 30
    assert result.repetition.fields["repetition_distinct"] == 4
    assert sorted(result.repetition.fields["repetition_signatures"]) == sorted(
        signature(name, args) for name, args in CYCLE
    )


def test_interleaved_evasion_shape_trips_window_diversity(run_dir):
    # A,A,A,A,B repeating: each identical streak stops at 4 (under the
    # consecutive cap), but the 30-call window holds only 2 distinct
    # signatures — a rotating read-set by another name (audit D43 note).
    intents = []
    for block in range(8):
        for j in range(4):
            intents.append(call("get_status", {}, f"a{block}-{j}"))
        intents.append(call("workspace_list", {}, f"b{block}"))
    adapter = ScriptedAdapter(*[response(intent) for intent in intents])
    result = run_loop(run_dir, adapter, game=None)
    assert result.reason == "repetition"
    assert result.repetition.rule == "window_diversity"
    assert len(tool_events(run_dir)) == 30
    assert result.repetition.fields["repetition_distinct"] == 2


# --- 001 pathology 4: parameter sweep, consecutive reverts (44-47x in 001) -------


def test_parameter_sweep_of_reverts_trips_same_tool_errors_at_8(run_dir):
    game = ScriptedGame({"quest_accept": REVERTED})
    intents = [call("quest_accept", {"quest_index": i}, f"q{i}") for i in range(44)]
    adapter = ScriptedAdapter(*[response(intent) for intent in intents])
    result = run_loop(run_dir, adapter, game)
    assert result.reason == "repetition"
    assert result.repetition.rule == "same_tool_errors"
    # Distinct args defeat the identical-call rule; the same-tool
    # error/revert run trips at 8, well before tool_cap (50).
    assert len(tool_events(run_dir)) == 8
    assert result.repetition.fields["repetition_tool"] == "quest_accept"
    assert result.repetition.fields["repetition_count"] == 8
    # Success-shaped reverts (ok=true) never advanced the §5.4 error cap.
    assert all(e["ok"] for e in tool_events(run_dir))


def test_sweep_of_loop_level_errors_still_ends_via_error_cap_first(run_dir):
    # When failures surface as loop-level errors (ok=false), the §5.4
    # consecutive-error cap (5) fires before the same-tool rule (8) —
    # unchanged semantics; the breaker adds coverage, it takes none away.
    intents = [call("no_such_tool", {"i": i}, f"x{i}") for i in range(10)]
    adapter = ScriptedAdapter(*[response(intent) for intent in intents])
    result = run_loop(run_dir, adapter, game=None)
    assert result.reason == "errors"
    assert len(tool_events(run_dir)) == 5


# --- negative fixture: a productive session never trips ---------------------------


def test_productive_session_with_occasional_retries_does_not_trip(run_dir):
    # 34 executed calls: varied tools, distinct args, two identical failed
    # retries of the same read (then success), get_status polled 4 times —
    # no identical run reaches 5 consecutive, every full 30-window holds
    # well over 4 distinct signatures, and no same-tool error run
    # approaches 8.
    game = ScriptedGame({"get_kami": SUCCESS})
    intents = []
    intents.append(call("get_status", {}, "s0"))
    for i in range(8):
        intents.append(call("get_kami", {"kami_id": i}, f"k{i}"))
    # Two identical failed retries (missing file), then the write that fixes it.
    intents.append(call("workspace_read", {"path": "notes.md"}, "r0"))
    intents.append(call("workspace_read", {"path": "notes.md"}, "r1"))
    intents.append(call("workspace_write", {"path": "notes.md", "content": "plan"}, "w0"))
    intents.append(call("workspace_read", {"path": "notes.md"}, "r2"))
    intents.append(call("get_status", {}, "s1"))
    for i in range(8):
        intents.append(call("workspace_write", {"path": f"log/{i}.md", "content": str(i)}, f"l{i}"))
    intents.append(call("get_status", {}, "s2"))
    for i in range(8):
        intents.append(call("workspace_read", {"path": f"log/{i}.md"}, f"rl{i}"))
    intents.append(call("get_status", {}, "s3"))
    intents.append(call("set_next_wake", {"minutes_from_now": 45}, "wk"))
    intents.append(call("end_session", {"reason": "done"}, "end"))
    assert len(intents) == 34
    adapter = ScriptedAdapter(*[response(intent) for intent in intents])
    result = run_loop(run_dir, adapter, game)
    assert result.reason == "agent"
    assert result.repetition is None
    assert len(tool_events(run_dir)) == 34


# --- silence + telemetry shape ---------------------------------------------------


def test_trip_is_silent_and_ends_like_tool_cap(run_dir):
    intents = [call("get_status", {}, f"s{i}") for i in range(6)]
    adapter = ScriptedAdapter(*[response(intent) for intent in intents])
    result = run_loop(run_dir, adapter, game=None)
    assert result.reason == "repetition"
    # Silent (D13): no continuation, no warning message; the transcript ends
    # on the tripping call's tool result.
    assert result.messages[-1].role == "tool_result"
    # Exactly 5 llm_calls were made (one per executed call) — no final model
    # call after the trip.
    assert result.llm_calls == 5


# --- tracker unit coverage --------------------------------------------------------


def test_signature_normalizes_key_order():
    assert signature("t", {"a": 1, "b": 2}) == signature("t", {"b": 2, "a": 1})
    assert signature("t", {"a": 1}) != signature("t", {"a": 2})
    assert signature("t", {}) != signature("u", {})


def test_is_error_or_revert_classification():
    assert is_error_or_revert(False, "anything")
    assert is_error_or_revert(True, REVERTED)
    assert is_error_or_revert(True, json.dumps({"error": "failed to read account state"}))
    assert is_error_or_revert(True, json.dumps({"result": {"status": "reverted"}}))
    assert not is_error_or_revert(True, SUCCESS)
    assert not is_error_or_revert(True, json.dumps({"error": None}))
    assert not is_error_or_revert(True, "plain text result")
    assert not is_error_or_revert(True, json.dumps({"results": [{"error": "row-level"}]}))


def test_identical_call_streak_is_consecutive_not_cumulative():
    # D43: 4 identical, a different call, 4 more identical — 8 total in the
    # session, never 5 in a row — must NOT trip; 5 in a row must.
    tracker = RepetitionTracker(window=1000)
    for _ in range(2):
        for _ in range(4):
            assert tracker.record("get_status", {}, error_or_revert=False) is None
        assert tracker.record("workspace_list", {}, error_or_revert=False) is None
    for _ in range(4):
        assert tracker.record("get_status", {}, error_or_revert=False) is None
    trip = tracker.record("get_status", {}, error_or_revert=False)
    assert trip is not None and trip.rule == "identical_call"
    assert trip.fields["repetition_count"] == 5


def test_same_tool_error_streak_resets_on_success_and_on_tool_change():
    tracker = RepetitionTracker(identical_cap=1000, window=1000, same_tool_error_cap=3)
    assert tracker.record("a", {"i": 1}, error_or_revert=True) is None
    assert tracker.record("a", {"i": 2}, error_or_revert=True) is None
    assert tracker.record("a", {"i": 3}, error_or_revert=False) is None  # success resets
    assert tracker.record("a", {"i": 4}, error_or_revert=True) is None
    assert tracker.record("b", {"i": 5}, error_or_revert=True) is None  # tool change resets
    assert tracker.record("b", {"i": 6}, error_or_revert=True) is None
    trip = tracker.record("b", {"i": 7}, error_or_revert=True)
    assert trip is not None and trip.rule == "same_tool_errors"
    assert trip.fields == {"repetition_tool": "b", "repetition_count": 3}


def test_window_not_evaluated_until_full():
    tracker = RepetitionTracker(identical_cap=1000, window=6, min_distinct=4)
    for i in range(5):
        assert tracker.record(f"t{i % 3}", {}, error_or_revert=False) is None
    trip = tracker.record("t2", {}, error_or_revert=False)  # 6th call fills the window
    assert trip is not None and trip.rule == "window_diversity"
    assert trip.fields["repetition_distinct"] == 3
