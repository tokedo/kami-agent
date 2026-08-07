"""Agent loop: serialization (I12), error semantics (P2), retries (P8), guard (X7)."""

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
from kami_agent.tools.errors import ToolError
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


# --- receipt status: three terminal states, kept distinct (D1, P9) ---------------

REVERT_MESSAGE = (
    "transaction 0xbadbeef landed on-chain in block 77 and REVERTED: gas was "
    "spent (91234 gas) and no state change was applied. Revert reason "
    "(best-effort eth_call replay at block 77): insufficient stamina"
)
UNCONFIRMED_MESSAGE = (
    "transaction 0xfeed is UNCONFIRMED: it was broadcast, but no receipt arrived "
    "within 120s. It may still be included and spend gas later. Check its "
    "on-chain status before retrying — a blind retry can execute the action twice."
)
REJECTED_MESSAGE = "validation failed; no transaction sent: kami 42 is RESTING, not HARVESTING"


class RaisingGame(FakeGame):
    """A harness that raises its transaction outcomes, as v2 does."""

    def __init__(self, message):
        super().__init__()
        self._message = message

    def execute(self, name, args):
        self.calls.append((name, args))
        raise ToolError(self._message)


@pytest.mark.parametrize(
    ("message", "state"),
    [
        (REVERT_MESSAGE, "reverted"),
        (UNCONFIRMED_MESSAGE, "unconfirmed"),
        (REJECTED_MESSAGE, "validation_rejected"),
    ],
)
def test_raised_outcome_reaches_the_model_verbatim_and_telemetry_by_field(run_dir, message, state):
    """The model gets the harness's words; analysis gets a field, not those words."""
    adapter = ScriptedAdapter(
        response(call("get_state", id_="g1")),
        response(end_call()),
    )
    loop, _, _ = make_loop(run_dir, adapter, game=RaisingGame(message))
    loop.run()

    # (a) verbatim to the model: the whole message, nothing prepended,
    # appended, reworded, or summarized.
    results = [m for m in adapter.requests[1]["messages"] if isinstance(m, ToolResultMessage)]
    assert results[0].is_error
    assert results[0].content == message

    # (b) the terminal state is a field, so the split needs no string-matching.
    event = events_of(run_dir, "tool_call")[0]
    assert event["tx_terminal_state"] == state
    assert event["ok"] is False
    assert event["error"] == message


def test_a_raised_outcome_is_executed_once_and_never_retried(run_dir):
    """No retry-swallowing: a reverted or unconfirmed tx must not be re-sent."""
    game = RaisingGame(UNCONFIRMED_MESSAGE)
    adapter = ScriptedAdapter(
        response(call("get_state", id_="g1")),
        response(end_call()),
    )
    loop, _, _ = make_loop(run_dir, adapter, game=game)
    loop.run()
    assert game.calls == [("get_state", {})]
    assert len(events_of(run_dir, "tool_call")) == 2  # the call + end_session


def test_scaffold_failures_carry_no_terminal_state(run_dir):
    """Only the harness reports transaction outcomes; scaffold errors are not ones."""
    adapter = ScriptedAdapter(
        response(call("workspace_read", {"path": "workspace/ghost.md"}, id_="x1")),
        response(end_call()),
    )
    loop, _, _ = make_loop(run_dir, adapter)
    loop.run()
    assert "tx_terminal_state" not in events_of(run_dir, "tool_call")[0]


def test_confirmed_success_is_recorded_from_the_harness_result(run_dir):
    class ConfirmingGame(FakeGame):
        def execute(self, name, args):
            self.calls.append((name, args))
            return GameToolResult(
                content='{"tx_hash": "0xc0ffee", "status": "success"}',
                tx_hash="0xc0ffee",
                terminal_state="confirmed_success",
            )

    adapter = ScriptedAdapter(
        response(call("get_state", id_="g1")),
        response(end_call()),
    )
    loop, _, _ = make_loop(run_dir, adapter, game=ConfirmingGame())
    loop.run()
    event = events_of(run_dir, "tool_call")[0]
    assert event["ok"] is True
    assert event["tx_terminal_state"] == "confirmed_success"


def test_reads_carry_no_terminal_state(run_dir):
    adapter = ScriptedAdapter(
        response(call("get_state", id_="g1")),
        response(end_call()),
    )
    loop, _, _ = make_loop(run_dir, adapter, game=FakeGame())
    loop.run()
    assert "tx_terminal_state" not in events_of(run_dir, "tool_call")[0]


# --- I12: strict serialization + end_session batch semantics ------------------


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


# --- P2 error semantics -----------------------------------------------------


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


# --- X7 context guard (post-call) ---------------------------------------------------------


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


# --- P8 retries ---------------------------------------------------------------


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


# --- I16 result cap --------------------------------------------------------------


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
    # SPEC P7.1/P9: llm_call preserves the cache decomposition and cost_usd
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
    # I12 semantics unchanged: intents skipped by end_session stay skipped.
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
    # empty turn — the P2 continuation path applies, not the retry path.
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


# --- per-call identity in the stream (P9 call_seq, 0.4.0) --------------------


def test_every_emitted_row_carries_a_monotonic_call_identity(run_dir):
    """Telemetry carried no call identity at all before 0.4.0, so two rows of
    the same tool in one turn could not be told apart without the transcript."""
    calls = [call("get_state", id_="a"), call("get_state", id_="b"), end_call()]
    loop, _, _ = make_loop(run_dir, ScriptedAdapter(response(*calls)), game=FakeGame())
    loop.run()
    rows = events_of(run_dir, "tool_call")
    assert [r["call_seq"] for r in rows] == list(range(1, len(rows) + 1))


def test_skipped_intents_also_get_an_identity(run_dir):
    """One row, one number — including rows for intents that never executed."""
    calls = [end_call(), call("get_state", id_="after")]
    loop, _, _ = make_loop(run_dir, ScriptedAdapter(response(*calls)), game=FakeGame())
    loop.run()
    rows = events_of(run_dir, "tool_call")
    assert [r["call_seq"] for r in rows] == [1, 2]
    assert rows[1]["skipped"] is True


def test_provider_call_ids_are_recorded_verbatim(run_dir):
    calls = [call("get_state", id_="call_AAA111"), end_call(id_="call_BBB222")]
    loop, _, _ = make_loop(run_dir, ScriptedAdapter(response(*calls)), game=FakeGame())
    loop.run()
    rows = events_of(run_dir, "tool_call")
    assert [r["provider_call_id"] for r in rows] == ["call_AAA111", "call_BBB222"]
    assert not any(r.get("provider_call_id_duplicate") for r in rows)


def test_a_reused_provider_call_id_is_flagged_and_both_calls_still_execute(run_dir):
    """The loop routes positionally and cannot alias results, so execution is
    unaffected. What breaks is any downstream join that pairs a result to its
    call BY ID — so the rows say so, and nothing raises (X23)."""
    game = FakeGame()
    duplicated = [
        call("get_state", {"room": 8}, id_="call_SAME"),
        call("get_state", {"room": 17}, id_="call_SAME"),
        end_call(),
    ]
    loop, _, _ = make_loop(run_dir, ScriptedAdapter(response(*duplicated)), game=game)
    result = loop.run()

    # Both executed, in order, with their own arguments: no deduplication.
    assert game.calls == [("get_state", {"room": 8}), ("get_state", {"room": 17})]
    assert result.reason == "agent"
    rows = [e for e in events_of(run_dir, "tool_call") if e["tool"] == "get_state"]
    assert [r["provider_call_id_duplicate"] for r in rows] == [True, True]
    # The scaffold's own identity still separates them, which is the point.
    assert rows[0]["call_seq"] != rows[1]["call_seq"]


# --- transaction evidence on the raised path (P9 tx_hash, txs, 0.4.0) --------


def test_a_reverted_transaction_records_its_hash_on_the_field(run_dir):
    """It used to live only in the error text — the one field P9 says not to parse."""

    class RevertingGame(FakeGame):
        def execute(self, name, args):
            raise ToolError(
                "transaction 0xbadbeef landed on-chain in block 77 and REVERTED: "
                "gas was spent (91234 gas) and no state change was applied."
            )

    loop, _, _ = make_loop(
        run_dir, ScriptedAdapter(response(call("get_state"), end_call())), game=RevertingGame()
    )
    loop.run()
    row = next(e for e in events_of(run_dir, "tool_call") if e["tool"] == "get_state")
    assert row["ok"] is False
    assert row["tx_terminal_state"] == "reverted"
    assert row["tx_hash"] == "0xbadbeef"


def test_a_scaffold_failure_carries_no_transaction_hash(run_dir):
    loop, _, _ = make_loop(run_dir, ScriptedAdapter(response(call("nope"), end_call())))
    loop.run()
    row = next(e for e in events_of(run_dir, "tool_call") if e["tool"] == "nope")
    assert "tx_hash" not in row


def test_in_band_receipts_and_error_shaped_results_reach_telemetry(run_dir):
    class MultiTxGame(FakeGame):
        def execute(self, name, args):
            return GameToolResult(
                content='{"error": "step failed", "txs": [{"tx_hash": "0xaa"}]}',
                txs=({"tx_hash": "0xaa", "status": "success"},),
            )

    loop, _, _ = make_loop(
        run_dir, ScriptedAdapter(response(call("get_state"), end_call())), game=MultiTxGame()
    )
    loop.run()
    row = next(e for e in events_of(run_dir, "tool_call") if e["tool"] == "get_state")
    assert row["txs"] == [{"tx_hash": "0xaa", "status": "success"}]
    # The tool RETURNED its failure rather than raising it: ok stays
    # exception-keyed and true, and the shape is named on its own field.
    assert row["ok"] is True
    assert row["result_error_shaped"] is True


def test_an_ordinary_result_carries_neither_field(run_dir):
    loop, _, _ = make_loop(
        run_dir, ScriptedAdapter(response(call("get_state"), end_call())), game=FakeGame()
    )
    loop.run()
    row = next(e for e in events_of(run_dir, "tool_call") if e["tool"] == "get_state")
    assert "txs" not in row
    assert "result_error_shaped" not in row


# --- write-ahead model requests (P3, P9 llm_request, 0.4.0) ------------------


def test_every_model_request_is_written_before_it_is_sent(run_dir):
    """The provider is billed when the request leaves; the outcome row is
    written when it comes back. This is what makes the gap between them
    recoverable rather than invisible."""
    seen = []

    class WatchingAdapter(ScriptedAdapter):
        def complete(self, system, messages, tools, params):
            seen.append([e["event"] for e in read_events(run_dir / "telemetry.jsonl")])
            return super().complete(system, messages, tools, params)

    loop, _, _ = make_loop(run_dir, WatchingAdapter(response(end_call())))
    loop.run()
    # At the moment the request went out, its marker was already on disk.
    assert seen[0][-1] == "llm_request"
    events = events_of(run_dir)
    requests = [e for e in events if e["event"] == "llm_request"]
    calls = [e for e in events if e["event"] == "llm_call"]
    assert [r["request_seq"] for r in requests] == [1]
    assert [c["request_seq"] for c in calls] == [1]


def test_each_retry_is_its_own_request(run_dir):
    """A retried call is a second billable request, so it gets its own marker."""
    adapter = ScriptedAdapter(
        AdapterError("429", retryable=True),
        AdapterError("429", retryable=True),
        response(end_call()),
    )
    loop, _, _ = make_loop(run_dir, adapter)
    loop.run()
    events = events_of(run_dir)
    assert [e["request_seq"] for e in events if e["event"] == "llm_request"] == [1, 2, 3]
    assert [e["request_seq"] for e in events if e["event"] == "llm_call"] == [1, 2, 3]


def test_write_ahead_markers_never_contribute_to_accounting(run_dir):
    """One exists per model call, so folding them would double every total."""
    from kami_agent.state import fold_telemetry

    loop, _, _ = make_loop(run_dir, ScriptedAdapter(response(end_call())))
    result = loop.run()
    state = fold_telemetry(events_of(run_dir))
    assert state.cumulative_tokens == result.cumulative_tokens == 1100
    assert state.cumulative_usd == pytest.approx(result.cumulative_usd)


def test_an_unnormalizable_response_is_recorded_instead_of_escaping(run_dir):
    """A fault the adapter did not turn into an AdapterError used to escape the
    loop entirely, leaving a billed call with no row of any kind."""

    class BrokenAdapter:
        def __init__(self):
            self.calls = 0

        def complete(self, system, messages, tools, params):
            self.calls += 1
            raise AttributeError("'NoneType' object has no attribute 'prompt_tokens'")

    adapter = BrokenAdapter()
    loop, _, _ = make_loop(run_dir, adapter)
    result = loop.run()

    assert result.reason == "errors"
    # Not retried: an unnormalizable response is not a transient fault.
    assert adapter.calls == 1
    events = events_of(run_dir)
    llm = [e for e in events if e["event"] == "llm_call"]
    assert len(llm) == 1
    assert llm[0]["usage_unknown"] is True
    assert llm[0]["stop_reason"] == "error"
    assert llm[0]["cost_usd"] == 0.0
    # And it is paired with its write-ahead marker, so nothing looks phantom.
    assert llm[0]["request_seq"] == 1
