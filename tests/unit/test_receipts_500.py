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

from kami_agent.harness import _extract_txs
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


# --- 0.8.2 (F-TS6): the per-step receipts of a batch tool ---------------------------------
#
# kami-harness 5.0.0's act_sequence reports every step under `steps[]`, each
# row {index, op, <its ids>, status, and where a transaction exists tx_hash /
# block / gas_used / fee_wei, plus payouts / cooldowns / the decoded kill /
# reason by op and state} (executor/server.py act_sequence; SPEC P4 "A
# pipelined sequence has K terminal states"), and no `txs`. A 7-transaction
# batch therefore left no hash in telemetry: run 3 s1 recorded 46 hashes
# against 53 chain transactions, the 7 missing being one batch's steps.

S = ["0x" + str(digit) * 64 for digit in range(1, 8)]

SEVEN_STEP_SEQUENCE = {
    "status": "partial",
    "steps": [
        {
            "index": 0,
            "op": "harvest_stop",
            "kami_ids": [11],
            "status": "success",
            "tx_hash": S[0],
            "block": 1001,
            "gas_used": 201556,
            "fee_wei": "25866000000000",
            "payouts": [{"kami_id": 11, "item": 2, "item_name": "VIPP", "amount": 515}],
            "cooldowns": [{"kami_id": 11, "cooldown_until": 1700000180}],
        },
        {
            "index": 1,
            "op": "feed",
            "kami_id": 11,
            "item_id": 11301,
            "status": "success",
            "tx_hash": S[1],
            "block": 1001,
            "gas_used": 48211,
            "fee_wei": "6188000000000",
        },
        {
            "index": 2,
            "op": "feed",
            "kami_id": 12,
            "item_id": 11301,
            "status": "reverted",
            "tx_hash": S[2],
            "block": 1002,
            "gas_used": 48211,
            "fee_wei": None,
            "reason": "revert reason unavailable (replay inconclusive)",
        },
        {
            "index": 3,
            "op": "harvest_start",
            "kami_ids": [11, 12],
            "node_index": 56,
            "status": "success",
            "tx_hash": S[3],
            "block": 1002,
            "gas_used": 160233,
            "fee_wei": "20561000000000",
            "cooldowns": [{"kami_id": 11, "cooldown_until": None, "decode_error": "unstated"}],
        },
        {
            "index": 4,
            "op": "liquidate",
            "kami_id": 13,
            "victim_kami_id": 99,
            "status": "success",
            "tx_hash": S[4],
            "block": 1003,
            "gas_used": 310402,
            "fee_wei": "39845000000000",
            "victim_gross": 1200,
            "spoils": 300,
            "attacker_hp_after": 41,
            "cooldown_until": 1700000300,
        },
        {
            "index": 5,
            "op": "feed",
            "kami_id": 13,
            "item_id": 11301,
            "status": "unconfirmed",
            "tx_hash": S[5],
            "reason": "no receipt within 60s",
        },
        {
            "index": 6,
            "op": "harvest_stop",
            "kami_ids": [12],
            "status": "success",
            "tx_hash": S[6],
            "block": 1004,
            "gas_used": 199870,
            "fee_wei": "25650000000000",
            "payouts": [{"kami_id": 12, "item": 2, "item_name": "VIPP", "amount": 0}],
        },
    ],
    "sent": 7,
    "landed": 5,
    "account": "main",
}


@pytest.mark.parametrize("structured", [True, False], ids=["structured", "text"])
def test_a_seven_step_sequence_lifts_seven_receipts(structured):
    payload = SEVEN_STEP_SEQUENCE
    text = json.dumps(payload)
    txs = _extract_txs(payload if structured else None, text)
    assert [t["tx_hash"] for t in txs] == S
    # Verbatim, in document order: the reverted and the unconfirmed step
    # are real transactions and are kept with the harness's own fields.
    assert list(txs) == payload["steps"]


def test_steps_with_no_transaction_lift_nothing():
    """A step that never reached the chain (`not_sent`, no hash) is no receipt."""
    payload = {
        "status": "partial",
        "steps": [
            {
                "index": 0,
                "op": "feed",
                "kami_id": 11,
                "item_id": 11301,
                "status": "not_sent",
                "reason": "the node refused it",
            },
            {
                "index": 1,
                "op": "harvest_stop",
                "kami_ids": [11],
                "status": "not_sent",
                "consumed_by": S[0],
            },
            "not a row",
        ],
        "sent": 0,
        "landed": 0,
        "account": "main",
    }
    assert _extract_txs(None, json.dumps(payload)) == ()


def test_a_result_with_txs_and_steps_lifts_both_and_no_hash_twice():
    payload = {
        "txs": [
            {"tx_hash": S[0], "status": "success", "block": 1001},
            {"tx_hash": S[1], "status": "success", "block": 1001},
        ],
        "steps": [
            {
                "index": 0,
                "op": "feed",
                "kami_id": 11,
                "item_id": 11301,
                "status": "success",
                "tx_hash": S[1],
                "block": 1001,
            },
            {
                "index": 1,
                "op": "feed",
                "kami_id": 12,
                "item_id": 11301,
                "status": "success",
                "tx_hash": S[2],
                "block": 1002,
            },
        ],
    }
    txs = _extract_txs(None, json.dumps(payload))
    assert [t["tx_hash"] for t in txs] == [S[0], S[1], S[2]]
    # `txs` entries first, then the steps that add a hash.
    assert list(txs) == payload["txs"] + payload["steps"][1:]
