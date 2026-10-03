"""Result shapes new at kami-harness 4.0.0 / kami-lens 1.0.0: no crash, no mis-render.

The scaffold reads harness and lens results in a handful of places — the
roster brief's freshness fields and journal roster, transaction evidence,
terminal-state classification, the error-shaped flag, the repetition
breaker's error classifier — and passes every result to the model
verbatim. None of that may break on the shapes these versions add, and
none of it may invent meaning for them:

- list rows the mirror could not complete: ``incomplete: true``, no vitals
  (lens 1.0.0), plus new ``meta`` keys (``appliedThrough``,
  ``incompleteRows``, ``reconciledThrough``, ``asOf``);
- the lens errors ``INCOMPLETE`` and ``NOT_APPLIED``, which the harness
  passes through with the daemon's own code;
- loop results cut short by the call box: ``time_boxed: true`` with
  ``remaining``;
- a ``notice`` as the FIRST key of a result;
- the new raised outcomes. A transaction proven NOT executed (nonce
  collision, dropped; a ``dropped`` row) is recorded as its own terminal
  state, ``not_executed`` (schema 0.7.0) — never as a revert or as
  unconfirmed — with its own hash, never the one that consumed its nonce.
  A blocked lane and a cancelled call are no single transaction outcome
  and are recorded as none.

No strategy is involved: the scaffold stays policy-free, and nothing here
tells the agent what to do about any of these.

Message texts are copied from the harness's own exception classes, not
imported, so a reworded harness message fails here instead of silently
changing what is recorded.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kami_agent import journal
from kami_agent.adapters.base import (
    AdapterResponse,
    SamplingParams,
    StopReason,
    ToolCall,
    ToolDef,
    ToolResultMessage,
    Usage,
)
from kami_agent.governor import PriceTable
from kami_agent.harness import _extract_tx_hash, _extract_txs
from kami_agent.loop import BALANCE_TOOL, BRIEF_TOOL, GameToolResult, LoopCaps
from kami_agent.repetition import is_error_or_revert
from kami_agent.runner import SESSION_RAN, RunConfig, run_session
from kami_agent.telemetry import read_events, validate_event
from kami_agent.tools.errors import ToolError
from kami_agent.tools.receipts import (
    BATCH_ERROR,
    CONFIRMED_SUCCESS,
    NOT_EXECUTED,
    UNCONFIRMED,
    classify_error,
    classify_success,
    error_shaped_payload,
    tx_hash_from_error,
)

H1 = "0x" + "a1" * 32
H2 = "0x" + "b2" * 32
OPERATOR = "0x" + "0e" * 20

# --- lens 1.0.0 shapes --------------------------------------------------------

META_100 = {
    "servedAt": "2026-10-03T12:00:00.000Z",
    "blockNumber": 34007708,
    "reconciledThrough": 34007690,
    "appliedThrough": 34007707,
    "stale": False,
    "mode": "daemon",
    "asOf": {"block": 34007708, "projectedAtSec": 1791028800},
    "incompleteRows": 1,
}
ROSTER_100 = {
    "data": {
        "account": {"index": 7, "roomIndex": 11},
        "kamis": [
            {"index": 1041, "state": "HARVESTING", "hp": [48, 60]},
            # The mirror could not complete this kami: identity and state,
            # NO hp — never served as 0/0.
            {"index": 1054, "state": "HARVESTING", "incomplete": True},
        ],
    },
    "untrusted": [],
    "meta": META_100,
}
PARTY_100 = {
    "data": {
        "account": {"index": 7},
        "kamis": [
            {"index": 1041, "state": "RESTING", "hp": {"current": 60, "total": 60}},
            {"index": 1054, "state": "HARVESTING", "incomplete": True},
        ],
        "kamisTotal": 2,
        "kamisServed": 2,
    },
    "untrusted": [],
    "meta": META_100,
}

# The harness's pass-through of the daemon's own error codes.
INCOMPLETE_ERROR = (
    "Error executing tool lens_kami: INCOMPLETE: kami 1054 cannot be projected "
    "completely right now (missing: harvest)"
)
NOT_APPLIED_ERROR = (
    "Error executing tool lens_kami: NOT_APPLIED: the mirror has applied through "
    "block 34007707; waited 5000 ms for block 34007712"
)

# --- harness 4.0.0 raised outcomes (copied from its exception classes) ---------

NONCE_COLLISION = (
    f"Error executing tool feed_kami: transaction {H1} was NOT executed and cannot "
    f"be: its nonce 500 was consumed by {H2} (NOT signed by this harness). This "
    "hash spent no gas. It is a nonce collision, not an unconfirmed transaction."
)
DROPPED = (
    f"Error executing tool feed_kami: transaction {H1} was NOT executed: the node "
    "no longer holds it (two lookups, 1 s apart, on fresh sessions) and its nonce "
    "500 is unconsumed; the nonce was released. This hash spent no gas."
)
LANE_BLOCKED = (
    f"Error executing tool feed_kami: lane blocked behind nonce 499 for {OPERATOR}: "
    "1 transaction(s) signed earlier by this harness are armed behind it and will "
    f"execute when nonce 499 is used: {H2} (nonce 500, use_item_batch step 2). The "
    "gap could not be filled (fill refused). Nothing was sent by this call."
)
CANCELLED = (
    "Error executing tool harvest_collect: harvest_collect: cancelled by the client; "
    "stopped at a step boundary and nothing further was sent"
)
UNCONFIRMED_60S = (
    f"Error executing tool feed_kami: transaction {H1} is UNCONFIRMED: it was "
    "broadcast, but no receipt arrived within 60s. It may still be included and "
    "spend gas later. Check its on-chain status before retrying — a blind retry "
    "can execute the action twice."
)
BATCH_WITH_DROPPED_ROW = (
    "Error executing tool use_item_batch: use_item_batch: 1 of 2 items failed. "
    "Items reported successful below are final on-chain (their gas was spent and "
    "their state changes applied) — do not resubmit them. Per-item outcomes: "
    f'[{{"kami": 1, "status": "success", "tx_hash": "{H1}"}}, '
    f'{{"kami": 2, "status": "dropped", "tx_hash": "{H2}", "consumed_by": null}}]'
)

# --- returned (success-shaped) 4.0.0 results -------------------------------------

TIME_BOXED_LOOP = {
    "notice": (
        f"drained an earlier call's armed transaction {H2} (use_item_batch step 2, "
        "signed 2026-10-03T11:59:01Z): mined late at nonce 500"
    ),
    "time_boxed": True,
    "remaining": [{"kami_id": 3}, {"kami_id": 4}],
    "count": 2,
    "ok": 2,
    "results": [
        {"kami_id": 1, "txs": [{"tx_hash": H1, "status": "success", "block": 9}]},
        {"kami_id": 2, "txs": [{"tx_hash": H2, "status": "success", "block": 9}]},
    ],
}
NOTICE_FIRST_SUCCESS = {
    "notice": f"step 2 (feed) was dropped: the node refused {H2}; steps 3-5 ran after it",
    "status": "success",
    "tx_hash": H1,
    "block": 41,
    "gas_used": 21000,
}


# --- classification: nothing new is mis-recorded --------------------------------


@pytest.mark.parametrize("message", [LANE_BLOCKED, CANCELLED, INCOMPLETE_ERROR, NOT_APPLIED_ERROR])
def test_new_failures_that_are_no_single_outcome_are_no_terminal_state(message):
    """Recorded as no state rather than a wrong one, and naming no transaction."""
    assert classify_error(message) is None
    assert tx_hash_from_error(message) is None


@pytest.mark.parametrize("message", [NONCE_COLLISION, DROPPED])
def test_a_transaction_proven_not_executed_is_its_own_terminal_state(message):
    """Not a revert (it never mined) and not unconfirmed (its outcome is closed).

    The nonce collision is the sharp case — its text says "not an
    unconfirmed transaction", and a looser match would record exactly that.
    Its hash is lifted; the hash that consumed its nonce (H2) never is.
    """
    assert classify_error(message) == NOT_EXECUTED
    assert tx_hash_from_error(message) == H1


def test_the_shortened_receipt_wait_is_still_unconfirmed():
    """4.0.0 waits 60 s instead of 120 s; the outcome is the same one."""
    assert classify_error(UNCONFIRMED_60S) == UNCONFIRMED
    assert tx_hash_from_error(UNCONFIRMED_60S) == H1


def test_a_batch_with_a_dropped_row_is_still_a_batch_error():
    assert classify_error(BATCH_WITH_DROPPED_ROW) == BATCH_ERROR
    assert tx_hash_from_error(BATCH_WITH_DROPPED_ROW) is None


def test_a_time_boxed_loop_result_is_evidence_not_an_error():
    text = json.dumps(TIME_BOXED_LOOP)
    assert classify_success(text) is None  # many transactions, no single outcome
    assert not error_shaped_payload(text)
    assert not is_error_or_revert(True, text)
    assert [t["tx_hash"] for t in _extract_txs(None, text)] == [H1, H2]
    # `remaining` is what was NOT attempted: nothing in it is read as a receipt.
    assert _extract_tx_hash(None, text) is None


def test_a_notice_first_key_changes_no_classification():
    text = json.dumps(NOTICE_FIRST_SUCCESS)
    assert next(iter(json.loads(text))) == "notice"
    assert classify_success(text) == CONFIRMED_SUCCESS
    assert _extract_tx_hash(None, text) == H1
    assert not error_shaped_payload(text)
    assert not is_error_or_revert(True, text)


# --- through a session: verbatim to the model, recorded as what it is -------------

ROSTER_DEF = ToolDef(
    name=BRIEF_TOOL,
    description="Compact roster.",
    input_schema={"type": "object", "properties": {"account_index": {"type": "integer"}}},
)
BALANCE_DEF = ToolDef(
    name=BALANCE_TOOL,
    description="Gas balances.",
    input_schema={"type": "object", "properties": {"account": {"type": "string"}}},
)
EMPTY = {"type": "object", "properties": {}}
SURFACE = [
    ROSTER_DEF,
    BALANCE_DEF,
    ToolDef(name="lens_party", description="Party.", input_schema=EMPTY),
    ToolDef(name="lens_kami", description="One kami.", input_schema=EMPTY),
    ToolDef(name="harvest_collect", description="Loop.", input_schema=EMPTY),
    ToolDef(name="feed_kami", description="One send.", input_schema=EMPTY),
    ToolDef(name="act_sequence", description="Sequence.", input_schema=EMPTY),
]


class Harness:
    """Serves a 4.0.0 / lens 1.0.0-shaped answer per tool; a string result raises."""

    def __init__(self, answers):
        self.tool_defs = SURFACE
        self.answers = answers

    def execute(self, name, args):
        answer = self.answers.get(name, {"ok": True})
        if isinstance(answer, str):
            raise ToolError(answer)
        text = json.dumps(answer, ensure_ascii=False)
        return GameToolResult(
            content=text,
            tx_hash=_extract_tx_hash(None, text),
            terminal_state=classify_success(text),
            txs=_extract_txs(None, text),
        )

    def close(self):
        pass


class Calls:
    """One turn calling the given tools, then end_session."""

    def __init__(self, *names):
        self.names = names
        self.requests = []

    def complete(self, system, messages, tools, params):
        self.requests.append(list(messages))
        if len(self.requests) == 1:
            calls = tuple(ToolCall(id=f"c{i}", name=n, args={}) for i, n in enumerate(self.names))
        else:
            calls = (ToolCall(id="end", name="end_session", args={"reason": "done"}),)
        return AdapterResponse(
            text_blocks=(),
            tool_calls=calls,
            stop_reason=StopReason.TOOL_USE,
            usage=Usage(input_tokens=1000, output_tokens=20),
        )


@pytest.fixture
def run_dir(tmp_path):
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    for name, text in (
        ("system.txt", "You are an agent."),
        ("kickoff.txt", "Session start."),
        ("continue.txt", "Continue."),
    ):
        (prompts / name).write_text(text, encoding="utf-8")
    (tmp_path / "reference").mkdir()
    return tmp_path


def run(run_dir, answers, *names):
    adapter = Calls(*names)
    config = RunConfig(
        run_dir=Path(run_dir),
        run_id="run-shapes",
        model="test-model",
        prices=PriceTable(input_usd_per_mtok=1.0, output_usd_per_mtok=5.0),
        caps=LoopCaps(session_token_cap=100_000),
        params=SamplingParams(max_tokens=1024),
    )
    outcome = run_session(config, adapter, harness_factory=lambda: Harness(answers))
    events = list(read_events(Path(run_dir) / "telemetry.jsonl"))
    for event in events:
        validate_event(event)
    return outcome, adapter, events


def results_seen(adapter):
    """Every tool result the model was shown, by call id."""
    return {m.tool_call_id: m for m in adapter.requests[-1] if isinstance(m, ToolResultMessage)}


def test_a_roster_with_an_incomplete_row_is_injected_verbatim_and_journaled_as_is(run_dir):
    outcome, adapter, events = run(run_dir, {BRIEF_TOOL: ROSTER_100})
    assert outcome == SESSION_RAN
    brief = results_seen(adapter)["brief_1"]
    assert brief.content == json.dumps(ROSTER_100, ensure_ascii=False)
    assert brief.is_error is False
    row = next(e for e in events if e["event"] == "tool_call" and e["tool"] == BRIEF_TOOL)
    assert row["ok"] is True
    assert row["lens_block"] == META_100["blockNumber"]
    assert row["lens_stale"] is False
    # The journal keeps the roster the session opened on exactly as served:
    # the incomplete row stays flagged and gains no invented hp.
    (entry,) = journal.read_entries(run_dir)
    assert entry["roster"] == ROSTER_100["data"]
    assert "hp" not in entry["roster"]["kamis"][1]


def test_a_failed_roster_brief_with_a_lens_code_degrades_visibly(run_dir):
    """A lens error code on the brief is the harness's own words, and the session goes on."""
    outcome, adapter, events = run(run_dir, {BRIEF_TOOL: INCOMPLETE_ERROR})
    assert outcome == SESSION_RAN
    brief = results_seen(adapter)["brief_1"]
    assert brief.content == INCOMPLETE_ERROR
    assert brief.is_error is True
    row = next(e for e in events if e["event"] == "tool_call" and e["tool"] == BRIEF_TOOL)
    assert row["ok"] is False
    assert "lens_block" not in row
    (entry,) = journal.read_entries(run_dir)
    assert "roster" not in entry


def test_incomplete_rows_from_an_agent_read_reach_the_model_untouched(run_dir):
    _, adapter, events = run(run_dir, {"lens_party": PARTY_100}, "lens_party")
    seen = results_seen(adapter)["c0"]
    assert seen.content == json.dumps(PARTY_100, ensure_ascii=False)
    assert seen.is_error is False
    row = next(e for e in events if e["event"] == "tool_call" and e["tool"] == "lens_party")
    assert row["ok"] is True
    assert "result_error_shaped" not in row
    assert "tx_terminal_state" not in row


@pytest.mark.parametrize(
    "message",
    [INCOMPLETE_ERROR, NOT_APPLIED_ERROR, LANE_BLOCKED, CANCELLED],
)
def test_new_raised_failures_reach_the_model_verbatim_and_record_no_outcome(run_dir, message):
    tool = "lens_kami" if "lens_kami" in message else "feed_kami"
    _, adapter, events = run(run_dir, {tool: message}, tool)
    seen = results_seen(adapter)["c0"]
    assert seen.content == message
    assert seen.is_error is True
    row = next(e for e in events if e["event"] == "tool_call" and e["tool"] == tool)
    assert row["ok"] is False
    assert row["error"] == message
    assert "tx_terminal_state" not in row
    assert "tx_hash" not in row


@pytest.mark.parametrize("message", [NONCE_COLLISION, DROPPED])
def test_a_raised_not_executed_transaction_is_recorded_as_one(run_dir, message):
    _, adapter, events = run(run_dir, {"feed_kami": message}, "feed_kami")
    seen = results_seen(adapter)["c0"]
    assert seen.content == message  # the agent still reads the harness's own words
    row = next(e for e in events if e["event"] == "tool_call" and e["tool"] == "feed_kami")
    assert row["ok"] is False
    assert row["tx_terminal_state"] == NOT_EXECUTED
    assert row["tx_hash"] == H1


def test_a_returned_dropped_row_is_recorded_as_not_executed(run_dir):
    dropped = {"tx_hash": H1, "status": "dropped", "nonce": 500, "consumed_by": H2}
    _, adapter, events = run(run_dir, {"feed_kami": dropped}, "feed_kami")
    assert results_seen(adapter)["c0"].content == json.dumps(dropped, ensure_ascii=False)
    row = next(e for e in events if e["event"] == "tool_call" and e["tool"] == "feed_kami")
    assert row["ok"] is True  # exception-keyed: nothing raised
    assert row["tx_terminal_state"] == NOT_EXECUTED
    assert row["tx_hash"] == H1


def test_dropped_rows_in_a_sequence_stay_verbatim_and_name_no_single_state(run_dir):
    sequence = {
        "notice": f"step 2 was dropped: its nonce was consumed by {H2}",
        "txs": [
            {"tx_hash": H2, "status": "success", "block": 9, "gas_used": 21000},
            {"tx_hash": H1, "status": "dropped", "nonce": 501, "consumed_by": H2},
        ],
    }
    _, _, events = run(run_dir, {"act_sequence": sequence}, "act_sequence")
    row = next(e for e in events if e["event"] == "tool_call" and e["tool"] == "act_sequence")
    assert row["txs"] == sequence["txs"]  # the harness's own row vocabulary, verbatim
    assert "tx_terminal_state" not in row


def test_a_time_boxed_loop_result_is_recorded_as_the_transactions_it_carries(run_dir):
    _, adapter, events = run(run_dir, {"harvest_collect": TIME_BOXED_LOOP}, "harvest_collect")
    seen = results_seen(adapter)["c0"]
    assert seen.content == json.dumps(TIME_BOXED_LOOP, ensure_ascii=False)
    assert list(json.loads(seen.content)) == list(TIME_BOXED_LOOP)  # notice stays first
    row = next(e for e in events if e["event"] == "tool_call" and e["tool"] == "harvest_collect")
    assert row["ok"] is True
    assert [t["tx_hash"] for t in row["txs"]] == [H1, H2]
    assert "tx_terminal_state" not in row
    assert "result_error_shaped" not in row
    (entry,) = journal.read_entries(run_dir)
    assert entry["tx_hashes"] == [H1, H2]


def test_a_notice_first_single_send_is_still_its_confirmed_outcome(run_dir):
    _, adapter, events = run(run_dir, {"feed_kami": NOTICE_FIRST_SUCCESS}, "feed_kami")
    seen = results_seen(adapter)["c0"]
    assert seen.content == json.dumps(NOTICE_FIRST_SUCCESS, ensure_ascii=False)
    row = next(e for e in events if e["event"] == "tool_call" and e["tool"] == "feed_kami")
    assert row["tx_terminal_state"] == CONFIRMED_SUCCESS
    assert row["tx_hash"] == H1
