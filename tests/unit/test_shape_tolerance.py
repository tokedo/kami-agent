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

kami-harness 4.1.0 / 4.2.0 and kami-lens 1.0.1 / 1.0.2 add more, on the
same terms (the last section of this file):

- every write result carries ``fee_wei`` — a string, or null where the
  receipt does not carry both gas-token legs (always null on a reverted
  row);
- harvest stops and collects, alone or as a sequence step, carry
  ``payouts``, one row per kami, with ``decode_error`` where an item or
  amount cannot be stated;
- ``portal_claim`` states its amount only when the payout is proven, and
  otherwise carries ``decode_error`` and no ``amount``;
- a lens ``inventory`` row may carry ``unregistered: true``;
- the lens ``status`` answer's ``sync`` gains ``reconcileRepairs`` and,
  once there has been one, ``lastRepair``;
- ``meta.asOf.clockSampleAgoMs`` may run past 300,000 on a healthy daemon.

A ``decode_error`` is the harness saying what it could not read out of a
transaction that LANDED. It is not a failure of the call, and is recorded
as none.

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
    ToolDef(name="harvest_stop", description="Stop.", input_schema=EMPTY),
    ToolDef(name="portal_claim", description="Claim.", input_schema=EMPTY),
    ToolDef(name="allocate_skills", description="Loop.", input_schema=EMPTY),
    ToolDef(name="lens_inventory", description="Inventory.", input_schema=EMPTY),
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


def run(run_dir, answers, *names, lens=None):
    adapter = Calls(*names)
    config = RunConfig(
        run_dir=Path(run_dir),
        run_id="run-shapes",
        model="test-model",
        prices=PriceTable(input_usd_per_mtok=1.0, output_usd_per_mtok=5.0),
        caps=LoopCaps(session_token_cap=100_000),
        params=SamplingParams(max_tokens=1024),
    )
    outcome = run_session(
        config,
        adapter,
        harness_factory=lambda: Harness(answers),
        lens_factory=(lambda: lens) if lens is not None else None,
    )
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


# =================================================================================
# kami-harness 4.1.0 / 4.2.0 and kami-lens 1.0.1 / 1.0.2
# =================================================================================
#
# Shapes copied from the harness's own result builders at 4.2.0 (_tx_fields,
# _failed_tx_fields, _harvest_payouts, portal_claim / _portal_payout) and from
# the lens's schemas at 1.0.2 (inventory.json, status.json, envelope.ts).

PAYEE = "0x" + "9f" * 20
HOLDER = "0x" + "70" * 20
GAS_TOKEN = "0x" + "e1" * 20

# --- 4.2.0: `fee_wei` and `payouts` on a write result ----------------------------

STOP_WITH_PAYOUTS = {
    "tx_hash": H1,
    "status": "success",
    "block": 34007712,
    "gas_used": 403112,
    "fee_wei": "51732000000000",
    "account": "main",
    "kamis": [1041, 1054, 1077],
    "payouts": [
        {"kami_id": 1041, "item": 2, "item_name": "VIPP", "amount": 515},
        # The game's event states the amount; the item could not be singled out.
        {
            "kami_id": 1054,
            "item": None,
            "item_name": None,
            "amount": 498,
            "decode_error": (
                "the HARVEST_STOP event pays kami #1054's account 498, and "
                "inventory writes of items [2, 103] precede it in the receipt; "
                "the item is not stated"
            ),
        },
        # No event for this kami: neither is stated.
        {
            "kami_id": 1077,
            "item": None,
            "item_name": None,
            "amount": None,
            "decode_error": (
                "no HARVEST_STOP event for kami #1077 in the receipt; the payout is not stated"
            ),
        },
    ],
}
# A landed receipt without both gas-token legs states its fee as null.
COLLECT_FEE_UNSTATED = {
    "tx_hash": H1,
    "status": "success",
    "block": 34007690,
    "gas_used": 210554,
    "fee_wei": None,
    "account": "main",
    "kamis": [1088],
    "payouts": [{"kami_id": 1088, "item": 2, "item_name": "VIPP", "amount": 0}],
}
# A partial loop result whose last leg reverted: the leg and the top level
# both carry fee_wei null (a reverted receipt has no logs, so no fee legs).
ALLOCATE_LEG_REVERTED = {
    "kami_id": 1041,
    "allocated": 1,
    "failed_at": 212,
    "total_planned": 2,
    "error": f"transaction {H2} landed on-chain in block 34007721 and REVERTED",
    "txs": [
        {
            "tx_hash": H1,
            "status": "success",
            "block": 34007720,
            "gas_used": 98211,
            "fee_wei": "12601000000000",
        },
        {
            "tx_hash": H2,
            "status": "reverted",
            "block": 34007721,
            "gas_used": 61002,
            "fee_wei": None,
        },
    ],
    "tx_hash": H2,
    "status": "reverted",
    "block": 34007721,
    "gas_used": 61002,
    "fee_wei": None,
    "chain": {"skill_points": 1},
}
# An act_sequence that landed one step and reverted the next.
SEQUENCE_STOP_THEN_REVERT = {
    "status": "partial",
    "steps": [
        {
            "index": 0,
            "op": "harvest_stop",
            "kami_ids": [1041],
            "status": "success",
            "tx_hash": H1,
            "block": 34007712,
            "gas_used": 201556,
            "fee_wei": "25866000000000",
            "payouts": [{"kami_id": 1041, "item": 2, "item_name": "VIPP", "amount": 515}],
        },
        {
            "index": 1,
            "op": "feed",
            "kami_id": 1041,
            "item_id": 11301,
            "status": "reverted",
            "tx_hash": H2,
            "block": 34007713,
            "gas_used": 48211,
            "fee_wei": None,
        },
    ],
    "sent": 2,
    "landed": 1,
    "account": "main",
}

# --- 4.1.0: portal_claim without a stated amount ----------------------------------

CLAIM_NO_PAYOUT_FOUND = {
    "notice": (
        f"operator-lane receipt: the payout goes to the account's operator as of "
        f"this claim, {PAYEE}, which is not this server's operator wallet (none)"
    ),
    "tx_hash": H1,
    "status": "success",
    "block": 34007730,
    "gas_used": 131070,
    "fee_wei": "16823000000000",
    "receipt_id": "0x1f",
    "route": "operator",
    "item": 103,
    "token": GAS_TOKEN,
    "payee": None,
    "amount_wei": None,
    "decode_error": (
        f"no payout identified in the receipt: no Transfer of the token from the "
        f"portal's token holder {HOLDER} to the payee {PAYEE} among its 2 Transfer "
        "log(s) of the token (gas is paid in this token, so its prepayment and "
        "refund are Transfers of it too, and neither is the payout)"
    ),
}
# The payout was found but disagrees with the receipt: payee stated, no amount.
CLAIM_AMOUNT_DISAGREES = {
    "tx_hash": H1,
    "status": "success",
    "block": 34007731,
    "gas_used": 131070,
    "fee_wei": "16823000000000",
    "receipt_id": "0x20",
    "route": "owner",
    "item": 103,
    "token": GAS_TOKEN,
    "payee": PAYEE,
    "amount_wei": None,
    "decode_error": (
        f"the payout Transfer to {PAYEE} is 90000000000000 wei but the receipt's "
        "token amount read before signing is 90000000000001 wei; the amount is "
        "not stated"
    ),
}

# --- lens 1.0.2: an answer's meta, an inventory row, the status answer ------------

# clockSampleAgoMs past 300,000: from 1.0.2 a sample waits for a newer block,
# so on a quiet chain it runs past the 300 s tick on a healthy daemon.
META_102 = {
    "servedAt": "2026-10-04T12:00:00.000Z",
    "blockNumber": 34007720,
    "stale": False,
    "mode": "daemon",
    "reconciledThrough": 34007700,
    "appliedThrough": 34007719,
    "asOf": {
        "block": 34007720,
        "projectedAtSec": 1791115200,
        "clockSampleBlock": 34007600,
        "clockSampleBlockTime": 1791114788,
        "clockOffsetMs": -812,
        "clockSampleAgoMs": 412345,
    },
}
ROSTER_102 = {
    "data": {
        "account": {"index": 7, "roomIndex": 11, "levelUpReady": [], "skillPoints": []},
        "kamis": [{"index": 1041, "state": "HARVESTING", "hp": [48, 60]}],
    },
    "untrusted": [],
    "meta": META_102,
}
INVENTORY_102 = {
    "data": {
        "account": {"index": 7, "name": "shapes"},
        "items": [
            {
                "balance": 412,
                "item": {"id": "0x02", "index": 2, "name": "VIPP", "type": "MISC", "rarity": 1},
            },
            # The stored item index has no registry entry: the index is the
            # stored one, the rest is the reference client's null item.
            {
                "balance": 3,
                "item": {"id": "0", "index": 30017, "name": "None", "type": "", "rarity": 0},
                "unregistered": True,
            },
        ],
    },
    "untrusted": ["data.account.name"],
    "meta": META_102,
}
REPAIR = {
    "block": 34007650,
    "component": "Health",
    "entity": "0x" + "4a" * 32,
    "at": "2026-10-04T11:58:41.000Z",
}


def status_102(sync_extra):
    """A lens 1.0.2 `status` answer; ``sync_extra`` is what 1.0.1 added."""
    sync = {
        "reconnects": 0,
        "gapsHealed": 0,
        "gapsDeferred": 0,
        "reconcilePasses": 41,
        "reconciledThrough": 34007700,
        "lastReconcileAt": "2026-10-04T11:59:30.000Z",
        "unhealedRanges": [],
        "lastHealMs": None,
        "reconcileIntervalMs": 30000,
        "appliedThrough": 34007719,
        "shortReads": 0,
        "olderWritesSkipped": 1873,
        "lastReconcileAdvanceAt": "2026-10-04T11:59:30.000Z",
        **sync_extra,
    }
    return {
        "data": {
            "version": "1.0.2",
            "upstreamPin": "ffda3963",
            "state": "LIVE",
            "config": {
                "chainId": 428962654539583,
                "worldAddress": "0x2729174c265dbBd8416C6449E0E813E88f43D0E7",
                "jsonRpcUrl": "http://127.0.0.1:8545",
                "dataDir": "/srv/lens",
                "enrich": False,
                "defaultOperator": 7,
            },
            "sync": sync,
        },
        "untrusted": [],
        "meta": META_102,
    }


class StatusLens:
    """Answers the scaffold's one operator-side query, `status`."""

    def __init__(self, envelope):
        self.envelope = envelope
        self.queries = []

    def query(self, name, args=None):
        self.queries.append(name)
        assert name == "status", name
        return self.envelope


# --- classification: the 4.1.0 / 4.2.0 shapes are recorded as what they are ------


@pytest.mark.parametrize(
    "result",
    [STOP_WITH_PAYOUTS, COLLECT_FEE_UNSTATED, CLAIM_NO_PAYOUT_FOUND, CLAIM_AMOUNT_DISAGREES],
    ids=["stop-payouts", "collect-fee-null", "claim-no-payout", "claim-amount-disagrees"],
)
def test_a_landed_write_with_fee_payouts_or_decode_error_is_a_confirmed_success(result):
    """`fee_wei` (string or null), `payouts` and `decode_error` change nothing.

    The transaction landed: confirmed, its own hash, not error-shaped (a
    `decode_error` is not an `error`), not counted toward the repetition
    breaker's error streak, and no per-leg evidence invented from `payouts`.
    """
    text = json.dumps(result)
    assert classify_success(text) == CONFIRMED_SUCCESS
    assert _extract_tx_hash(None, text) == H1
    assert not error_shaped_payload(text)
    assert not is_error_or_revert(True, text)
    assert _extract_txs(None, text) == ()


def test_the_claim_shapes_carry_decode_error_and_no_amount():
    """Guards the fixtures: the 4.1.0 case exercised here and below is a
    claim with NO `amount` and a `decode_error`, which the session tests
    then show reaching the model byte for byte — nothing fills it in."""
    for result in (CLAIM_NO_PAYOUT_FOUND, CLAIM_AMOUNT_DISAGREES):
        assert "amount" not in result
        assert result["amount_wei"] is None
        assert "decode_error" in result


def test_a_reverted_leg_with_a_null_fee_is_copied_verbatim():
    text = json.dumps(ALLOCATE_LEG_REVERTED)
    legs = _extract_txs(None, text)
    assert legs == tuple(ALLOCATE_LEG_REVERTED["txs"])
    assert legs[1]["fee_wei"] is None
    assert legs[0]["fee_wei"] == "12601000000000"  # a string, never parsed
    # The call itself reverted: as before 4.2.0, null fee or not.
    assert classify_success(text) is None
    assert error_shaped_payload(text)
    assert is_error_or_revert(True, text)


def test_a_sequence_with_payouts_and_a_null_fee_names_no_single_outcome():
    text = json.dumps(SEQUENCE_STOP_THEN_REVERT)
    assert classify_success(text) is None  # "partial" is no single outcome
    assert _extract_tx_hash(None, text) is None
    assert not error_shaped_payload(text)
    assert not is_error_or_revert(True, text)


# --- through a session: verbatim to the model, recorded as what it is --------------


@pytest.mark.parametrize(
    ("tool", "result"),
    [
        ("harvest_stop", STOP_WITH_PAYOUTS),
        ("harvest_collect", COLLECT_FEE_UNSTATED),
        ("portal_claim", CLAIM_NO_PAYOUT_FOUND),
        ("portal_claim", CLAIM_AMOUNT_DISAGREES),
    ],
    ids=["stop-payouts", "collect-fee-null", "claim-no-payout", "claim-amount-disagrees"],
)
def test_fee_payouts_and_decode_error_reach_the_model_untouched(run_dir, tool, result):
    _, adapter, events = run(run_dir, {tool: result}, tool)
    seen = results_seen(adapter)["c0"]
    assert seen.content == json.dumps(result, ensure_ascii=False)
    assert seen.is_error is False
    assert list(json.loads(seen.content)) == list(result)  # key order kept, notice first
    row = next(e for e in events if e["event"] == "tool_call" and e["tool"] == tool)
    assert row["ok"] is True
    assert row["tx_terminal_state"] == CONFIRMED_SUCCESS
    assert row["tx_hash"] == H1
    assert "result_error_shaped" not in row
    assert "txs" not in row
    (entry,) = journal.read_entries(run_dir)
    assert entry["tx_hashes"] == [H1]


def test_a_reverted_leg_with_a_null_fee_is_recorded_as_the_harness_wrote_it(run_dir):
    _, adapter, events = run(run_dir, {"allocate_skills": ALLOCATE_LEG_REVERTED}, "allocate_skills")
    seen = results_seen(adapter)["c0"]
    assert seen.content == json.dumps(ALLOCATE_LEG_REVERTED, ensure_ascii=False)
    row = next(e for e in events if e["event"] == "tool_call" and e["tool"] == "allocate_skills")
    assert row["ok"] is True  # exception-keyed: nothing raised
    assert row["result_error_shaped"] is True
    assert row["txs"] == ALLOCATE_LEG_REVERTED["txs"]  # fee_wei null kept, not dropped
    assert "tx_terminal_state" not in row


def test_a_sequence_with_payouts_and_a_null_fee_reaches_the_model_untouched(run_dir):
    _, adapter, events = run(run_dir, {"act_sequence": SEQUENCE_STOP_THEN_REVERT}, "act_sequence")
    seen = results_seen(adapter)["c0"]
    assert seen.content == json.dumps(SEQUENCE_STOP_THEN_REVERT, ensure_ascii=False)
    row = next(e for e in events if e["event"] == "tool_call" and e["tool"] == "act_sequence")
    assert row["ok"] is True
    assert "tx_terminal_state" not in row
    assert "result_error_shaped" not in row


def test_an_unregistered_inventory_row_reaches_the_model_untouched(run_dir):
    _, adapter, events = run(run_dir, {"lens_inventory": INVENTORY_102}, "lens_inventory")
    seen = results_seen(adapter)["c0"]
    assert seen.content == json.dumps(INVENTORY_102, ensure_ascii=False)
    assert seen.is_error is False
    row = next(e for e in events if e["event"] == "tool_call" and e["tool"] == "lens_inventory")
    assert row["ok"] is True
    assert "result_error_shaped" not in row
    assert "tx_terminal_state" not in row


def test_a_late_clock_sample_is_no_staleness_the_daemon_did_not_state(run_dir):
    """clockSampleAgoMs past 300 s is recorded as nothing: `lens_stale` is meta.stale.

    The brief still reaches the model verbatim, its freshness fields are
    the daemon's own, and the journal keeps the roster as served.
    """
    outcome, adapter, events = run(run_dir, {BRIEF_TOOL: ROSTER_102})
    assert outcome == SESSION_RAN
    brief = results_seen(adapter)["brief_1"]
    assert brief.content == json.dumps(ROSTER_102, ensure_ascii=False)
    assert brief.is_error is False
    row = next(e for e in events if e["event"] == "tool_call" and e["tool"] == BRIEF_TOOL)
    assert row["ok"] is True
    assert row["lens_block"] == META_102["blockNumber"]
    assert row["lens_stale"] is False
    (entry,) = journal.read_entries(run_dir)
    assert entry["roster"] == ROSTER_102["data"]


@pytest.mark.parametrize(
    "sync_extra",
    [{"reconcileRepairs": 0}, {"reconcileRepairs": 2, "lastRepair": REPAIR}],
    ids=["no-repair-yet", "after-a-repair"],
)
def test_a_status_with_repair_counters_records_the_same_identity(run_dir, sync_extra):
    """The provenance read takes the daemon's identity and nothing from `sync`."""
    lens = StatusLens(status_102(sync_extra))
    outcome, adapter, events = run(run_dir, {}, "lens_kami", lens=lens)
    assert outcome == SESSION_RAN
    assert lens.queries == ["status"]
    start = next(e for e in events if e["event"] == "session_start")
    assert {k: v for k, v in start.items() if k.startswith("lens_")} == {
        "lens_version": "1.0.2",
        "lens_upstream_pin": "ffda3963",
        "lens_enrich": False,
        "lens_default_operator": "7",
    }
    # Operator-side only: nothing of the status answer reaches the model.
    shown = json.dumps([getattr(m, "content", None) for m in adapter.requests[-1]], default=str)
    assert "reconcileRepairs" not in shown
    assert "ffda3963" not in shown
