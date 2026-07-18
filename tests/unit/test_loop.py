"""Agent loop: serialization (D18), error semantics (§5.4), retries (§5.5), guard (D17)."""

import pytest

from kami_agent.adapters.base import (
    AdapterError,
    AdapterResponse,
    AssistantMessage,
    SamplingParams,
    StopReason,
    ToolCall,
    ToolDef,
    ToolResultMessage,
    Usage,
    UserMessage,
)
from kami_agent.governor import PriceTable
from kami_agent.loop import AgentLoop, GameToolResult, LoopCaps, SessionResult
from kami_agent.telemetry import TelemetryWriter, read_events
from kami_agent.tools.scaffold import ScaffoldTools

PRICES = PriceTable(input_usd_per_mtok=3.0, output_usd_per_mtok=15.0)
PARAMS = SamplingParams(max_tokens=4096)
KICKOFF = "Session start."
CONTINUE = "Continue. To end this session, call end_session."


def response(*tool_calls, text=None, stop=None, tokens=(1000, 100)):
    return AdapterResponse(
        text_blocks=(text,) if text else (),
        tool_calls=tuple(tool_calls),
        stop_reason=stop or (StopReason.TOOL_USE if tool_calls else StopReason.END_TURN),
        usage=Usage(input_tokens=tokens[0], output_tokens=tokens[1]),
    )


def call(name, args=None, id_="t1"):
    return ToolCall(id=id_, name=name, args=args or {})


def end_call(id_="t-end"):
    return ToolCall(id=id_, name="end_session", args={"reason": "done"})


class ScriptedAdapter:
    def __init__(self, *script):
        self.script = list(script)
        self.requests = []

    def complete(self, system, messages, tools, params):
        self.requests.append({"system": system, "messages": list(messages), "tools": tools})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeGame:
    def __init__(self):
        self.calls = []
        self.tool_defs = [
            ToolDef(
                name="get_state",
                description="d",
                input_schema={"type": "object", "properties": {}},
            )
        ]

    def execute(self, name, args):
        self.calls.append((name, args))
        return GameToolResult(content='{"world": "state"}', tx_hash="0xabc")


class SlowGame(FakeGame):
    def execute(self, name, args):
        import time

        time.sleep(0.5)
        return GameToolResult(content="too late")


@pytest.fixture
def run_dir(tmp_path):
    (tmp_path / "reference").mkdir()
    (tmp_path / "reference" / "gdd.md").write_text("lore " * 100)
    return tmp_path


def make_loop(run_dir, adapter, *, game=None, session=1, sleeps=None, **cap_overrides):
    caps = LoopCaps(
        session_token_cap=cap_overrides.pop("session_token_cap", 100_000), **cap_overrides
    )
    scaffold = ScaffoldTools(run_dir, session_number=session)
    telemetry = TelemetryWriter(run_dir / "telemetry.jsonl", run_id="test-run")
    loop = AgentLoop(
        adapter=adapter,
        model="test-model",
        system="system prompt",
        kickoff_text=KICKOFF,
        continuation_text=CONTINUE,
        scaffold=scaffold,
        game=game,
        telemetry=telemetry,
        session=session,
        params=PARAMS,
        prices=PRICES,
        caps=caps,
        sleep=(sleeps.append if sleeps is not None else (lambda s: None)),
    )
    return loop, scaffold, telemetry


def events_of(run_dir, kind=None):
    events = list(read_events(run_dir / "telemetry.jsonl"))
    return [e for e in events if kind is None or e["event"] == kind]


# --- happy path / agent-ended sessions ---------------------------------------


def test_kickoff_and_agent_end(run_dir):
    adapter = ScriptedAdapter(response(end_call(), text="Nothing to do."))
    loop, scaffold, _ = make_loop(run_dir, adapter)
    result = loop.run()
    assert isinstance(result, SessionResult)
    assert result.reason == "agent"
    assert result.llm_calls == 1
    assert result.tool_calls == 1
    assert scaffold.end_reason == "done"
    first = adapter.requests[0]
    assert first["messages"] == [UserMessage(text=KICKOFF)]
    assert first["system"] == "system prompt"
    # Game tools first, scaffold tools second, deterministic order.
    assert [t.name for t in first["tools"]][-7:] == [
        "workspace_write",
        "workspace_read",
        "workspace_list",
        "workspace_delete",
        "set_next_wake",
        "get_status",
        "end_session",
    ]


def test_tool_roundtrip_and_transcript(run_dir):
    adapter = ScriptedAdapter(
        response(call("get_status", id_="s1"), text="Checking."),
        response(end_call()),
    )
    loop, _, _ = make_loop(run_dir, adapter)
    result = loop.run()
    assert result.reason == "agent"
    # Second request carries assistant turn + tool result.
    second = adapter.requests[1]["messages"]
    assert second[0] == UserMessage(text=KICKOFF)
    assert isinstance(second[1], AssistantMessage)
    assert second[1].text == "Checking."
    assert isinstance(second[2], ToolResultMessage)
    assert second[2].tool_call_id == "s1"
    assert not second[2].is_error
    assert '"session_number"' in second[2].content
    assert result.messages == adapter.requests[1]["messages"] + [
        AssistantMessage(text=None, tool_calls=(end_call(),)),
        ToolResultMessage(tool_call_id="t-end", content="Session ended."),
    ]


def test_game_tool_routing_and_tx_hash(run_dir):
    game = FakeGame()
    adapter = ScriptedAdapter(
        response(call("get_state", id_="g1")),
        response(end_call()),
    )
    loop, _, _ = make_loop(run_dir, adapter, game=game)
    result = loop.run()
    assert result.reason == "agent"
    assert game.calls == [("get_state", {})]
    game_event = events_of(run_dir, "tool_call")[0]
    assert game_event["tool"] == "get_state"
    assert game_event["source"] == "harness"
    assert game_event["tx_hash"] == "0xabc"


# --- D18: strict serialization + end_session batch semantics ------------------


def test_batch_executes_in_order_and_skips_after_end_session(run_dir):
    adapter = ScriptedAdapter(
        response(
            call("workspace_write", {"path": "workspace/a.md", "content": "x"}, id_="w1"),
            end_call(id_="e2"),
            call("workspace_read", {"path": "workspace/a.md"}, id_="r3"),
            call("get_status", id_="s4"),
        )
    )
    loop, _, _ = make_loop(run_dir, adapter)
    result = loop.run()
    assert result.reason == "agent"
    tool_events = events_of(run_dir, "tool_call")
    assert [e["tool"] for e in tool_events] == [
        "workspace_write",
        "end_session",
        "workspace_read",
        "get_status",
    ]
    assert [e.get("skipped", False) for e in tool_events] == [False, False, True, True]
    assert (run_dir / "workspace" / "a.md").exists()  # earlier intent did run
    assert result.tool_calls == 4  # skipped intents are logged tool_call events


def test_later_intents_see_earlier_effects(run_dir):
    adapter = ScriptedAdapter(
        response(
            call("workspace_write", {"path": "workspace/n.md", "content": "seen"}, id_="w1"),
            call("workspace_read", {"path": "workspace/n.md"}, id_="r2"),
        ),
        response(end_call()),
    )
    loop, _, _ = make_loop(run_dir, adapter)
    loop.run()
    results = [m for m in adapter.requests[1]["messages"] if isinstance(m, ToolResultMessage)]
    assert results[1].content == "seen"


# --- §5.4 error semantics -----------------------------------------------------


def test_malformed_calls_return_error_results(run_dir):
    adapter = ScriptedAdapter(
        response(
            call("no_such_tool", id_="x1"),
            call("workspace_read", {"path": 5}, id_="x2"),
        ),
        response(end_call()),
    )
    loop, _, _ = make_loop(run_dir, adapter)
    result = loop.run()
    assert result.reason == "agent"
    results = [m for m in adapter.requests[1]["messages"] if isinstance(m, ToolResultMessage)]
    assert results[0].is_error and "unknown tool" in results[0].content
    assert results[1].is_error and "invalid arguments" in results[1].content
    tool_events = events_of(run_dir, "tool_call")
    assert [e["ok"] for e in tool_events[:2]] == [False, False]
    assert "unknown tool: no_such_tool" == tool_events[0]["error"]


def test_failed_execution_returns_error_and_counts(run_dir):
    adapter = ScriptedAdapter(
        response(call("workspace_read", {"path": "workspace/ghost.md"}, id_="x1")),
        response(end_call()),
    )
    loop, _, _ = make_loop(run_dir, adapter)
    result = loop.run()
    assert result.reason == "agent"
    results = [m for m in adapter.requests[1]["messages"] if isinstance(m, ToolResultMessage)]
    assert results[0].is_error
    assert "no such file" in results[0].content


def test_continuation_on_tool_less_turn(run_dir):
    adapter = ScriptedAdapter(
        response(text="Let me think about my plans...", stop=StopReason.END_TURN),
        response(end_call()),
    )
    loop, _, _ = make_loop(run_dir, adapter)
    result = loop.run()
    assert result.reason == "agent"
    # The frozen continuation string was appended as a user message.
    followup = adapter.requests[1]["messages"]
    assert followup[-1] == UserMessage(text=CONTINUE)
    # The llm_call after a continuation send carries continuation: true.
    llm_events = events_of(run_dir, "llm_call")
    assert "continuation" not in llm_events[0]
    assert llm_events[1]["continuation"] is True


def test_max_tokens_without_complete_call_gets_continuation(run_dir):
    adapter = ScriptedAdapter(
        response(text="truncated mid-", stop=StopReason.MAX_TOKENS),
        response(end_call()),
    )
    loop, _, _ = make_loop(run_dir, adapter)
    assert loop.run().reason == "agent"
    assert adapter.requests[1]["messages"][-1] == UserMessage(text=CONTINUE)


def test_consecutive_errors_end_session(run_dir):
    adapter = ScriptedAdapter(*[response(text="monologue") for _ in range(5)])
    loop, _, _ = make_loop(run_dir, adapter)
    result = loop.run()
    assert result.reason == "errors"
    assert result.llm_calls == 5
    assert adapter.script == []  # exactly max_consecutive_errors calls


def test_error_counter_resets_on_successful_tool_call(run_dir):
    adapter = ScriptedAdapter(
        response(text="hmm"),  # error 1
        response(call("get_status", id_="s1")),  # success → reset
        response(text="hmm"),  # error 1
        response(text="hmm"),  # error 2 → cap
    )
    loop, _, _ = make_loop(run_dir, adapter, max_consecutive_errors=2)
    result = loop.run()
    assert result.reason == "errors"
    assert result.llm_calls == 4


def test_failed_tool_calls_count_toward_error_cap(run_dir):
    adapter = ScriptedAdapter(
        response(
            call("no_such_tool", id_="x1"),
            call("no_such_tool", id_="x2"),
        )
    )
    loop, _, _ = make_loop(run_dir, adapter, max_consecutive_errors=2)
    assert loop.run().reason == "errors"


def test_tool_timeout_is_an_error_result(run_dir):
    adapter = ScriptedAdapter(
        response(call("get_state", id_="g1")),
        response(end_call()),
    )
    loop, _, _ = make_loop(run_dir, adapter, game=SlowGame(), tool_timeout_s=0.05)
    result = loop.run()
    assert result.reason == "agent"
    results = [m for m in adapter.requests[1]["messages"] if isinstance(m, ToolResultMessage)]
    assert results[0].is_error
    assert "timed out after 0.05 seconds" in results[0].content


# --- D17 context guard ---------------------------------------------------------


def test_context_guard_trips_post_call_and_is_silent(run_dir):
    adapter = ScriptedAdapter(
        response(call("get_status", id_="s1"), tokens=(59_000, 2_000)),
    )
    loop, _, _ = make_loop(run_dir, adapter, session_token_cap=60_000)
    result = loop.run()
    assert result.reason == "token_cap"
    # SIGKILL semantics: the tripping response's intents never execute.
    assert events_of(run_dir, "tool_call") == []
    assert result.llm_calls == 1


def test_context_guard_boundary_is_gte(run_dir):
    adapter = ScriptedAdapter(
        response(call("get_status", id_="s1"), tokens=(50_000, 10_000)),
    )
    loop, _, _ = make_loop(run_dir, adapter, session_token_cap=60_000)
    assert loop.run().reason == "token_cap"


# --- tool cap -------------------------------------------------------------------


def test_tool_cap_ends_session(run_dir):
    adapter = ScriptedAdapter(
        response(
            call("get_status", id_="s1"),
            call("get_status", id_="s2"),
            call("get_status", id_="s3"),
        )
    )
    loop, _, _ = make_loop(run_dir, adapter, session_tool_cap=2)
    result = loop.run()
    assert result.reason == "tool_cap"
    assert len(events_of(run_dir, "tool_call")) == 2


def test_end_session_at_cap_is_still_agent(run_dir):
    adapter = ScriptedAdapter(response(call("get_status", id_="s1"), end_call(id_="e2")))
    loop, _, _ = make_loop(run_dir, adapter, session_tool_cap=2)
    assert loop.run().reason == "agent"


# --- §5.5 retries ---------------------------------------------------------------


def test_retryable_errors_backoff_and_recover(run_dir):
    sleeps = []
    adapter = ScriptedAdapter(
        AdapterError("429", retryable=True, status_code=429),
        AdapterError("529", retryable=True, status_code=529),
        response(end_call()),
    )
    loop, _, _ = make_loop(run_dir, adapter, sleeps=sleeps)
    result = loop.run()
    assert result.reason == "agent"
    assert sleeps == [1.0, 2.0]
    llm_events = events_of(run_dir, "llm_call")
    assert [e["retry_count"] for e in llm_events] == [0, 1, 2]
    assert [e.get("usage_unknown", False) for e in llm_events] == [True, True, False]
    assert [e["stop_reason"] for e in llm_events] == ["error", "error", "tool_use"]
    assert [e["cost_usd"] for e in llm_events][:2] == [0.0, 0.0]
    assert result.llm_calls == 3


def test_retries_exhausted_end_session(run_dir):
    sleeps = []
    adapter = ScriptedAdapter(*[AdapterError("529", retryable=True) for _ in range(3)])
    loop, _, _ = make_loop(run_dir, adapter, retry_max_attempts=2, sleeps=sleeps)
    result = loop.run()
    assert result.reason == "errors"
    assert result.llm_calls == 3  # initial + 2 retries, all logged
    assert sleeps == [1.0, 2.0]


def test_non_retryable_error_ends_immediately(run_dir):
    sleeps = []
    adapter = ScriptedAdapter(AdapterError("401", retryable=False, status_code=401))
    loop, _, _ = make_loop(run_dir, adapter, sleeps=sleeps)
    assert loop.run().reason == "errors"
    assert sleeps == []


# --- D19 result cap --------------------------------------------------------------


def test_big_read_truncated_with_reread_hint(run_dir):
    (run_dir / "workspace").mkdir(exist_ok=True)
    (run_dir / "workspace" / "big.md").write_text("z" * 500)
    adapter = ScriptedAdapter(
        response(call("workspace_read", {"path": "workspace/big.md"}, id_="r1")),
        response(end_call()),
    )
    loop, _, _ = make_loop(run_dir, adapter, tool_result_max_bytes=100)
    loop.run()
    results = [m for m in adapter.requests[1]["messages"] if isinstance(m, ToolResultMessage)]
    assert results[0].content.startswith("z" * 100)
    assert "showing the first 100 bytes of 500" in results[0].content
    assert "workspace_read(path='workspace/big.md', offset, length)" in results[0].content
    event = events_of(run_dir, "tool_call")[0]
    assert event["truncated"] is True
    assert event["original_bytes"] == 500
    assert event["path"] == "workspace/big.md"


# --- accounting -------------------------------------------------------------------


def test_cost_and_cumulative_accounting(run_dir):
    adapter = ScriptedAdapter(
        response(call("get_status", id_="s1"), tokens=(1000, 100)),
        response(end_call(), tokens=(2000, 200)),
    )
    loop, _, _ = make_loop(run_dir, adapter)
    result = loop.run()
    per_call = [1000 * 3.0 / 1e6 + 100 * 15.0 / 1e6, 2000 * 3.0 / 1e6 + 200 * 15.0 / 1e6]
    assert result.session_cost_usd == pytest.approx(sum(per_call))
    assert result.session_tokens == 3300
    assert result.cumulative_usd == pytest.approx(sum(per_call))
    llm_events = events_of(run_dir, "llm_call")
    assert llm_events[0]["cost_usd"] == pytest.approx(per_call[0])
    assert llm_events[1]["cumulative_usd"] == pytest.approx(sum(per_call))
    assert llm_events[1]["cumulative_tokens"] == 3300


def test_cache_decomposition_reaches_telemetry_and_cost(run_dir):
    # SPEC §5.2/§8: llm_call preserves the cache decomposition and cost_usd
    # prices the components; cumulative_tokens stays input+output (total).
    cached = AdapterResponse(
        text_blocks=(),
        tool_calls=(end_call(),),
        stop_reason=StopReason.TOOL_USE,
        usage=Usage(
            input_tokens=10_000, output_tokens=100, cache_read_tokens=8_000, cache_write_tokens=500
        ),
    )
    adapter = ScriptedAdapter(cached)
    prices = PriceTable(
        input_usd_per_mtok=3.0,
        output_usd_per_mtok=15.0,
        cache_read_usd_per_mtok=0.30,
        cache_write_usd_per_mtok=3.75,
    )
    caps = LoopCaps(session_token_cap=100_000)
    scaffold = ScaffoldTools(run_dir, session_number=1)
    telemetry = TelemetryWriter(run_dir / "telemetry.jsonl", run_id="test-run")
    loop = AgentLoop(
        adapter=adapter,
        model="test-model",
        system="s",
        kickoff_text=KICKOFF,
        continuation_text=CONTINUE,
        scaffold=scaffold,
        game=None,
        telemetry=telemetry,
        session=1,
        params=PARAMS,
        prices=prices,
        caps=caps,
        sleep=lambda s: None,
    )
    result = loop.run()
    (event,) = events_of(run_dir, "llm_call")
    assert event["input_tokens"] == 10_000
    assert event["cache_read_tokens"] == 8_000
    assert event["cache_write_tokens"] == 500
    expected = (1_500 * 3.0 + 8_000 * 0.30 + 500 * 3.75 + 100 * 15.0) / 1e6
    assert event["cost_usd"] == pytest.approx(expected)
    # cumulative_tokens semantics unchanged: total input + output.
    assert event["cumulative_tokens"] == 10_100
    assert result.session_tokens == 10_100


def test_failed_attempts_emit_zero_cache_fields(run_dir):
    adapter = ScriptedAdapter(
        AdapterError("boom", retryable=True),
        response(end_call()),
    )
    loop, _, _ = make_loop(run_dir, adapter)
    loop.run()
    failed = events_of(run_dir, "llm_call")[0]
    assert failed["usage_unknown"] is True
    assert failed["cache_read_tokens"] == 0
    assert failed["cache_write_tokens"] == 0


def test_cumulative_carries_across_sessions(run_dir):
    adapter = ScriptedAdapter(response(end_call(), tokens=(1000, 100)))
    caps = {"session_token_cap": 100_000}
    scaffold = ScaffoldTools(run_dir, session_number=2)
    telemetry = TelemetryWriter(run_dir / "telemetry.jsonl", run_id="test-run")
    loop = AgentLoop(
        adapter=adapter,
        model="test-model",
        system="s",
        kickoff_text=KICKOFF,
        continuation_text=CONTINUE,
        scaffold=scaffold,
        game=None,
        telemetry=telemetry,
        session=2,
        params=PARAMS,
        prices=PRICES,
        caps=LoopCaps(**caps),
        cumulative_usd=5.0,
        cumulative_tokens=1_000_000,
        sleep=lambda s: None,
    )
    result = loop.run()
    assert result.cumulative_usd == pytest.approx(5.0 + 1000 * 3.0 / 1e6 + 100 * 15.0 / 1e6)
    assert result.cumulative_tokens == 1_001_100


# --- carried set_next_wake (cap-skipped final-turn intent) -------------------------


def test_token_cap_carries_final_turn_wake_intent(run_dir):
    adapter = ScriptedAdapter(
        response(
            call("get_status", id_="s1"),
            call("set_next_wake", {"minutes_from_now": 45}, id_="w2"),
            tokens=(59_000, 2_000),
        ),
    )
    loop, scaffold, _ = make_loop(run_dir, adapter, session_token_cap=60_000)
    result = loop.run()
    assert result.reason == "token_cap"
    assert result.carried_wake == "applied"
    # Validated and clamped exactly as normal; no tool_call event, no
    # tool-result message — the agent never observes the carried execution.
    assert (scaffold.requested_wake_min, scaffold.clamped_wake_min) == (45, 45.0)
    assert events_of(run_dir, "tool_call") == []
    assert not any(isinstance(m, ToolResultMessage) for m in result.messages)


def test_tool_cap_carries_unexecuted_wake_from_tripping_batch(run_dir):
    adapter = ScriptedAdapter(
        response(
            call("get_status", id_="s1"),
            call("workspace_list", id_="l2"),
            call("set_next_wake", {"minutes_from_now": 30}, id_="w3"),
        )
    )
    loop, scaffold, _ = make_loop(run_dir, adapter, session_tool_cap=2)
    result = loop.run()
    assert result.reason == "tool_cap"
    assert result.carried_wake == "applied"
    assert scaffold.clamped_wake_min == 30.0
    assert len(events_of(run_dir, "tool_call")) == 2  # the carried intent emits none


def test_last_unexecuted_wake_wins_and_overrides_executed_one(run_dir):
    # Normal last-call-wins semantics extend to the carried intent.
    adapter = ScriptedAdapter(
        response(
            call("set_next_wake", {"minutes_from_now": 60}, id_="w1"),
            call("get_status", id_="s2"),
            call("set_next_wake", {"minutes_from_now": 120}, id_="w3"),
            call("set_next_wake", {"minutes_from_now": 240}, id_="w4"),
        )
    )
    loop, scaffold, _ = make_loop(run_dir, adapter, session_tool_cap=2)
    result = loop.run()
    assert result.reason == "tool_cap"
    assert result.carried_wake == "applied"
    assert scaffold.clamped_wake_min == 240.0


def test_invalid_carried_wake_is_discarded_and_recorded(run_dir):
    adapter = ScriptedAdapter(
        response(
            call("get_status", id_="s1"),
            call("set_next_wake", {"minutes_from_now": "soon"}, id_="w2"),
            tokens=(59_000, 2_000),
        ),
    )
    loop, scaffold, _ = make_loop(run_dir, adapter, session_token_cap=60_000)
    result = loop.run()
    assert result.reason == "token_cap"
    assert result.carried_wake == "invalid"
    assert scaffold.clamped_wake_min is None  # falls back to wake_default in the runner


def test_invalid_carried_wake_leaves_prior_executed_wake_standing(run_dir):
    adapter = ScriptedAdapter(
        response(call("set_next_wake", {"minutes_from_now": 90}, id_="w1")),
        response(
            call("set_next_wake", {"minutes_from_now": "soon"}, id_="w2"),
            tokens=(59_000, 2_000),
        ),
    )
    loop, scaffold, _ = make_loop(run_dir, adapter, session_token_cap=60_000)
    result = loop.run()
    assert result.carried_wake == "invalid"
    assert scaffold.clamped_wake_min == 90.0  # the executed wake stands


def test_errors_ending_never_carries_a_wake(run_dir):
    adapter = ScriptedAdapter(
        response(
            call("no_such_tool", id_="x1"),
            call("no_such_tool", id_="x2"),
            call("set_next_wake", {"minutes_from_now": 15}, id_="w3"),
        )
    )
    loop, scaffold, _ = make_loop(run_dir, adapter, max_consecutive_errors=2)
    result = loop.run()
    assert result.reason == "errors"
    assert result.carried_wake is None
    assert scaffold.clamped_wake_min is None


def test_end_session_skipped_wake_is_never_carried(run_dir):
    # D18 semantics unchanged: intents skipped by end_session stay skipped.
    adapter = ScriptedAdapter(
        response(
            end_call(id_="e1"),
            call("set_next_wake", {"minutes_from_now": 15}, id_="w2"),
        )
    )
    loop, scaffold, _ = make_loop(run_dir, adapter)
    result = loop.run()
    assert result.reason == "agent"
    assert result.carried_wake is None
    assert scaffold.clamped_wake_min is None


def test_cap_without_wake_intent_carries_nothing(run_dir):
    adapter = ScriptedAdapter(
        response(call("get_status", id_="s1"), tokens=(59_000, 2_000)),
    )
    loop, _, _ = make_loop(run_dir, adapter, session_token_cap=60_000)
    result = loop.run()
    assert result.reason == "token_cap"
    assert result.carried_wake is None


# --- empty LLM responses (retryable provider fault) --------------------------------


def empty_response():
    return AdapterResponse(
        text_blocks=(),
        tool_calls=(),
        stop_reason=StopReason.END_TURN,
        usage=Usage(input_tokens=0, output_tokens=0),
    )


def test_empty_zero_usage_response_is_retried_with_backoff(run_dir):
    sleeps = []
    adapter = ScriptedAdapter(
        empty_response(),
        empty_response(),
        response(end_call()),
    )
    loop, _, _ = make_loop(run_dir, adapter, sleeps=sleeps)
    result = loop.run()
    assert result.reason == "agent"
    assert sleeps == [1.0, 2.0]
    llm_events = events_of(run_dir, "llm_call")
    assert [e.get("empty_response", False) for e in llm_events] == [True, True, False]
    assert [e["retry_count"] for e in llm_events] == [0, 1, 2]
    assert [e["cost_usd"] for e in llm_events][:2] == [0.0, 0.0]
    # Usage was known (zero), not unknowable.
    assert all("usage_unknown" not in e for e in llm_events)
    # Never leaked into the continuation path: no continuation user message,
    # no assistant turn recorded for the empty attempts.
    assert all(not isinstance(m, UserMessage) or m.text == KICKOFF for m in result.messages)
    assert result.llm_calls == 3


def test_empty_responses_exhaust_retries_and_end_session(run_dir):
    adapter = ScriptedAdapter(*[empty_response() for _ in range(3)])
    loop, _, _ = make_loop(run_dir, adapter, retry_max_attempts=2)
    result = loop.run()
    assert result.reason == "errors"
    assert result.llm_calls == 3  # initial + 2 retries, all logged


def test_empty_but_billed_response_keeps_continuation_handling(run_dir):
    # Nonzero usage means the provider really produced (and billed) an
    # empty turn — the §5.4 continuation path applies, not the retry path.
    billed_empty = AdapterResponse(
        text_blocks=(),
        tool_calls=(),
        stop_reason=StopReason.END_TURN,
        usage=Usage(input_tokens=1000, output_tokens=1),
    )
    adapter = ScriptedAdapter(billed_empty, response(end_call()))
    loop, _, _ = make_loop(run_dir, adapter)
    result = loop.run()
    assert result.reason == "agent"
    llm_events = events_of(run_dir, "llm_call")
    assert "empty_response" not in llm_events[0]
    assert llm_events[1]["continuation"] is True


# --- misc ------------------------------------------------------------------------


def test_harness_scaffold_name_collision_rejected(run_dir):
    class ShadowGame(FakeGame):
        def __init__(self):
            super().__init__()
            self.tool_defs = [
                ToolDef(name="get_status", description="d", input_schema={"type": "object"})
            ]

    with pytest.raises(ValueError, match="shadow"):
        make_loop(run_dir, ScriptedAdapter(), game=ShadowGame())


def test_set_next_wake_state_survives_loop(run_dir):
    adapter = ScriptedAdapter(
        response(call("set_next_wake", {"minutes_from_now": 90}, id_="w1"), end_call(id_="e2"))
    )
    loop, scaffold, _ = make_loop(run_dir, adapter)
    loop.run()
    assert scaffold.requested_wake_min == 90
    assert scaffold.clamped_wake_min == 90.0
