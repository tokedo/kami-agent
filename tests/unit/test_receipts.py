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
