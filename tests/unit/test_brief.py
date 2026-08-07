"""Session-start status brief: injection, verbatimness, provenance, degradation.

SPEC P1.12, I24, X20, X21, X22, D7.
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
from kami_agent.lens import CODE_UNAVAILABLE, LensQueryError, LensUnavailableError
from kami_agent.loop import (
    BRIEF_ARGS,
    BRIEF_QUERY,
    BRIEF_TOOL,
    AgentLoop,
    GameToolResult,
    LoopCaps,
)
from kami_agent.telemetry import TelemetryWriter, read_events, validate_event
from kami_agent.tools.errors import ToolError
from kami_agent.tools.scaffold import ScaffoldTools

PRICES = PriceTable(input_usd_per_mtok=3.0, output_usd_per_mtok=15.0)
PARAMS = SamplingParams(max_tokens=4096)
KICKOFF = "Session start."
CONTINUE = "Continue. To end this session, call end_session."

# A roster envelope in the shape the pinned daemon serves: one line per
# kami (index, on-chain state, [hp, hpTotal]) plus the room the account is
# standing in. No authored strings anywhere, by the query's design, so the
# untrusted path list is empty and stays empty in name-free mode.
ROSTER_ENVELOPE = {
    "data": {
        "account": {"index": 4271, "roomIndex": 11},
        "kamis": [
            {"index": 1041, "state": "HARVESTING", "hp": [48, 60]},
            {"index": 1054, "state": "RESTING", "hp": [33, 81]},
        ],
    },
    "untrusted": [],
    "meta": {
        "servedAt": "2026-08-07T12:00:00.000Z",
        "blockNumber": 8814052,
        "stale": False,
        "mode": "daemon",
    },
}
ROSTER_JSON = json.dumps(ROSTER_ENVELOPE, ensure_ascii=False)

PARTY_DEF = ToolDef(
    name="lens_party",
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


class FakeLens:
    """Serves the roster query; records every query it is asked for."""

    def __init__(self, *, envelope=None, raises=None):
        self.queries = []
        self._envelope = ROSTER_ENVELOPE if envelope is None else envelope
        self._raises = raises

    def query(self, name, args=None):
        self.queries.append((name, args))
        if self._raises is not None:
            raise self._raises
        return self._envelope


class Game:
    """A harness surface with the usual world-state tools on it."""

    def __init__(self, tool_defs=None):
        self.tool_defs = [PARTY_DEF, OTHER_DEF] if tool_defs is None else tool_defs
        self.calls = []

    def execute(self, name, args):
        self.calls.append((name, args))
        return GameToolResult(content=json.dumps({"ok": True, "tool": name}))


@pytest.fixture
def run_dir(tmp_path):
    (tmp_path / "reference").mkdir()
    (tmp_path / "reference" / "gdd.md").write_text("lore " * 100)
    return tmp_path


def make_loop(run_dir, adapter, *, lens, game=None, session=1, **cap_overrides):
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
        game=Game() if game is None else game,
        lens=lens,
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
    lens = FakeLens()
    adapter = ScriptedAdapter(response(end_call()))
    loop = make_loop(run_dir, adapter, lens=lens)
    loop.run()

    # Executed exactly once, with no account argument: the daemon's own
    # default operator resolves it (D7).
    assert lens.queries == [(BRIEF_QUERY, None)]
    assert BRIEF_ARGS == {}
    # And it was already in context when the model was first called.
    first_request = adapter.requests[0]["messages"]
    assert isinstance(first_request[0], UserMessage)
    assert first_request[0].text == KICKOFF
    assert isinstance(first_request[1], AssistantMessage)
    assert [c.name for c in first_request[1].tool_calls] == [BRIEF_TOOL]
    assert isinstance(first_request[2], ToolResultMessage)
    assert first_request[2].tool_call_id == first_request[1].tool_calls[0].id


def test_brief_result_is_injected_verbatim(run_dir):
    lens = FakeLens()
    loop = make_loop(run_dir, ScriptedAdapter(response(end_call())), lens=lens)
    result = loop.run()

    injected = next(m for m in result.messages if isinstance(m, ToolResultMessage))
    # Whole-message equality: envelope untouched, nothing summarized,
    # reordered, filtered, or annotated. Serialized compactly — the
    # scaffold owns this serialization now, and every byte of it lands in
    # the fixed floor of every model call in the session.
    assert injected.content == ROSTER_JSON
    assert json.loads(injected.content) == ROSTER_ENVELOPE
    assert injected.is_error is False


def test_the_brief_is_a_special_path_and_the_agent_cannot_make_it(run_dir):
    """X22, replacing the retired 'no special path' claim.

    The roster is read straight from the daemon. It is not a tool, it is
    not on the surface the model is shown, and an agent that tries the
    name gets what any unknown tool gets.
    """
    lens = FakeLens()
    game = Game()
    attempt = ToolCall(id="t1", name=BRIEF_TOOL, args={})
    loop = make_loop(
        run_dir, ScriptedAdapter(response(attempt), response(end_call())), lens=lens, game=game
    )
    loop.run()

    assert BRIEF_TOOL not in {t.name for t in loop._tool_defs}
    # The daemon was queried once, by the scaffold — never by the agent.
    assert lens.queries == [(BRIEF_QUERY, None)]
    assert game.calls == []
    attempted = [e for e in tool_events(run_dir) if e["initiator"] == "model"][0]
    assert attempted["ok"] is False
    assert attempted["error"] == f"unknown tool: {BRIEF_TOOL}"
    assert attempted["source"] == "scaffold"


def test_full_per_kami_detail_stays_on_the_harness_surface(run_dir):
    """What the compact brief drops, the agent can still ask for itself."""
    game = Game()
    party = ToolCall(id="t1", name="lens_party", args={"account_index": -1})
    loop = make_loop(
        run_dir,
        ScriptedAdapter(response(party), response(end_call())),
        lens=FakeLens(),
        game=game,
    )
    loop.run()
    assert game.calls == [("lens_party", {"account_index": -1})]


def test_a_harness_tool_of_the_briefs_name_is_refused_before_any_model_call(run_dir):
    """Two things called one name would be a mis-pin, not a runtime state."""
    shadow = ToolDef(name=BRIEF_TOOL, description="d", input_schema={"type": "object"})
    with pytest.raises(ValueError, match=BRIEF_TOOL):
        make_loop(
            run_dir,
            ScriptedAdapter(response(end_call())),
            lens=FakeLens(),
            game=Game([shadow]),
        )


# --- telemetry provenance (P9) -----------------------------------------------


def test_brief_is_telemetered_and_marked_scaffold_initiated_from_the_lens(run_dir):
    lens = FakeLens()
    loop = make_loop(run_dir, ScriptedAdapter(response(end_call())), lens=lens)
    loop.run()

    events = tool_events(run_dir)
    for event in events:
        validate_event(event)
    assert events[0]["tool"] == BRIEF_TOOL
    assert events[0]["initiator"] == "scaffold"
    # Neither harness nor scaffold: the daemon owns the answer.
    assert events[0]["source"] == "lens"
    assert events[0]["ok"] is True
    # A scaffold-minted call id is not a provider fact, so it is not
    # recorded as one.
    assert "provider_call_id" not in events[0]
    assert all("initiator" in e for e in events)
    assert [e["initiator"] for e in events[1:]] == ["model"] * (len(events) - 1)


def test_brief_records_the_freshness_of_what_it_injected(run_dir):
    stale_envelope = {
        **ROSTER_ENVELOPE,
        "meta": {**ROSTER_ENVELOPE["meta"], "stale": True, "blockNumber": 8813000},
    }
    loop = make_loop(
        run_dir, ScriptedAdapter(response(end_call())), lens=FakeLens(envelope=stale_envelope)
    )
    loop.run()
    event = tool_events(run_dir)[0]
    assert event["lens_stale"] is True
    assert event["lens_block"] == 8813000
    # Not a second channel: the same values were inside what the agent saw.
    assert json.loads(event["error"]) if not event["ok"] else True


def test_brief_counts_toward_emitted_tool_calls(run_dir):
    loop = make_loop(run_dir, ScriptedAdapter(response(end_call())), lens=FakeLens())
    result = loop.run()
    assert result.tool_calls == len(tool_events(run_dir)) == 2


# --- the brief bounds nothing the agent does (X20) ---------------------------


def test_brief_consumes_no_session_tool_cap(run_dir):
    """session_tool_cap bounds agent-executed intents; the brief is not one."""
    calls = [ToolCall(id=f"t{i}", name="lens_node", args={}) for i in range(2)]
    adapter = ScriptedAdapter(response(*calls), response(end_call()))
    loop = make_loop(run_dir, adapter, lens=FakeLens(), session_tool_cap=3)
    result = loop.run()
    # Had the brief counted, it would have been executed intent 1 and the
    # cap would have tripped on the second lens_node — end_session would
    # never have run.
    assert result.reason == "agent"


def test_a_failed_brief_does_not_advance_the_consecutive_error_counter(run_dir):
    class FailingGame(Game):
        def execute(self, name, args):
            raise ToolError("unavailable")

    lens = FakeLens(raises=LensUnavailableError("the daemon is unreachable"))
    calls = [ToolCall(id="t1", name="lens_node", args={})]
    adapter = ScriptedAdapter(response(*calls), response(end_call()))
    loop = make_loop(run_dir, adapter, lens=lens, game=FailingGame(), max_consecutive_errors=2)
    result = loop.run()
    # lens_node fails too, so it is error #1. Had the brief counted, the cap
    # would have been reached there and the session would have ended as
    # errors before end_session.
    assert result.reason == "agent"


def test_brief_never_feeds_the_repetition_breaker(run_dir):
    """An identical_call cap of 3 must count only the agent's own repeats."""
    repeat = [ToolCall(id=f"t{i}", name="lens_node", args={}) for i in range(2)]
    adapter = ScriptedAdapter(response(*repeat), response(end_call()))
    loop = make_loop(run_dir, adapter, lens=FakeLens(), repetition_identical_cap=3)
    result = loop.run()
    assert result.reason == "agent"
    assert result.repetition is None


# --- degrade visibly, never block (X21) --------------------------------------


def test_a_query_error_is_injected_as_the_daemons_own_words(run_dir):
    lens = FakeLens(raises=LensQueryError("BAD_ARGS", "account index must be an integer"))
    loop = make_loop(run_dir, ScriptedAdapter(response(end_call())), lens=lens)
    result = loop.run()

    injected = next(m for m in result.messages if isinstance(m, ToolResultMessage))
    assert json.loads(injected.content) == {
        "error": {"code": "BAD_ARGS", "message": "account index must be an integer"}
    }
    assert injected.is_error is True
    assert result.reason == "agent"
    event = tool_events(run_dir)[0]
    assert event["ok"] is False
    assert event["initiator"] == "scaffold"
    assert event["error"] == injected.content


def test_an_unreachable_daemon_is_injected_as_the_minimal_record(run_dir):
    """The one string the scaffold contributes is the code; the rest is the OS's."""
    lens = FakeLens(raises=LensUnavailableError("cannot connect to /run/lens.sock: [Errno 2]"))
    loop = make_loop(run_dir, ScriptedAdapter(response(end_call())), lens=lens)
    result = loop.run()

    injected = next(m for m in result.messages if isinstance(m, ToolResultMessage))
    record = json.loads(injected.content)
    assert set(record) == {"error"}
    assert record["error"]["code"] == CODE_UNAVAILABLE
    assert record["error"]["message"] == "cannot connect to /run/lens.sock: [Errno 2]"
    assert result.reason == "agent"


def test_a_failing_brief_is_attempted_exactly_once(run_dir):
    """Single attempt: no retry loop, no second query, no fallback content."""
    lens = FakeLens(raises=LensUnavailableError("unavailable"))
    loop = make_loop(run_dir, ScriptedAdapter(response(end_call())), lens=lens)
    loop.run()
    assert lens.queries == [(BRIEF_QUERY, None)]


def test_no_brief_when_no_daemon_is_configured(run_dir):
    loop = make_loop(run_dir, ScriptedAdapter(response(end_call())), lens=None)
    result = loop.run()
    assert not [e for e in tool_events(run_dir) if e["initiator"] == "scaffold"]
    # The session opens on the kickoff and the model's own first turn — no
    # injected pair in between.
    assert isinstance(result.messages[0], UserMessage)
    assert [c.name for c in result.messages[1].tool_calls] == ["end_session"]


def test_an_oversized_brief_is_capped_like_any_tool_result(run_dir):
    """The byte cap is the only transformation any tool result gets (P2)."""
    huge = {"data": {"kamis": [{"index": i, "state": "RESTING", "hp": [1, 1]} for i in range(500)]}}
    loop = make_loop(
        run_dir,
        ScriptedAdapter(response(end_call())),
        lens=FakeLens(envelope=huge),
        tool_result_max_bytes=1024,
    )
    result = loop.run()
    injected = next(m for m in result.messages if isinstance(m, ToolResultMessage))
    assert len(injected.content.encode()) < len(json.dumps(huge))
    event = tool_events(run_dir)[0]
    assert event["truncated"] is True
    assert event["original_bytes"] == len(json.dumps(huge, ensure_ascii=False).encode())
