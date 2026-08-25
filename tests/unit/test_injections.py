"""Session-start injections beyond the brief: gas balances and the plan file.

SPEC P1.12 (the three injections and their order), D1 (the balance-source
coupling), X10 (ETH is a world resource, not the apparatus), X20/X21 (they
bound nothing and degrade visibly), P9 (their telemetry).
"""

import json
from pathlib import Path

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
    BALANCE_CALL_ID,
    BALANCE_TOOL,
    BRIEF_TOOL,
    JOURNAL_TOOL,
    PLAN_PATH,
    PLAN_TOOL,
    AgentLoop,
    GameToolResult,
    LoopCaps,
)
from kami_agent.telemetry import TelemetryWriter, read_events, validate_event
from kami_agent.tools.errors import ToolError
from kami_agent.tools.scaffold import (
    PLAN_FILE_MAX_BYTES,
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
ROSTER_DEF = ToolDef(
    name=BRIEF_TOOL,
    description="Compact roster: one line per kami plus where the account is.",
    input_schema={
        "type": "object",
        "properties": {"account_index": {"type": "integer", "default": -1}},
    },
)
OTHER_DEF = ToolDef(
    name="lens_node", description="d", input_schema={"type": "object", "properties": {}}
)


class Game:
    """A harness surface carrying both injection tools, as the pinned one does.

    The roster tool is not optional on a fake any more: from 0.6.0 a
    surface without it is refused at loop construction (SPEC D1), so a
    test that wants a DEGRADED balance injection removes the balance tool
    and keeps this one.
    """

    def __init__(self, tool_defs=None, raises=None):
        self.tool_defs = (
            [OTHER_DEF, BALANCE_DEF, ROSTER_DEF] if tool_defs is None else [*tool_defs, ROSTER_DEF]
        )
        self.calls = []
        self._raises = raises

    def execute(self, name, args):
        self.calls.append((name, args))
        if name == BRIEF_TOOL:
            return GameToolResult(content=json.dumps(ROSTER_ENVELOPE, ensure_ascii=False))
        if self._raises is not None:
            raise self._raises
        if name == BALANCE_TOOL:
            return GameToolResult(content=BALANCES_JSON)
        return GameToolResult(content=json.dumps({"ok": True, "tool": name}))


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
        telemetry=TelemetryWriter(run_dir / "telemetry.jsonl", run_id="test-run"),
        session=session,
        params=PARAMS,
        prices=PRICES,
        caps=caps,
        sleep=lambda s: None,
    )


def tool_events(run_dir):
    return [e for e in read_events(run_dir / "telemetry.jsonl") if e["event"] == "tool_call"]


def balance_row(run_dir):
    """The balance injection's telemetry row, found by its tool name."""
    return [e for e in injected(run_dir) if e["tool"] == BALANCE_TOOL][0]


def balance_result(adapter):
    """The balance injection's result message, found by its call id.

    Two injections come off the harness from 0.6.0 (the roster and the
    balances), so position no longer identifies either.
    """
    return next(
        m
        for m in adapter.requests[0]["messages"]
        if isinstance(m, ToolResultMessage) and m.tool_call_id == BALANCE_CALL_ID
    )


def plan_row(run_dir):
    """The plan injection's telemetry row, identified by its PATH.

    Two injections are ``workspace_read`` calls from 0.6.0 — the plan file
    and the journal entry — so the tool name no longer identifies either,
    exactly as ``initiator: scaffold`` stopped identifying the brief at
    0.5.0. Split on ``path`` (SPEC P9).
    """
    return [e for e in injected(run_dir) if e.get("path") == PLAN_PATH][0]


def injected(run_dir):
    return [e for e in tool_events(run_dir) if e["initiator"] == "scaffold"]


# --- order and shape (P1.12) --------------------------------------------------


def test_the_four_injections_run_in_order_before_the_first_model_call(run_dir):
    (run_dir / "workspace").mkdir(exist_ok=True)
    (run_dir / "workspace" / PLAN_PATH).write_text("goal: quests\n", encoding="utf-8")
    adapter = ScriptedAdapter(response(end_call()))
    loop = make_loop(run_dir, adapter, profile=PROFILE_PLANNING)
    loop.run()

    # Telemetry: roster, balances, plan, journal — all before any model
    # call, in this order, each scaffold-initiated. The journal read is
    # appended last, so the three that existed at 0.5.0 keep their
    # positions and their call_seq numbers.
    assert [e["tool"] for e in injected(run_dir)] == [
        BRIEF_TOOL,
        BALANCE_TOOL,
        PLAN_TOOL,
        JOURNAL_TOOL,
    ]
    assert [e["source"] for e in injected(run_dir)] == [
        # The roster moved onto the harness surface at 0.6.0, so the first
        # two injections now share a source and only `tool` tells them
        # apart (SPEC P9, D1).
        "harness",
        "harness",
        "scaffold",
        "scaffold",
    ]
    kinds = [e["event"] for e in read_events(run_dir / "telemetry.jsonl")]
    assert kinds[: kinds.index("llm_call")].count("tool_call") == 4

    # Context: four completed call/result pairs after the kickoff.
    first = adapter.requests[0]["messages"]
    assert [type(m) for m in first[1:9]] == [
        AssistantMessage,
        ToolResultMessage,
        AssistantMessage,
        ToolResultMessage,
        AssistantMessage,
        ToolResultMessage,
        AssistantMessage,
        ToolResultMessage,
    ]
    assert [c.name for m in first[1:9:2] for c in m.tool_calls] == [
        BRIEF_TOOL,
        BALANCE_TOOL,
        PLAN_TOOL,
        JOURNAL_TOOL,
    ]
    # Both halves of every injected pair are marked as the scaffold's, and
    # nothing sent to a provider carries the mark (P12).
    assert all(m.initiator == "scaffold" for m in first[1:9])
    for event in read_events(run_dir / "telemetry.jsonl"):
        validate_event(event)


def test_call_seq_covers_the_injections_in_order(run_dir):
    loop = make_loop(run_dir, ScriptedAdapter(response(end_call())))
    loop.run()
    rows = tool_events(run_dir)
    assert [r["call_seq"] for r in rows] == list(range(1, len(rows) + 1))
    assert [r["tool"] for r in rows[:2]] == [BRIEF_TOOL, BALANCE_TOOL]


# --- gas balances (item 2) ----------------------------------------------------


def test_balances_reach_the_model_verbatim(run_dir):
    adapter = ScriptedAdapter(response(end_call()))
    loop = make_loop(run_dir, adapter)
    loop.run()
    assert balance_result(adapter).content == BALANCES_JSON
    assert not balance_result(adapter).is_error
    row = balance_row(run_dir)
    assert row["tool"] == BALANCE_TOOL
    assert row["source"] == "harness"
    assert row["ok"] is True
    assert "error" not in row


def test_the_balance_call_sends_no_account_argument(run_dir):
    """The scaffold cannot know which account a run is (D7's argument)."""
    game = Game()
    loop = make_loop(run_dir, ScriptedAdapter(response(end_call())), game=game)
    loop.run()
    assert (BALANCE_TOOL, {}) in game.calls


def test_balances_are_injected_on_every_profile(run_dir):
    for profile in (PROFILE_CONTROL, PROFILE_SEARCH, PROFILE_PLANNING):
        directory = run_dir / profile
        (directory / "reference").mkdir(parents=True)
        loop = make_loop(directory, ScriptedAdapter(response(end_call())), profile=profile)
        loop.run()
        assert BALANCE_TOOL in [e["tool"] for e in injected(directory)]


def test_a_surface_without_the_balance_tool_degrades_visibly(run_dir):
    """A mis-pinned surface says so every session — it does not go quiet."""
    adapter = ScriptedAdapter(response(end_call()))
    loop = make_loop(run_dir, adapter, game=Game(tool_defs=[OTHER_DEF]))
    loop.run()
    assert balance_result(adapter).is_error
    assert balance_result(adapter).content == f"unknown tool: {BALANCE_TOOL}"
    row = balance_row(run_dir)
    assert row["ok"] is False
    # The scaffold layer is what rejects an absent name (X15).
    assert row["source"] == "scaffold"
    assert row["error"] == f"unknown tool: {BALANCE_TOOL}"


def test_a_failing_balance_call_is_injected_as_the_harness_own_words(run_dir):
    message = "RPC error: could not read balance for 0xe1"
    adapter = ScriptedAdapter(response(end_call()))
    loop = make_loop(run_dir, adapter, game=Game(raises=ToolError(message)))
    loop.run()
    assert balance_result(adapter).content == message
    assert balance_result(adapter).is_error
    assert balance_row(run_dir)["error"] == message


def test_the_balance_call_is_attempted_exactly_once(run_dir):
    game = Game(raises=ToolError("boom"))
    loop = make_loop(run_dir, ScriptedAdapter(response(end_call())), game=game)
    loop.run()
    assert [c for c in game.calls if c[0] == BALANCE_TOOL] == [(BALANCE_TOOL, {})]


def test_no_harness_means_no_balance_injection_and_no_telemetry(run_dir):
    """With no surface there is nothing to ask, so nothing is recorded."""
    loop = make_loop(run_dir, ScriptedAdapter(response(end_call())), game=None)
    loop.run()
    # The journal read survives: it needs neither a daemon nor a harness.
    assert [e["tool"] for e in injected(run_dir)] == [JOURNAL_TOOL]


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
        # The journal read is a workspace_read too, so the plan read is
        # identified by its path rather than by its tool name here.
        assert PLAN_PATH not in [e.get("path") for e in injected(directory)]


def test_the_plan_file_is_read_through_the_normal_tool_and_recorded_by_path(run_dir):
    (run_dir / "workspace").mkdir(exist_ok=True)
    (run_dir / "workspace" / PLAN_PATH).write_text("1. quests\n2. level up\n", encoding="utf-8")
    adapter = ScriptedAdapter(response(end_call()))
    loop = make_loop(run_dir, adapter, profile=PROFILE_PLANNING)
    loop.run()
    row = plan_row(run_dir)
    assert row["source"] == "scaffold"
    assert row["path"] == PLAN_PATH
    assert row["ok"] is True
    results = [m for m in adapter.requests[0]["messages"] if isinstance(m, ToolResultMessage)]
    # The journal read is injected after the plan, so the plan's result is
    # the second from the end.
    assert results[-2].content == "1. quests\n2. level up\n"


def test_a_missing_plan_file_is_the_normal_not_found_error(run_dir):
    adapter = ScriptedAdapter(response(end_call()))
    loop = make_loop(run_dir, adapter, profile=PROFILE_PLANNING)
    loop.run()
    row = plan_row(run_dir)
    assert row["ok"] is False
    assert row["error"] == f"no such file: {PLAN_PATH!r}"
    results = [m for m in adapter.requests[0]["messages"] if isinstance(m, ToolResultMessage)]
    assert results[-1].is_error


def test_the_plan_file_is_capped_at_its_own_smaller_bound(run_dir):
    """The plan injection has its OWN cap, below tool_result_max_bytes.

    The plan is re-sent on every call of every session, so before 0.6.0 an
    agent could grow it to the full 64 KiB tool-result cap and pay for
    that on every call for the rest of the run — the one term of the fixed
    floor the operator cannot size (D1). PLAN_FILE_MAX_BYTES bounds it,
    and the number is stated in prompts/planning.txt so the bound is a
    known mechanism rather than a surprise.
    """
    (run_dir / "workspace").mkdir(exist_ok=True)
    oversize = PLAN_FILE_MAX_BYTES + 500
    (run_dir / "workspace" / PLAN_PATH).write_text("x" * oversize, encoding="utf-8")
    adapter = ScriptedAdapter(response(end_call()))
    # A tool-result cap far ABOVE the plan cap: this must still truncate,
    # which is exactly what proves the plan is not capped by that knob.
    loop = make_loop(
        run_dir, adapter, profile=PROFILE_PLANNING, tool_result_max_bytes=10 * oversize
    )
    loop.run()
    row = plan_row(run_dir)
    assert row["truncated"] is True
    assert row["original_bytes"] == oversize
    results = [m for m in adapter.requests[0]["messages"] if isinstance(m, ToolResultMessage)]
    plan_result = results[-2].content
    assert plan_result.startswith("x" * PLAN_FILE_MAX_BYTES)
    assert f"showing the first {PLAN_FILE_MAX_BYTES} bytes of {oversize}" in plan_result
    # The re-read hint names the path, so the agent can page the rest (I16).
    assert PLAN_PATH in plan_result


def test_the_stated_plan_cap_matches_the_code_default(run_dir):
    """prompts/planning.txt states the number; the code owns it (I5).

    The same discipline the wake bounds get: a frozen asset that names a
    number and a constant that sets it cannot be allowed to drift, so the
    asset is asserted to contain the constant's own value.
    """
    asset = (Path(__file__).parents[2] / "prompts" / "planning.txt").read_text(encoding="utf-8")
    assert str(PLAN_FILE_MAX_BYTES) in asset


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
