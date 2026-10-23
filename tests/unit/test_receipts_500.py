"""The terminal-state classifier on kami-harness 5.0.0's own texts (SPEC P9, D1; 0.8.0).

From 0.8.0 the classifier keys on the harness's stable clauses — a
``transaction <hash> …`` clause for every post-broadcast state, the
earliest one deciding, and ``no transaction sent`` in the FIRST clause of
a refusal before signing — rather than on one fixed prefix and markers
matched anywhere. The fixture is 5.0.0's own wording, generated from its
exception classes.
"""

import json
from pathlib import Path

import pytest

from kami_agent.tools.receipts import classify_error, tx_hash_from_error

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "harness_500_refusals.json").read_text(encoding="utf-8")
)["messages"]

D = "0x" + "d4" * 32
A = "0x" + "a1" * 32
C = "0x" + "c3" * 32

EXPECTED = {
    "validation_rejected": ("validation_rejected", None),
    "dry_run_reverted": ("validation_rejected", None),
    # Not performed is still nothing signed, nothing spent, nothing done.
    "dry_run_not_performed": ("validation_rejected", None),
    "soulbound": ("validation_rejected", None),
    "transfer_target_not_account": ("validation_rejected", None),
    # The approve landed and spent gas: not one state, so none (P9 table).
    "refused_after_approve_landed": (None, None),
    "reverted": ("reverted", D),
    "reverted_no_reason": ("reverted", D),
    "unconfirmed": ("unconfirmed", D),
    "unconfirmed_wait_failed": ("unconfirmed", D),
    "not_executed_collision": ("not_executed", D),
    "not_executed_dropped": ("not_executed", D),
    # 5.0.0: the deposit's own outcome after the lead naming the approve.
    "deposit_reverted_after_approve": ("reverted", D),
    "deposit_unconfirmed_after_approve": ("unconfirmed", D),
    "deposit_not_executed_after_approve": ("not_executed", D),
    "lane_blocked": (None, None),
    "batch_error": ("batch_error", None),
    "owner_key_missing": (None, None),
}


def test_the_fixture_covers_every_expectation():
    assert set(FIXTURE) == set(EXPECTED)


@pytest.mark.parametrize("name", sorted(EXPECTED))
@pytest.mark.parametrize(
    "wrapper", ["", "Error executing tool portal_deposit: "], ids=["bare", "mcp"]
)
def test_every_5_0_0_text_classifies_as_its_contract_says(name, wrapper):
    message = wrapper + FIXTURE[name]
    state, tx_hash = EXPECTED[name]
    assert classify_error(message) == state
    assert tx_hash_from_error(message) == tx_hash


def test_a_refusal_that_quotes_a_revert_clause_is_still_a_refusal():
    """The first clause decides: nothing was sent by THIS call, whatever the
    detail quotes about another transaction."""
    message = (
        "validation failed; no transaction sent: the last attempt, transaction "
        f"{C} landed on-chain in block 9 and REVERTED: gas was spent (1 gas)"
    )
    assert classify_error(message) == "validation_rejected"
    assert tx_hash_from_error(message) is None


def test_a_reworded_refusal_is_read_by_its_first_clause():
    message = "refused before signing; no transaction sent: kami 5 is resting"
    assert classify_error(message) == "validation_rejected"
    assert classify_error("Error executing tool feed_kami: " + message) == "validation_rejected"


def test_no_transaction_sent_outside_the_first_clause_moves_nothing():
    message = (
        f"transaction {D} is UNCONFIRMED: it was broadcast, but no receipt arrived "
        "within 60s. The receipt wait failed: no transaction sent: a proxy's own words"
    )
    assert classify_error(message) == "unconfirmed"
    assert tx_hash_from_error(message) == D


def test_the_earliest_transaction_clause_is_the_outcome():
    message = (
        f"transaction {D} is UNCONFIRMED: it was broadcast. Its nonce follows "
        f"transaction {C} landed on-chain in block 5 and REVERTED earlier."
    )
    assert classify_error(message) == "unconfirmed"
    assert tx_hash_from_error(message) == D
