"""Receipt-status classification: three terminal states, kept distinct (SPEC D1, P9).

The harness reports a submitted transaction as exactly one of
confirmed-success, confirmed-revert, or unconfirmed, and rejects some
calls before signing at all. These tests pin that the scaffold records
which one occurred as a field, and — the load-bearing half — that it
never edits the message the agent reads in order to do so.
"""

import json

import pytest

from kami_agent.tools import receipts

REVERT = (
    "transaction 0xbadbeef landed on-chain in block 77 and REVERTED: gas was "
    "spent (91234 gas) and no state change was applied. Revert reason "
    "(best-effort eth_call replay at block 77): insufficient stamina"
)
UNCONFIRMED = (
    "transaction 0xfeed is UNCONFIRMED: it was broadcast, but no receipt "
    "arrived within 120s. It may still be included and spend gas later."
)
REJECTED = "validation failed; no transaction sent: kami 42 is RESTING, not HARVESTING"
BATCH = (
    "stop_harvest_batch: 1 of 2 items failed. Items reported successful below "
    "are final on-chain (their gas was spent and their state changes applied) "
    '— do not resubmit them. Per-item outcomes: [{"kami": 2, "status": "reverted"}]'
)

# kami-harness 4.0.0: a broadcast transaction PROVEN never to execute.
# Copied from its TxNonceCollisionError / TxDroppedError, not imported.
NONCE_COLLISION = (
    "transaction 0xa11ce was NOT executed and cannot be: its nonce 500 was "
    "consumed by 0xb0b (NOT signed by this harness). This hash spent no gas. "
    "It is a nonce collision, not an unconfirmed transaction."
)
DROPPED = (
    "transaction 0xd0d0 was NOT executed: the node no longer holds it (two "
    "lookups agree) and its nonce 501 is unconsumed; the nonce was released. "
    "This hash spent no gas."
)

# The MCP server wraps a raised tool exception before the client sees it,
# so no marker can be assumed to sit at position 0.
WRAPPED = "Error executing tool harvest_stop: "


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (REVERT, receipts.REVERTED),
        (UNCONFIRMED, receipts.UNCONFIRMED),
        (REJECTED, receipts.VALIDATION_REJECTED),
        (BATCH, receipts.BATCH_ERROR),
        (NONCE_COLLISION, receipts.NOT_EXECUTED),
        (DROPPED, receipts.NOT_EXECUTED),
    ],
)
def test_each_terminal_state_classifies_distinctly(message, expected):
    assert receipts.classify_error(message) == expected
    assert receipts.classify_error(WRAPPED + message) == expected


def test_batch_wins_over_the_item_states_it_quotes():
    """A batch message embeds per-item outcomes; it must not read as one of them."""
    embedded = BATCH + " " + REVERT + " " + UNCONFIRMED
    assert receipts.classify_error(embedded) == receipts.BATCH_ERROR


def test_non_transaction_errors_classify_as_nothing():
    """Absence means 'not one terminal state', so it must not be guessable."""
    for message in (
        "harvest_stop is not available",
        "tool call timed out after 120 seconds",
        "unknown tool: nope",
        "CHAT_DISABLED",
        "",
    ):
        assert receipts.classify_error(message) is None


def test_confirmed_success_from_a_returned_receipt():
    content = json.dumps({"tx_hash": "0xc0ffee", "status": "success", "block": 41})
    assert receipts.classify_success(content) == receipts.CONFIRMED_SUCCESS
    nested = json.dumps({"result": {"tx_hash": "0xc0ffee", "status": "success"}})
    assert receipts.classify_success(nested) == receipts.CONFIRMED_SUCCESS


def test_in_band_partial_and_skip_results_are_not_a_terminal_state():
    """allow_partial batches and dry-run skips have no single outcome to name."""
    partial = json.dumps({"results": [{"status": "success"}, {"status": "reverted"}]})
    assert receipts.classify_success(partial) is None
    skipped = json.dumps({"status": "skipped", "reason": "dry-run reverted"})
    assert receipts.classify_success(skipped) is None
    assert receipts.classify_success("plain text, not json") is None


# --- the hash a raised terminal state names in its prose (P9, 0.4.0) ----------


def test_raised_revert_and_unconfirmed_yield_their_transaction_hash():
    """The hash used to survive only inside the error text, which P9 tells
    readers never to parse. It is lifted onto the field at ingestion."""
    assert receipts.tx_hash_from_error(REVERT) == "0xbadbeef"
    assert receipts.tx_hash_from_error(UNCONFIRMED) == "0xfeed"
    # And through the MCP server's own error wrapping.
    assert receipts.tx_hash_from_error(WRAPPED + REVERT) == "0xbadbeef"


def test_a_batch_error_yields_no_single_hash():
    """A batch has no single transaction; inventing one would be worse than none."""
    assert receipts.tx_hash_from_error(BATCH) is None
    # Even when the batch message quotes single-transaction outcomes inside it.
    assert receipts.tx_hash_from_error(BATCH + " " + REVERT) is None


def test_a_pre_signing_rejection_has_no_hash_to_report():
    assert receipts.tx_hash_from_error(REJECTED) is None


# --- a transaction proven NOT executed (kami-harness 4.0.0, schema 0.7.0) ------


def test_not_executed_is_neither_unconfirmed_nor_reverted():
    """The nonce collision says "not an unconfirmed transaction": it must not
    be recorded as one, and nothing about it is a revert (no block, no gas)."""
    for message in (NONCE_COLLISION, DROPPED, WRAPPED + NONCE_COLLISION):
        state = receipts.classify_error(message)
        assert state == receipts.NOT_EXECUTED
        assert state not in (receipts.UNCONFIRMED, receipts.REVERTED)


def test_not_executed_lifts_its_own_hash_and_never_the_one_that_consumed_its_nonce():
    assert receipts.tx_hash_from_error(NONCE_COLLISION) == "0xa11ce"
    assert receipts.tx_hash_from_error(WRAPPED + DROPPED) == "0xd0d0"


def test_a_batch_quoting_a_not_executed_item_is_still_a_batch():
    assert receipts.classify_error(BATCH + " " + NONCE_COLLISION) == receipts.BATCH_ERROR
    assert receipts.tx_hash_from_error(BATCH + " " + NONCE_COLLISION) is None


def test_a_returned_dropped_row_is_not_executed():
    """The harness's per-row word for the same verdict, as a single result."""
    row = {"tx_hash": "0xd0d0", "status": "dropped", "nonce": 501, "consumed_by": None}
    assert receipts.classify_success(json.dumps(row)) == receipts.NOT_EXECUTED
    nested = json.dumps({"result": row})
    assert receipts.classify_success(nested) == receipts.NOT_EXECUTED


def test_dropped_rows_inside_a_multi_transaction_payload_are_no_single_state():
    """Rows in a txs list stay verbatim; the call as a whole is not one outcome."""
    payload = json.dumps(
        {"txs": [{"tx_hash": "0x1", "status": "success"}, {"tx_hash": "0x2", "status": "dropped"}]}
    )
    assert receipts.classify_success(payload) is None


def test_an_odd_status_value_classifies_as_nothing_and_never_raises():
    for status in (["success"], {"a": 1}, 1, None, "SUCCESS", "reverted"):
        assert receipts.classify_success(json.dumps({"status": status})) is None


def test_the_enum_is_the_documented_six():
    assert receipts.TERMINAL_STATES == (
        "confirmed_success",
        "reverted",
        "unconfirmed",
        "validation_rejected",
        "batch_error",
        "not_executed",
    )


def test_non_transaction_errors_yield_no_hash():
    for message in ("boom", "tool call timed out after 120 seconds", ""):
        assert receipts.tx_hash_from_error(message) is None


def test_a_hash_quoted_outside_the_contract_clause_is_not_read_as_the_transaction():
    """Only the harness's own opening phrasing counts as naming THE transaction."""
    assert receipts.tx_hash_from_error("see transaction 0xdead for context") is None


# --- results that RETURN their failure (P9 result_error_shaped, 0.4.0) --------


def test_error_shaped_payloads_are_detected_at_both_nesting_levels():
    assert receipts.error_shaped_payload(json.dumps({"error": "could not read state"}))
    assert receipts.error_shaped_payload(json.dumps({"result": {"error": "nope"}}))
    assert receipts.error_shaped_payload(
        json.dumps({"reached_target": False, "error": "step failed", "txs": []})
    )


def test_ordinary_success_payloads_are_not_error_shaped():
    assert not receipts.error_shaped_payload(json.dumps({"ok": True, "tx_hash": "0xc0ffee"}))
    # An empty or null error field is not a reported failure.
    assert not receipts.error_shaped_payload(json.dumps({"error": ""}))
    assert not receipts.error_shaped_payload(json.dumps({"error": None}))
    # Non-JSON content can never be error-shaped.
    assert not receipts.error_shaped_payload("plain text, not json")
    assert not receipts.error_shaped_payload("")
