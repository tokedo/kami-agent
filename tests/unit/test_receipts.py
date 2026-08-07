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
