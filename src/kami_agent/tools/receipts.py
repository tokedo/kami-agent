"""Harness receipt-status classification for telemetry (SPEC D1, P9).

The harness distinguishes three terminal states for a submitted
transaction, plus a pre-signing rejection that never reaches the chain.
Which one occurred is recoverable only from the harness's own message
text, so it is classified **once, here, at ingestion** and recorded as a
telemetry field — analysis never has to string-match harness prose.

Nothing in this module changes what the agent sees. The message the
harness produced is passed to the model verbatim (SPEC P2); this is a
read-only observation of it for the operator-side event stream.

The classifier keys on the harness's STABLE CLAUSES (from 0.8.0), not on
any one sentence of its prose — the contract text of its SPEC P4:

- every post-broadcast terminal state names its transaction in a clause
  that opens ``transaction <hash>``: ``… landed on-chain in block N and
  REVERTED`` (``OnChainRevertError``), ``… is UNCONFIRMED``
  (``TxUnconfirmedError``), ``… was NOT executed``
  (``TxNotExecutedError``: ``TxNonceCollisionError`` and
  ``TxDroppedError``, kami-harness 4.0.0). The EARLIEST such clause in a
  message is its outcome: the harness puts what it reports first and may
  quote other transactions after it. From 5.0.0 a deposit whose approve
  landed reports its own outcome after a lead naming the approve
  (``the approve <hash> landed …; the deposit: transaction <hash> …``),
  which the clause finds where it stands;
- a refusal before signing says ``no transaction sent`` in its FIRST
  clause (the text before the first ``": "``), the harness's
  ``PreTxValidationError.PREFIX`` among others. Read in the first clause
  only: a later quote of those words, or of a transaction clause inside a
  refusal's detail, does not move the outcome. A refusal after an earlier
  transaction of the same call landed (``RefusedAfterLandedError``, 4.7.0)
  leads with what landed instead, and is therefore no single state;
- ``BatchTxError`` — a multi-transaction call in which at least one item
  failed; its message itemizes every outcome, successes included.

The MCP server wraps a raised tool exception as
``Error executing tool <name>: <message>``; the wrapper is stripped
before the first clause is read, and the transaction clauses are found
wherever they stand.

An unrecognized message classifies as ``None`` (field omitted): most
harness errors are not transaction outcomes at all, and inventing a
state for them would be worse than recording nothing.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Confirmed on-chain success of a submitted transaction. Derived from the
# returned result, not from an error: the harness returns status="success"
# only after the receipt confirmed.
CONFIRMED_SUCCESS = "confirmed_success"
# Included on-chain and reverted: gas spent, no state change, final.
REVERTED = "reverted"
# Broadcast, but no receipt within the harness's timeout. The outcome is
# unknown and the transaction may still land.
UNCONFIRMED = "unconfirmed"
# A precondition failed before signing: nothing was broadcast, no gas spent.
VALIDATION_REJECTED = "validation_rejected"
# A multi-transaction call with at least one failed item; the message
# itemizes per-item outcomes, which may mix all of the states above.
BATCH_ERROR = "batch_error"
# Broadcast and PROVEN never to execute (kami-harness 4.0.0): its nonce
# was consumed by another transaction, or the node no longer holds it and
# its nonce was released. This hash spent no gas, changed nothing, and
# will never appear in a block. Not a revert (which mined and spent gas)
# and not unconfirmed (whose outcome is still open).
NOT_EXECUTED = "not_executed"

TERMINAL_STATES = (
    CONFIRMED_SUCCESS,
    REVERTED,
    UNCONFIRMED,
    VALIDATION_REJECTED,
    BATCH_ERROR,
    NOT_EXECUTED,
)

# A BatchTxError message embeds per-item outcomes whose text can contain
# any of the single-transaction clauses, so it is recognized first.
_BATCH_MARKERS = ("Per-item outcomes:", "are final")

# The phrase a refusal before signing carries in its first clause.
_NO_TRANSACTION_SENT = "no transaction sent"

# The MCP server's wrapper around a raised tool exception.
_MCP_WRAPPER = re.compile(r"^Error executing tool [^:\s]+: ")

# The post-broadcast clauses, each naming its transaction first: the
# harness's own contract phrasing (SPEC P4 of kami-harness). The hash is
# group 1, so it is recoverable without parsing the rest of the prose.
_TX_CLAUSES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        REVERTED,
        re.compile(r"transaction (0x[0-9a-fA-F]+) landed on-chain in block \d+ and REVERTED"),
    ),
    (UNCONFIRMED, re.compile(r"transaction (0x[0-9a-fA-F]+) is UNCONFIRMED")),
    (NOT_EXECUTED, re.compile(r"transaction (0x[0-9a-fA-F]+) was NOT executed")),
)


def _first_clause(message: str) -> str:
    """The message up to its first ``": "``, with the MCP wrapper removed."""
    text = _MCP_WRAPPER.sub("", message, count=1)
    return text.split(": ", 1)[0]


def _earliest_tx_clause(message: str) -> tuple[str, str] | None:
    """(state, hash) of the transaction clause that opens earliest, or None."""
    found = [
        (match.start(), state, match.group(1))
        for state, pattern in _TX_CLAUSES
        if (match := pattern.search(message)) is not None
    ]
    if not found:
        return None
    _, state, tx_hash = min(found)
    return state, tx_hash


def classify_error(message: str) -> str | None:
    """Terminal state of a failed harness tool call, or None if not a tx outcome."""
    if all(marker in message for marker in _BATCH_MARKERS):
        return BATCH_ERROR
    if _NO_TRANSACTION_SENT in _first_clause(message):
        return VALIDATION_REJECTED
    clause = _earliest_tx_clause(message)
    return clause[0] if clause is not None else None


def tx_hash_from_error(message: str) -> str | None:
    """The transaction hash a raised revert/unconfirmed/not-executed error names.

    A harness that RAISES its terminal states reports the transaction in
    prose, so the hash reaches the scaffold only inside the error text —
    the one field P9 tells readers never to parse. This lifts it onto
    ``tool_call.tx_hash`` at ingestion, on the same terms as the success
    path: recovered once, here, or recorded as absent.

    A not-executed transaction's hash is lifted too: it was signed and
    broadcast, and a reconciliation keyed on hashes needs to know that
    this one will never appear in a block (``tx_terminal_state`` says so).
    For a nonce collision the message also names the hash that consumed
    the nonce; that one is NOT this call's transaction and is not lifted.

    Returns None for batch errors (no single transaction), for
    validation rejections (nothing was ever broadcast, so there is no
    hash to report), and for anything unrecognized.
    """
    if classify_error(message) in (BATCH_ERROR, VALIDATION_REJECTED, None):
        return None
    clause = _earliest_tx_clause(message)
    return clause[1] if clause is not None else None


def error_shaped_payload(content: str) -> bool:
    """True when a RETURNED result's body carries a non-empty error field.

    A tool that reports failure by returning ``{"error": ...}`` instead of
    raising produces ``ok=true`` — ``ok`` is exception-keyed by contract
    (P9) and that is not changed here. This is the honest flag for the
    shape, so a reader can separate "the call raised" from "the call
    returned a failure" without parsing payloads or moving ``ok``.

    Checked at the top level and one ``result`` level down, the same
    nesting the tx_hash extractor and the repetition classifier tolerate.
    Non-JSON content is never error-shaped.
    """
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return False
    candidates: list[Any] = [parsed]
    if isinstance(parsed, dict) and isinstance(parsed.get("result"), dict):
        candidates.append(parsed["result"])
    return any(isinstance(c, dict) and c.get("error") for c in candidates)


def classify_success(content: str, structured: Any = None) -> str | None:
    """Terminal state of a returned harness result, or None if not a tx outcome.

    Only a top-level ``status`` (or one ``result`` level down, matching
    the tx_hash extraction path) counts: ``"success"`` is a confirmed
    receipt, and ``"dropped"`` — the harness's per-row word for a
    transaction it proved will never execute (4.0.0) — is NOT_EXECUTED.
    Partial batches under ``allow_partial`` return per-item outcomes with
    no single terminal state, and pre-send dry-run skips report
    ``skipped``; both classify as None rather than being flattened into
    one label. Rows inside a multi-transaction ``txs`` list are not
    classified here: they are copied verbatim, ``dropped`` included.
    """
    for candidate in (structured, _maybe_json(content)):
        if not isinstance(candidate, dict):
            continue
        inner = candidate.get("result")
        for level in (candidate, inner if isinstance(inner, dict) else {}):
            status = level.get("status")
            # A str check, not a bare lookup: a payload's `status` may be
            # anything, and an unhashable one must classify as nothing.
            if isinstance(status, str) and status in _RETURNED_STATUS:
                return _RETURNED_STATUS[status]
    return None


# A returned single-transaction ``status`` and the terminal state it is.
_RETURNED_STATUS: dict[str, str] = {
    "success": CONFIRMED_SUCCESS,
    "dropped": NOT_EXECUTED,
}


def _maybe_json(text: str) -> Any:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
