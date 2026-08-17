"""Session-start injections beyond the brief: gas balances and the plan file.

SPEC P1.12 (the three injections and their order), D1 (the balance-source
coupling), X10 (ETH is a world resource, not the apparatus), X20/X21 (they
bound nothing and degrade visibly), P9 (their telemetry).
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
)
from kami_agent.governor import PriceTable
from kami_agent.loop import (
    BALANCE_TOOL,
    BRIEF_TOOL,
    PLAN_PATH,
    PLAN_TOOL,
    AgentLoop,
    GameToolResult,
    LoopCaps,
)
from kami_agent.telemetry import TelemetryWriter, read_events, validate_event
from kami_agent.tools.errors import ToolError
from kami_agent.tools.scaffold import (
    PROFILE_CONTROL,
    PROFILE_PLANNING,
    PROFILE_SEARCH,
    ScaffoldTools,
)

PRICES = PriceTable(input_usd_per_mtok=3.0, output_usd_per_mtok=15.0)
PARAMS = SamplingParams(max_tokens=4096)
KICKOFF = "Session start."
CONTINUE = "Continue. To end this session, call end_session."

# The pinned harness's payload shape for the balance tool: owner and
# operator ETH on Yominet plus the owner's mainnet balance, per account
# label, with no argument sent (the empty label means every account).
BALANCES = {
    "balances": {
        "main": {
            "operator_address": "0x00000000000000000000000000000000000000e1",
            "operator_eth": "0.0194280000000000",
            "owner_address": "0x00000000000000000000000000000000000000e2",
            "owner_eth": "0.0081120000000000",
            "owner_mainnet_eth": "0",
        }
    }
}
BALANCES_JSON = json.dumps(BALANCES)

ROSTER_ENVELOPE = {
    "data": {"account": {"index": 7, "roomIndex": 11}, "kamis": []},
    "untrusted": [],
    "meta": {"servedAt": "2026-08-17T12:00:00.000Z", "blockNumber": 41, "stale": False},
}

BALANCE_DEF = ToolDef(
    name=BALANCE_TOOL,
    description="Check native ETH gas balances for the account's wallets.",
    input_schema={"type": "object", "properties": {"account": {"type": "string", "default": ""}}},
)
OTHER_DEF = ToolDef(
    name="lens_node", description="d", input_schema={"type": "object", "properties": {}}
)


class Game:
    """A harness surface carrying the balance tool, as the pinned one does."""

    def __init__(self, tool_defs=None, raises=None):
        self.tool_defs = [OTHER_DEF, BALANCE_DEF] if tool_defs is None else tool_defs
        self.calls = []
        self._raises = raises

    def execute(self, name, args):
        self.calls.append((name, args))
        if self._raises is not None:
            raise self._raises
        if name == BALANCE_TOOL:
            return GameToolResult(content=BALANCES_JSON)
        return GameToolResult(content=json.dumps({"ok": True, "tool": name}))


class FakeLens:
    def query(self, name, args=None):
        return ROSTER_ENVELOPE


class ScriptedAdapter:
    def __init__(self, *script):
        self.script = list(script)
        self.requests = []

    def complete(self, system, messages, tools, params):
        self.requests.append({"system": system, "messages": list(messages), "tools": tools})
        return self.script.pop(0)


def response(*tool_calls):
    return AdapterResponse(
        text_blocks=(),
        tool_calls=tuple(tool_calls),
        stop_reason=StopReason.TOOL_USE if tool_calls else StopReason.END_TURN,
        usage=Usage(input_tokens=1000, output_tokens=100),
    )


def end_call(id_="t-end"):
    return ToolCall(id=id_, name="end_session", args={"reason": "done"})


@pytest.fixture
def run_dir(tmp_path):
    (tmp_path / "reference").mkdir()
    (tmp_path / "reference" / "gdd.md").write_text("lore " * 50)
    return tmp_path


_DEFAULT_GAME = object()


def make_loop(
    run_dir,
    adapter,
    *,
    game=_DEFAULT_GAME,
    lens=None,
    profile=PROFILE_CONTROL,
    session=1,
    **cap_overrides,
):
    caps = LoopCaps(
        session_token_cap=cap_overrides.pop("session_token_cap", 100_000), **cap_overrides
    )
    scaffold = ScaffoldTools(run_dir, session_number=session, profile=profile)
    return AgentLoop(
        adapter=adapter,
        model="test-model",
        system="system prompt",
        kickoff_text=KICKOFF,
        continuation_text=CONTINUE,
        scaffold=scaffold,
        game=Game() if game is _DEFAULT_GAME else game,
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


def injected(run_dir):
    return [e for e in tool_events(run_dir) if e["initiator"] == "scaffold"]


# --- order and shape (P1.12) --------------------------------------------------


def test_the_three_injections_run_in_order_before_the_first_model_call(run_dir):
    (run_dir / "workspace").mkdir(exist_ok=True)
    (run_dir / "workspace" / PLAN_PATH).write_text("goal: quests\n", encoding="utf-8")
    adapter = ScriptedAdapter(response(end_call()))
    loop = make_loop(run_dir, adapter, lens=FakeLens(), profile=PROFILE_PLANNING)
    loop.run()

    # Telemetry: roster, balances, plan — all before any model call, in
    # this order, each scaffold-initiated.
    assert [e["tool"] for e in injected(run_dir)] == [BRIEF_TOOL, BALANCE_TOOL, PLAN_TOOL]
    assert [e["source"] for e in injected(run_dir)] == ["lens", "harness", "scaffold"]
    kinds = [e["event"] for e in read_events(run_dir / "telemetry.jsonl")]
    assert kinds[: kinds.index("llm_call")].count("tool_call") == 3

    # Context: three completed call/result pairs after the kickoff.
    first = adapter.requests[0]["messages"]
    assert [type(m) for m in first[1:7]] == [
        AssistantMessage,
        ToolResultMessage,
        AssistantMessage,
        ToolResultMessage,
        AssistantMessage,
        ToolResultMessage,
    ]
    assert [c.name for m in first[1:7:2] for c in m.tool_calls] == [
        BRIEF_TOOL,
        BALANCE_TOOL,
        PLAN_TOOL,
    ]
    for event in read_events(run_dir / "telemetry.jsonl"):
        validate_event(event)


def test_call_seq_covers_the_injections_in_order(run_dir):
    loop = make_loop(run_dir, ScriptedAdapter(response(end_call())), lens=FakeLens())
    loop.run()
    rows = tool_events(run_dir)
    assert [r["call_seq"] for r in rows] == list(range(1, len(rows) + 1))
    assert [r["tool"] for r in rows[:2]] == [BRIEF_TOOL, BALANCE_TOOL]


# --- gas balances (item 2) ----------------------------------------------------


def test_balances_reach_the_model_verbatim(run_dir):
    adapter = ScriptedAdapter(response(end_call()))
    loop = make_loop(run_dir, adapter)
    loop.run()
    results = [m for m in adapter.requests[0]["messages"] if isinstance(m, ToolResultMessage)]
    assert results[0].content == BALANCES_JSON
    assert not results[0].is_error
    row = injected(run_dir)[0]
    assert row["tool"] == BALANCE_TOOL
    assert row["source"] == "harness"
    assert row["ok"] is True
    assert "error" not in row


def test_the_balance_call_sends_no_account_argument(run_dir):
    """The scaffold cannot know which account a run is (D7's argument)."""
    game = Game()
    loop = make_loop(run_dir, ScriptedAdapter(response(end_call())), game=game)
    loop.run()
    assert game.calls[0] == (BALANCE_TOOL, {})


def test_balances_are_injected_on_every_profile(run_dir):
    for profile in (PROFILE_CONTROL, PROFILE_SEARCH, PROFILE_PLANNING):
        directory = run_dir / profile
        (directory / "reference").mkdir(parents=True)
        loop = make_loop(directory, ScriptedAdapter(response(end_call())), profile=profile)
        loop.run()
        assert [e["tool"] for e in injected(directory)][:1] == [BALANCE_TOOL]


def test_a_surface_without_the_balance_tool_degrades_visibly(run_dir):
    """A mis-pinned surface says so every session — it does not go quiet."""
    adapter = ScriptedAdapter(response(end_call()))
    loop = make_loop(run_dir, adapter, game=Game(tool_defs=[OTHER_DEF]))
    loop.run()
    results = [m for m in adapter.requests[0]["messages"] if isinstance(m, ToolResultMessage)]
    assert results[0].is_error
    assert results[0].content == f"unknown tool: {BALANCE_TOOL}"
    row = injected(run_dir)[0]
    assert row["ok"] is False
    # The scaffold layer is what rejects an absent name (X15).
    assert row["source"] == "scaffold"
    assert row["error"] == f"unknown tool: {BALANCE_TOOL}"


def test_a_failing_balance_call_is_injected_as_the_harness_own_words(run_dir):
    message = "RPC error: could not read balance for 0xe1"
    adapter = ScriptedAdapter(response(end_call()))
    loop = make_loop(run_dir, adapter, game=Game(raises=ToolError(message)))
    loop.run()
    results = [m for m in adapter.requests[0]["messages"] if isinstance(m, ToolResultMessage)]
    assert results[0].content == message
    assert results[0].is_error
    assert injected(run_dir)[0]["error"] == message


def test_the_balance_call_is_attempted_exactly_once(run_dir):
    game = Game(raises=ToolError("boom"))
    loop = make_loop(run_dir, ScriptedAdapter(response(end_call())), game=game)
    loop.run()
    assert game.calls == [(BALANCE_TOOL, {})]


def test_no_harness_means_no_balance_injection_and_no_telemetry(run_dir):
    """With no surface there is nothing to ask, so nothing is recorded."""
    loop = make_loop(run_dir, ScriptedAdapter(response(end_call())), game=None)
    loop.run()
    assert injected(run_dir) == []


def test_balances_bound_nothing_the_agent_does(run_dir):
    """No session_tool_cap, no error counter, no repetition breaker (X20)."""
    calls = [ToolCall(id=f"t{i}", name="lens_node", args={}) for i in range(3)]
    adapter = ScriptedAdapter(response(*calls), response(end_call()))
    # A cap of 3 must still admit three agent calls after the injection.
    loop = make_loop(run_dir, adapter, session_tool_cap=3)
    result = loop.run()
    assert result.reason == "tool_cap"
    assert len([e for e in tool_events(run_dir) if e["initiator"] == "model"]) == 3


def test_a_failed_balance_call_does_not_advance_the_error_counter(run_dir):
    """One failed injection plus one failed agent call is one error, not two."""
    adapter = ScriptedAdapter(
        response(ToolCall(id="a", name="lens_node", args={})),
        response(end_call()),
    )
    loop = make_loop(
        run_dir,
        adapter,
        game=Game(tool_defs=[OTHER_DEF]),  # no balance tool: the injection fails
        max_consecutive_errors=2,
    )
    result = loop.run()
    # The agent's own call succeeded, so the session ended on its terms.
    assert result.reason == "agent"


# --- the plan file (item 4) ---------------------------------------------------


def test_the_plan_file_is_injected_only_on_the_planning_profile(run_dir):
    for profile in (PROFILE_CONTROL, PROFILE_SEARCH):
        directory = run_dir / profile
        (directory / "reference").mkdir(parents=True)
        loop = make_loop(directory, ScriptedAdapter(response(end_call())), profile=profile)
        loop.run()
        assert PLAN_TOOL not in [e["tool"] for e in injected(directory)]


def test_the_plan_file_is_read_through_the_normal_tool_and_recorded_by_path(run_dir):
    (run_dir / "workspace").mkdir(exist_ok=True)
    (run_dir / "workspace" / PLAN_PATH).write_text("1. quests\n2. level up\n", encoding="utf-8")
    adapter = ScriptedAdapter(response(end_call()))
    loop = make_loop(run_dir, adapter, profile=PROFILE_PLANNING)
    loop.run()
    row = [e for e in injected(run_dir) if e["tool"] == PLAN_TOOL][0]
    assert row["source"] == "scaffold"
    assert row["path"] == PLAN_PATH
    assert row["ok"] is True
    results = [m for m in adapter.requests[0]["messages"] if isinstance(m, ToolResultMessage)]
    assert results[-1].content == "1. quests\n2. level up\n"


def test_a_missing_plan_file_is_the_normal_not_found_error(run_dir):
    adapter = ScriptedAdapter(response(end_call()))
    loop = make_loop(run_dir, adapter, profile=PROFILE_PLANNING)
    loop.run()
    row = [e for e in injected(run_dir) if e["tool"] == PLAN_TOOL][0]
    assert row["ok"] is False
    assert row["error"] == f"no such file: {PLAN_PATH!r}"
    results = [m for m in adapter.requests[0]["messages"] if isinstance(m, ToolResultMessage)]
    assert results[-1].is_error


def test_a_large_plan_file_is_capped_like_any_tool_result(run_dir):
    (run_dir / "workspace").mkdir(exist_ok=True)
    (run_dir / "workspace" / PLAN_PATH).write_text("x" * 5000, encoding="utf-8")
    adapter = ScriptedAdapter(response(end_call()))
    loop = make_loop(run_dir, adapter, profile=PROFILE_PLANNING, tool_result_max_bytes=1000)
    loop.run()
    row = [e for e in injected(run_dir) if e["tool"] == PLAN_TOOL][0]
    assert row["truncated"] is True
    assert row["original_bytes"] == 5000
    results = [m for m in adapter.requests[0]["messages"] if isinstance(m, ToolResultMessage)]
    # The re-read hint names the path, so the agent can page the rest (I16).
    assert PLAN_PATH in results[-1].content


def test_the_scaffold_never_creates_the_plan_file(run_dir):
    """It is the agent's file: absent stays absent (P11)."""
    loop = make_loop(run_dir, ScriptedAdapter(response(end_call())), profile=PROFILE_PLANNING)
    loop.run()
    assert not (run_dir / "workspace" / PLAN_PATH).exists()


def test_the_plan_injection_bounds_nothing_the_agent_does(run_dir):
    calls = [ToolCall(id=f"t{i}", name="lens_node", args={}) for i in range(2)]
    adapter = ScriptedAdapter(response(*calls), response(end_call()))
    loop = make_loop(run_dir, adapter, profile=PROFILE_PLANNING, session_tool_cap=2)
    result = loop.run()
    assert result.reason == "tool_cap"
    assert len([e for e in tool_events(run_dir) if e["initiator"] == "model"]) == 2
