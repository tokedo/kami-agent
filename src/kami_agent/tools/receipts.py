"""Harness receipt-status classification for telemetry (SPEC D1, P9).

The harness distinguishes three terminal states for a submitted
transaction, plus a pre-signing rejection that never reaches the chain.
Which one occurred is recoverable only from the harness's own message
text, so it is classified **once, here, at ingestion** and recorded as a
telemetry field — analysis never has to string-match harness prose.

Nothing in this module changes what the agent sees. The message the
harness produced is passed to the model verbatim (SPEC P2); this is a
read-only observation of it for the operator-side event stream.

Markers are the harness's own contract text, not incidental wording:

- ``PreTxValidationError.PREFIX`` — a fixed prefix the harness SPEC
  states is always present, guaranteeing the transaction was never sent.
- ``OnChainRevertError`` / ``TxUnconfirmedError`` — the two raised
  post-broadcast terminal states, each with distinct fixed phrasing.
- ``TxNotExecutedError`` (kami-harness 4.0.0) — its two subclasses,
  ``TxNonceCollisionError`` and ``TxDroppedError``, report a broadcast
  transaction the harness PROVED will never execute; both open with
  ``transaction <hash> was NOT executed`` and say the hash spent no gas.
  The same verdict on a per-leg row is ``status: "dropped"``.
- ``BatchTxError`` — a multi-transaction call in which at least one item
  failed; its message itemizes every outcome, successes included.

The MCP server wraps a raised tool exception as
``Error executing tool <name>: <message>``, so markers are matched as
substrings rather than anchored at position 0.

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

# Ordered: a BatchTxError message embeds per-item outcomes whose text can
# contain any of the single-transaction markers, so it is tested first.
_ERROR_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (BATCH_ERROR, ("Per-item outcomes:", "are final")),
    # Before UNCONFIRMED on purpose: a nonce collision's text says it is
    # "not an unconfirmed transaction".
    (NOT_EXECUTED, ("was NOT executed", "spent no gas")),
    (UNCONFIRMED, ("is UNCONFIRMED",)),
    (REVERTED, ("landed on-chain in block", "REVERTED")),
    (VALIDATION_REJECTED, ("validation failed; no transaction sent:",)),
)


# The transaction hash a raised terminal state names in its first clause.
# The raised single-transaction states — reverted, unconfirmed, and from
# 4.0.0 not executed — all open with "transaction <hash> ",
# which is the harness's own contract phrasing (the same phrasing the
# markers above match on), so the hash is recoverable without parsing the
# rest of the prose. Anchored at the start of the clause rather than
# matched anywhere, so a hash quoted later in a message — a batch message
# itemizing several — is never mistaken for THE transaction: a batch has
# no single hash, and inventing one would be worse than reporting none.
_RAISED_TX_HASH = re.compile(
    r"transaction (0x[0-9a-fA-F]+) (?:landed on-chain|is UNCONFIRMED|was NOT executed)"
)


def classify_error(message: str) -> str | None:
    """Terminal state of a failed harness tool call, or None if not a tx outcome."""
    for state, markers in _ERROR_MARKERS:
        if all(marker in message for marker in markers):
            return state
    return None


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
    if classify_error(message) == BATCH_ERROR:
        return None
    match = _RAISED_TX_HASH.search(message)
    return match.group(1) if match else None


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
