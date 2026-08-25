"""Session-start status brief: injection, verbatimness, provenance, degradation.

SPEC P1.12, I24, X20, X21, D1.

**The brief moved onto the harness surface at 0.6.0.** Through 0.5.1 the
scaffold read the world-state daemon's socket itself and injected the
answer under a name the tool surface did not carry — a call the agent
could see in its own transcript and could never make. Agents tried to
make it anyway. The pinned harness now serves the same compact roster as
an ordinary tool, so the brief is a scaffold-initiated call of a real
tool, on exactly the terms the gas-balance injection has always had, and
the special path is gone.
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
from kami_agent.loop import (
    BALANCE_TOOL,
    BRIEF_ARGS,
    BRIEF_CALL_ID,
    BRIEF_TOOL,
    JOURNAL_TOOL,
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

# A roster envelope in the shape the pinned daemon serves and the harness
# returns verbatim: one line per kami (index, on-chain state, [hp,
# hpTotal]) plus the room the account is standing in. No authored strings
# anywhere, by the query's design, so the untrusted path list is empty and
# stays empty in name-free mode.
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

ROSTER_DEF = ToolDef(
    name=BRIEF_TOOL,
    description="Compact roster: one line per kami (index, state, HP) plus where the account is.",
    input_schema={
        "type": "object",
        "properties": {"account_index": {"type": "integer", "default": -1}},
    },
)
BALANCE_DEF = ToolDef(
    name=BALANCE_TOOL,
    description="Gas balances for every configured account.",
    input_schema={"type": "object", "properties": {"account": {"type": "string"}}},
)
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

BALANCE_JSON = '{"balances": {"main": {"owner_eth": "0.03", "operator_eth": "0.01"}}}'


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


class Game:
    """A harness surface carrying the roster and balance tools."""

    def __init__(self, tool_defs=None, *, roster=None, raises=None):
        self.tool_defs = (
            [ROSTER_DEF, BALANCE_DEF, PARTY_DEF, OTHER_DEF] if tool_defs is None else tool_defs
        )
        self.calls = []
        self._roster = ROSTER_JSON if roster is None else roster
        self._raises = raises

    def execute(self, name, args):
        self.calls.append((name, args))
        if name == BRIEF_TOOL:
            if self._raises is not None:
                raise self._raises
            return GameToolResult(content=self._roster)
        if name == BALANCE_TOOL:
            return GameToolResult(content=BALANCE_JSON)
        return GameToolResult(content=json.dumps({"ok": True, "tool": name}))


@pytest.fixture
def run_dir(tmp_path):
    (tmp_path / "reference").mkdir()
    (tmp_path / "reference" / "gdd.md").write_text("lore " * 100)
    return tmp_path


_DEFAULT = object()


def make_loop(run_dir, adapter, *, game=_DEFAULT, session=1, **cap_overrides):
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
        game=Game() if game is _DEFAULT else game,
        telemetry=TelemetryWriter(run_dir / "telemetry.jsonl", run_id="test-run"),
        session=session,
        params=PARAMS,
        prices=PRICES,
        caps=caps,
        sleep=lambda s: None,
    )


def tool_events(run_dir):
    return [e for e in read_events(run_dir / "telemetry.jsonl") if e["event"] == "tool_call"]


def brief_row(run_dir):
    return [e for e in tool_events(run_dir) if e["tool"] == BRIEF_TOOL][0]


# --- the brief reaches call 1 (P1.12) ----------------------------------------


def test_brief_is_executed_before_the_first_model_call(run_dir):
    game = Game()
    adapter = ScriptedAdapter(response(end_call()))
    make_loop(run_dir, adapter, game=game).run()
    # The harness saw the roster call before the model saw anything.
    assert game.calls[0] == (BRIEF_TOOL, {})
    first = adapter.requests[0]["messages"]
    assert isinstance(first[0], UserMessage)
    assert [c.name for c in first[1].tool_calls] == [BRIEF_TOOL]
    assert isinstance(first[2], ToolResultMessage)


def test_brief_result_is_injected_verbatim(run_dir):
    """The byte cap is the only transformation any tool result gets (P2)."""
    adapter = ScriptedAdapter(response(end_call()))
    make_loop(run_dir, adapter).run()
    result = next(
        m
        for m in adapter.requests[0]["messages"]
        if isinstance(m, ToolResultMessage) and m.tool_call_id == BRIEF_CALL_ID
    )
    assert result.content == ROSTER_JSON
    assert result.is_error is False


def test_no_arguments_are_sent_so_the_daemon_fills_the_account_in(run_dir):
    """The scaffold has no account identity of its own (D1, D7)."""
    game = Game()
    make_loop(run_dir, ScriptedAdapter(response(end_call())), game=game).run()
    assert BRIEF_ARGS == {}
    assert game.calls[0] == (BRIEF_TOOL, {})


# --- it is a real tool, and that is the point (X22 retired) ------------------


def test_the_brief_names_a_tool_the_agent_can_call_itself(run_dir):
    """No special path: the same name is on the surface the model is shown.

    Through 0.5.1 the injected call named something that was NOT a tool,
    so the transcript showed the agent a call it could never make — and
    agents kept trying. Now the name in the injected turn is a name in
    the tool list, so re-issuing it is an ordinary call.
    """
    adapter = ScriptedAdapter(
        response(ToolCall(id="r1", name=BRIEF_TOOL, args={})), response(end_call())
    )
    game = Game()
    make_loop(run_dir, adapter, game=game).run()
    assert BRIEF_TOOL in {t.name for t in adapter.requests[0]["tools"]}
    # The agent's own call ran, through the ordinary dispatch, and got the
    # same answer the injection did.
    assert game.calls.count((BRIEF_TOOL, {})) == 2
    rows = [e for e in tool_events(run_dir) if e["tool"] == BRIEF_TOOL]
    assert [e["initiator"] for e in rows] == ["scaffold", "model"]
    assert all(e["source"] == "harness" for e in rows)


def test_a_surface_without_the_roster_tool_is_refused_before_any_model_call(run_dir):
    """A mis-pin, not a runtime condition: it has no useful degraded shape.

    Unlike the balance tool, which degrades visibly every session (N10),
    a session that cannot see its own kamis is a session pointed at the
    wrong environment — and every session of the run would be that one.
    """
    with pytest.raises(ValueError) as excinfo:
        make_loop(
            run_dir,
            ScriptedAdapter(response(end_call())),
            game=Game(tool_defs=[BALANCE_DEF, OTHER_DEF]),
        )
    message = str(excinfo.value)
    assert BRIEF_TOOL in message
    assert "mis-pin" in message
    assert "3.0.0" in message


def test_full_per_kami_detail_stays_on_the_harness_surface(run_dir):
    """The compact roster is not a replacement for the party report."""
    adapter = ScriptedAdapter(response(end_call()))
    make_loop(run_dir, adapter).run()
    assert "lens_party" in {t.name for t in adapter.requests[0]["tools"]}


# --- telemetry provenance (P9) -----------------------------------------------


def test_brief_is_telemetered_and_marked_scaffold_initiated_from_the_harness(run_dir):
    make_loop(run_dir, ScriptedAdapter(response(end_call()))).run()

    events = tool_events(run_dir)
    for event in events:
        validate_event(event)
    assert events[0]["tool"] == BRIEF_TOOL
    assert events[0]["initiator"] == "scaffold"
    # The harness owns the tool now; `lens` as a source retires with the
    # direct daemon read (SPEC P9).
    assert events[0]["source"] == "harness"
    assert events[0]["ok"] is True
    # A scaffold-minted call id is not a provider fact, so it is not
    # recorded as one.
    assert "provider_call_id" not in events[0]
    assert all("initiator" in e for e in events)
    scaffold_rows = [e for e in events if e["initiator"] == "scaffold"]
    assert [e["tool"] for e in scaffold_rows] == [BRIEF_TOOL, BALANCE_TOOL, JOURNAL_TOOL]


def test_brief_records_the_freshness_of_what_it_injected(run_dir):
    """Operator-side only: the same values are inside the payload already."""
    stale_envelope = {
        **ROSTER_ENVELOPE,
        "meta": {**ROSTER_ENVELOPE["meta"], "stale": True, "blockNumber": 9000001},
    }
    game = Game(roster=json.dumps(stale_envelope))
    make_loop(run_dir, ScriptedAdapter(response(end_call())), game=game).run()
    row = brief_row(run_dir)
    assert row["lens_stale"] is True
    assert row["lens_block"] == 9000001


def test_an_unparseable_roster_costs_nothing(run_dir):
    """A harness free to change its serialization cannot break a session."""
    game = Game(roster="not json at all")
    result = make_loop(run_dir, ScriptedAdapter(response(end_call())), game=game).run()
    assert result.reason == "agent"
    row = brief_row(run_dir)
    assert row["ok"] is True
    assert "lens_stale" not in row
    assert "lens_block" not in row


def test_brief_counts_toward_emitted_tool_calls(run_dir):
    result = make_loop(run_dir, ScriptedAdapter(response(end_call()))).run()
    # brief + gas balances + journal entry + the agent's end_session.
    assert result.tool_calls == len(tool_events(run_dir)) == 4


# --- the brief bounds nothing the agent does (X20) ---------------------------


def test_brief_consumes_no_session_tool_cap(run_dir):
    calls = [ToolCall(id=f"t{i}", name="lens_node", args={}) for i in range(3)]
    adapter = ScriptedAdapter(response(*calls), response(end_call()))
    result = make_loop(run_dir, adapter, session_tool_cap=3).run()
    assert result.reason == "tool_cap"
    executed = [
        e for e in tool_events(run_dir) if e["initiator"] == "model" and not e.get("skipped")
    ]
    assert len(executed) == 3


def test_a_failed_brief_does_not_advance_the_consecutive_error_counter(run_dir):
    game = Game(raises=ToolError("the daemon is not answering"))
    adapter = ScriptedAdapter(response(end_call()))
    result = make_loop(run_dir, adapter, game=game, max_consecutive_errors=1).run()
    assert result.reason == "agent"


def test_brief_never_feeds_the_repetition_breaker(run_dir):
    adapter = ScriptedAdapter(response(end_call()))
    result = make_loop(run_dir, adapter, repetition_identical_cap=1).run()
    assert result.reason == "agent"


# --- degrade visibly, never block (X21) --------------------------------------


def test_a_harness_failure_is_injected_as_the_harness_own_words(run_dir):
    """The scaffold authors nothing about a failed brief any more (D1, I21)."""
    message = "cannot connect to the daemon socket: [Errno 2] No such file or directory"
    game = Game(raises=ToolError(message))
    adapter = ScriptedAdapter(response(end_call()))
    result = make_loop(run_dir, adapter, game=game).run()
    assert result.reason == "agent"
    injected = next(
        m
        for m in adapter.requests[0]["messages"]
        if isinstance(m, ToolResultMessage) and m.tool_call_id == BRIEF_CALL_ID
    )
    assert injected.content == message
    assert injected.is_error is True
    row = brief_row(run_dir)
    assert row["ok"] is False
    assert row["error"] == message


def test_a_failing_brief_is_attempted_exactly_once(run_dir):
    game = Game(raises=ToolError("boom"))
    make_loop(run_dir, ScriptedAdapter(response(end_call())), game=game).run()
    assert [c for c in game.calls if c[0] == BRIEF_TOOL] == [(BRIEF_TOOL, {})]


def test_no_brief_when_no_harness_is_configured(run_dir):
    """With no surface there is nothing to ask, so nothing is recorded."""
    result = make_loop(run_dir, ScriptedAdapter(response(end_call())), game=None).run()
    assert not [e for e in tool_events(run_dir) if e["tool"] == BRIEF_TOOL]
    # The journal read survives: it needs neither a daemon nor a harness.
    assert [e["tool"] for e in tool_events(run_dir) if e["initiator"] == "scaffold"] == [
        JOURNAL_TOOL
    ]
    assert isinstance(result.messages[0], UserMessage)
    assert [c.name for c in result.messages[1].tool_calls] == [JOURNAL_TOOL]


def test_an_oversized_brief_is_capped_like_any_tool_result(run_dir):
    """The byte cap is the only transformation any tool result gets (P2)."""
    huge = {"data": {"kamis": [{"index": i, "state": "RESTING", "hp": [1, 1]} for i in range(500)]}}
    game = Game(roster=json.dumps(huge))
    adapter = ScriptedAdapter(response(end_call()))
    make_loop(run_dir, adapter, game=game, tool_result_max_bytes=500).run()
    injected = next(
        m
        for m in adapter.requests[0]["messages"]
        if isinstance(m, ToolResultMessage) and m.tool_call_id == BRIEF_CALL_ID
    )
    assert "[truncated: showing the first 500 bytes of" in injected.content
    row = brief_row(run_dir)
    assert row["truncated"] is True


def test_the_injected_pair_is_marked_in_the_transcript(run_dir):
    """A synthesized turn is not a model turn, and the file says so (P12)."""
    result = make_loop(run_dir, ScriptedAdapter(response(end_call()))).run()
    assert isinstance(result.messages[1], AssistantMessage)
    assert result.messages[1].initiator == "scaffold"
    assert result.messages[2].initiator == "scaffold"
