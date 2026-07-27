"""Session-start status brief: injection, verbatimness, provenance, degradation.

SPEC P1.12, I24, X20, X21.
"""

import json

import pytest

from kami_agent.adapters.base import (
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
from kami_agent.loop import BRIEF_ARGS, BRIEF_TOOL, AgentLoop, GameToolResult, LoopCaps
from kami_agent.telemetry import TelemetryWriter, read_events, validate_event
from kami_agent.tools.errors import ToolError
from kami_agent.tools.scaffold import ScaffoldTools

PRICES = PriceTable(input_usd_per_mtok=3.0, output_usd_per_mtok=15.0)
PARAMS = SamplingParams(max_tokens=4096)
KICKOFF = "Session start."
CONTINUE = "Continue. To end this session, call end_session."

# A party envelope in the shape the pinned harness serves: on-chain state
# per kami plus the calculated vitals (HP now/total/%, HP rate per hour,
# cooldown seconds, accrual).
PARTY_ENVELOPE = {
    "data": {
        "account": {"index": 7, "name": "fixture"},
        "kamis": [
            {
                "id": "0x01",
                "index": 1,
                "name": "one",
                "state": "HARVESTING",
                "hp": {"current": 41, "total": 90, "percent": 45.6},
                "hpRatePerHr": "-3.10",
                "cooldownSec": 0,
            }
        ],
    },
    "untrusted": ["account.name", "kamis[].name"],
    "meta": {"stale": 3},
}
PARTY_JSON = json.dumps(PARTY_ENVELOPE)

BRIEF_DEF = ToolDef(
    name=BRIEF_TOOL,
    description="Party report for an account: every kami with full vitals.",
    input_schema={
        "type": "object",
        "properties": {"account_index": {"type": "integer", "default": -1}},
    },
)
OTHER_DEF = ToolDef(
    name="lens_node", description="d", input_schema={"type": "object", "properties": {}}
)


def response(*tool_calls, tokens=(1000, 100)):
    return AdapterResponse(
        text_blocks=(),
        tool_calls=tuple(tool_calls),
        stop_reason=StopReason.TOOL_USE if tool_calls else StopReason.END_TURN,
        usage=Usage(input_tokens=tokens[0], output_tokens=tokens[1]),
    )


def end_call(id_="t-end"):
    return ToolCall(id=id_, name="end_session", args={"reason": "done"})


class ScriptedAdapter:
    def __init__(self, *script):
        self.script = list(script)
        self.requests = []

    def complete(self, system, messages, tools, params):
        self.requests.append({"system": system, "messages": list(messages), "tools": tools})
        return self.script.pop(0)


class BriefGame:
    """Serves the brief tool; records every execution it is asked for."""

    def __init__(self, *, tool_defs=None, result=None, raises=None):
        self.tool_defs = [BRIEF_DEF, OTHER_DEF] if tool_defs is None else tool_defs
        self.calls = []
        self._result = result if result is not None else PARTY_JSON
        self._raises = raises

    def execute(self, name, args):
        self.calls.append((name, args))
        if self._raises is not None:
            raise self._raises
        return GameToolResult(content=self._result)


@pytest.fixture
def run_dir(tmp_path):
    (tmp_path / "reference").mkdir()
    (tmp_path / "reference" / "gdd.md").write_text("lore " * 100)
    return tmp_path


def make_loop(run_dir, adapter, *, game, session=1, **cap_overrides):
    caps = LoopCaps(
        session_token_cap=cap_overrides.pop("session_token_cap", 100_000), **cap_overrides
    )
    return AgentLoop(
        adapter=adapter,
        model="test-model",
        system="system prompt",
        kickoff_text=KICKOFF,
        continuation_text=CONTINUE,
        scaffold=ScaffoldTools(run_dir, session_number=session),
        game=game,
        telemetry=TelemetryWriter(run_dir / "telemetry.jsonl", run_id="test-run"),
        session=session,
        params=PARAMS,
        prices=PRICES,
        caps=caps,
        sleep=lambda s: None,
    )


def tool_events(run_dir):
    return [e for e in read_events(run_dir / "telemetry.jsonl") if e["event"] == "tool_call"]


# --- the brief reaches call 1 (P1.12) ----------------------------------------


def test_brief_is_executed_before_the_first_model_call(run_dir):
    game = BriefGame()
    adapter = ScriptedAdapter(response(end_call()))
    loop = make_loop(run_dir, adapter, game=game)
    loop.run()

    # Executed exactly once, with the account's own operator.
    assert game.calls == [(BRIEF_TOOL, dict(BRIEF_ARGS))]
    # And it was already in context when the model was first called.
    first_request = adapter.requests[0]["messages"]
    assert isinstance(first_request[0], UserMessage)
    assert first_request[0].text == KICKOFF
    assert isinstance(first_request[1], AssistantMessage)
    assert [c.name for c in first_request[1].tool_calls] == [BRIEF_TOOL]
    assert isinstance(first_request[2], ToolResultMessage)
    assert first_request[2].tool_call_id == first_request[1].tool_calls[0].id


def test_brief_result_is_injected_verbatim(run_dir):
    game = BriefGame()
    loop = make_loop(run_dir, ScriptedAdapter(response(end_call())), game=game)
    result = loop.run()

    injected = next(m for m in result.messages if isinstance(m, ToolResultMessage))
    # Whole-message equality: envelope untouched, nothing summarized,
    # reordered, filtered, or annotated.
    assert injected.content == PARTY_JSON
    assert injected.is_error is False


def test_brief_is_the_general_tool_and_stays_available_to_the_agent(run_dir):
    """No special path: the agent can call the same tool itself, and does so
    through the same execution and telemetry, distinguished only by initiator."""
    game = BriefGame()
    agent_call = ToolCall(id="t1", name=BRIEF_TOOL, args={"account_index": 12})
    loop = make_loop(
        run_dir, ScriptedAdapter(response(agent_call), response(end_call())), game=game
    )
    loop.run()

    assert game.calls == [(BRIEF_TOOL, dict(BRIEF_ARGS)), (BRIEF_TOOL, {"account_index": 12})]
    brief, chosen = [e for e in tool_events(run_dir) if e["tool"] == BRIEF_TOOL]
    assert brief["initiator"] == "scaffold"
    assert chosen["initiator"] == "model"
    assert brief["source"] == chosen["source"] == "harness"


# --- telemetry provenance (P9) -----------------------------------------------


def test_brief_is_telemetered_like_any_tool_call_and_marked_scaffold_initiated(run_dir):
    game = BriefGame()
    loop = make_loop(run_dir, ScriptedAdapter(response(end_call())), game=game)
    loop.run()

    events = tool_events(run_dir)
    for event in events:
        validate_event(event)
    assert events[0]["tool"] == BRIEF_TOOL
    assert events[0]["initiator"] == "scaffold"
    assert events[0]["source"] == "harness"
    assert events[0]["ok"] is True
    # Every model-chosen call carries the other value; the field is never
    # absent in a stream this version wrote.
    assert all("initiator" in e for e in events)
    assert [e["initiator"] for e in events[1:]] == ["model"] * (len(events) - 1)


def test_brief_counts_toward_emitted_tool_calls(run_dir):
    game = BriefGame()
    loop = make_loop(run_dir, ScriptedAdapter(response(end_call())), game=game)
    result = loop.run()
    assert result.tool_calls == len(tool_events(run_dir)) == 2


# --- the brief bounds nothing the agent does (X20) ---------------------------


def test_brief_consumes_no_session_tool_cap(run_dir):
    """session_tool_cap bounds agent-executed intents; the brief is not one."""
    game = BriefGame()
    calls = [ToolCall(id=f"t{i}", name="lens_node", args={}) for i in range(2)]
    adapter = ScriptedAdapter(response(*calls), response(end_call()))
    loop = make_loop(run_dir, adapter, game=game, session_tool_cap=3)
    result = loop.run()
    # Had the brief counted, it would have been executed intent 1 and the
    # cap would have tripped on the second lens_node — end_session would
    # never have run.
    assert result.reason == "agent"


def test_a_failed_brief_does_not_advance_the_consecutive_error_counter(run_dir):
    game = BriefGame(raises=ToolError("the daemon is unreachable"))
    calls = [ToolCall(id="t1", name="lens_node", args={})]
    adapter = ScriptedAdapter(response(*calls), response(end_call()))
    loop = make_loop(run_dir, adapter, game=game, max_consecutive_errors=2)
    result = loop.run()
    # lens_node raises too (same game), so it is error #1. Had the brief
    # counted, the cap would have been reached there and the session would
    # have ended as errors before end_session.
    assert result.reason == "agent"


def test_brief_never_feeds_the_repetition_breaker(run_dir):
    """An identical_call cap of 2 must count only the agent's own repeats."""
    game = BriefGame()
    repeat = [ToolCall(id=f"t{i}", name=BRIEF_TOOL, args=dict(BRIEF_ARGS)) for i in range(2)]
    adapter = ScriptedAdapter(response(*repeat), response(end_call()))
    loop = make_loop(run_dir, adapter, game=game, repetition_identical_cap=3)
    result = loop.run()
    # Three executions of the same signature happened (brief + two agent
    # calls); only the agent's two are counted, so the rule did not trip.
    assert result.reason == "agent"
    assert result.repetition is None


# --- degrade visibly, never block (X21) --------------------------------------


def test_a_failing_brief_is_injected_as_its_error_and_the_session_proceeds(run_dir):
    message = "kami-lens daemon unavailable (socket: /run/lens.sock)"
    game = BriefGame(raises=ToolError(message))
    loop = make_loop(run_dir, ScriptedAdapter(response(end_call())), game=game)
    result = loop.run()

    injected = next(m for m in result.messages if isinstance(m, ToolResultMessage))
    assert injected.content == message  # as-is: no rewording, no fallback content
    assert injected.is_error is True
    assert result.reason == "agent"
    event = tool_events(run_dir)[0]
    assert event["ok"] is False
    assert event["initiator"] == "scaffold"
    assert event["error"] == message


def test_a_failing_brief_is_attempted_exactly_once(run_dir):
    """Single attempt: no retry loop, no second call, no fallback content."""
    game = BriefGame(raises=ToolError("unavailable"))
    loop = make_loop(run_dir, ScriptedAdapter(response(end_call())), game=game)
    loop.run()
    assert game.calls == [(BRIEF_TOOL, dict(BRIEF_ARGS))]


def test_no_brief_when_the_loaded_surface_does_not_carry_the_tool(run_dir):
    game = BriefGame(tool_defs=[OTHER_DEF])
    loop = make_loop(run_dir, ScriptedAdapter(response(end_call())), game=game)
    result = loop.run()
    assert game.calls == []
    assert not [e for e in tool_events(run_dir) if e["initiator"] == "scaffold"]
    # The session opens on the kickoff and the model's own first turn — no
    # injected pair in between.
    assert isinstance(result.messages[0], UserMessage)
    assert [c.name for c in result.messages[1].tool_calls] == ["end_session"]


def test_no_brief_without_a_harness(run_dir):
    loop = make_loop(run_dir, ScriptedAdapter(response(end_call())), game=None)
    loop.run()
    assert not [e for e in tool_events(run_dir) if e["initiator"] == "scaffold"]


def test_an_oversized_brief_is_capped_like_any_tool_result(run_dir):
    """The byte cap is the only transformation any tool result gets (P2)."""
    game = BriefGame(result="x" * 5000)
    loop = make_loop(
        run_dir, ScriptedAdapter(response(end_call())), game=game, tool_result_max_bytes=1024
    )
    result = loop.run()
    injected = next(m for m in result.messages if isinstance(m, ToolResultMessage))
    assert len(injected.content.encode()) < 5000
    event = tool_events(run_dir)[0]
    assert event["truncated"] is True
    assert event["original_bytes"] == 5000
