"""kami-keeper client: one JSON-lines request per connection (SPEC D8, P9).

On the ``shell_keeper`` rung a keeper daemon runs beside the lens. The
AGENT reaches it only through its own shell (the ``keeper`` command inside
the environment). The SCAFFOLD reaches it through this module, for three
operator-side things and nothing else:

- the keeper's version, recorded on ``session_start`` (``keeper_version``);
- the transactions the keeper signed, read back from its ledger at every
  session start and at ``run_complete`` and recorded as ``keeper_tx``
  events — the agent's gas whoever signed it;
- the revoke at ``run_complete``: ``stop --all`` then ``revoke`` for the
  run's account, so no book outlives the run (SPEC P1, X32).

**Nothing read here enters the model's context** (I1). Every answer goes
to telemetry or nowhere.

Wire protocol (the keeper's SPEC P1; the ``lens.py`` pattern):

    request   {"id": 1, "cmd": <name>, "args": {...}}
    response  {"id", "ok": true, "data", "meta"}
            | {"id", "ok": false, "error": {"code", "message"}, "meta"}

One request and one response per connection, then close.
"""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass, field
from typing import Any

# The scaffold's view of the keeper socket: the relay's socket, mounted
# into the agent container at the same path the keeper CLI defaults to.
DEFAULT_KEEPER_SOCKET = "/srv/keeper/keeper.sock"
KEEPER_SOCKET_ENV = "KAMI_KEEPER_SOCKET"

DEFAULT_TIMEOUT_S = 30.0

# The run's own account: the harness roster's `main` label, whose operator
# key the keeper resolves as MAIN_OPERATOR_KEY when the agent grants it.
RUN_ACCOUNT = "main"

_RECV_BYTES = 65536


class KeeperError(Exception):
    """A keeper request produced no answer frame (``code``/``message``)."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def resolve_keeper_socket(configured: str | None = None) -> str:
    """Configured path, else ``KAMI_KEEPER_SOCKET``, else the relay's default."""
    if configured:
        return configured
    return os.environ.get(KEEPER_SOCKET_ENV) or DEFAULT_KEEPER_SOCKET


class KeeperClient:
    """Stateless: one connection per request, so construction cannot fail."""

    def __init__(self, socket_path: str | None = None, *, timeout_s: float = DEFAULT_TIMEOUT_S):
        self.socket_path = resolve_keeper_socket(socket_path)
        self.timeout_s = timeout_s

    def request(self, cmd: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """One round trip; returns the whole frame (``ok`` true or false).

        Raises :class:`KeeperError` only when no frame came back: no
        socket, a refused or silent connection, an unparseable line.
        """
        payload = (json.dumps({"id": 1, "cmd": cmd, "args": args or {}}) + "\n").encode("utf-8")
        try:
            conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                conn.settimeout(self.timeout_s)
                conn.connect(self.socket_path)
                conn.sendall(payload)
                buffer = b""
                while b"\n" not in buffer:
                    chunk = conn.recv(_RECV_BYTES)
                    if not chunk:
                        raise KeeperError(
                            "UNAVAILABLE", "the keeper closed the connection before answering"
                        )
                    buffer += chunk
            finally:
                conn.close()
        except KeeperError:
            raise
        except TimeoutError:
            raise KeeperError(
                "UNAVAILABLE", f"the keeper did not answer within {self.timeout_s:g}s"
            ) from None
        except OSError as exc:
            raise KeeperError("UNAVAILABLE", f"cannot reach {self.socket_path}: {exc}") from exc
        try:
            frame = json.loads(buffer.split(b"\n", 1)[0].decode("utf-8"))
        except ValueError as exc:
            raise KeeperError("UNAVAILABLE", f"unparseable answer: {exc}") from exc
        if not isinstance(frame, dict):
            raise KeeperError("UNAVAILABLE", "the keeper answered a non-object")
        return frame

    def close(self) -> None:
        """No-op: connections do not outlive a request."""


def frame_error(frame: dict[str, Any]) -> str:
    """``CODE: message`` of a refused frame, verbatim."""
    error = frame.get("error")
    error = error if isinstance(error, dict) else {}
    return f"{error.get('code') or 'INTERNAL'}: {error.get('message') or ''}"


def keeper_version(frame: dict[str, Any]) -> str | None:
    meta = frame.get("meta")
    version = meta.get("keeperVersion") if isinstance(meta, dict) else None
    return version if isinstance(version, str) else None


# --- the ledger read (SPEC P9 keeper_tx) ------------------------------------------


@dataclass
class LedgerCursor:
    """Where the last read of each book's ledger stopped.

    ``blocks`` is the highest lens block seen per book: the keeper's
    ``ledger --since`` is a LENS-block lower bound, inclusive, so a
    re-read from it returns that block's rows again and the emitted-hash
    set dedups them. ``open`` holds, per book, the hashes seen broadcast
    (or with an unconfirmed receipt) whose final receipt row has not been
    seen yet: the keeper writes a late receipt row with no lens basis
    (``lensBlock: null``), and ``--since`` never returns such a row, so a
    book with an open hash is read whole until it closes.

    A cache, kept in ``state.json``: losing it costs one whole re-read of
    each book, never a duplicate event, because the set of hashes already
    emitted is folded from telemetry.
    """

    blocks: dict[str, int] = field(default_factory=dict)
    open: dict[str, list[str]] = field(default_factory=dict)


def read_keeper_receipts(
    client: KeeperClient,
    cursor: LedgerCursor,
    emitted: set[str],
    *,
    account: str = RUN_ACCOUNT,
) -> tuple[list[dict[str, Any]], str | None]:
    """New final receipts across the account's books, as ``keeper_tx`` fields.

    Returns ``(events, keeper_version)``. Mutates ``cursor`` and
    ``emitted``. A keeper that does not answer, or a book whose ledger
    cannot be read, contributes nothing and changes nothing — the next
    read starts from the same place.

    A FINAL receipt is a ``tx`` row at ``stage: receipt`` whose status is
    not ``unconfirmed``: the keeper writes an ``unconfirmed`` receipt row
    when its receipt read did not answer and the step stays open (its
    SPEC P5), so emitting on it would record a wrong status and then
    drop the real one as a duplicate.
    """
    try:
        books_frame = client.request("books", {"account": account})
    except KeeperError:
        return [], None
    version = keeper_version(books_frame)
    data = books_frame.get("data") if books_frame.get("ok") else None
    books = data.get("books") if isinstance(data, dict) else None
    events: list[dict[str, Any]] = []
    for book in books if isinstance(books, list) else []:
        book_id = book.get("id") if isinstance(book, dict) else None
        if not isinstance(book_id, str):
            continue
        whole = bool(cursor.open.get(book_id)) or book_id not in cursor.blocks
        rows = _ledger_rows(client, book_id, None if whole else cursor.blocks[book_id])
        if rows is None:
            continue
        events.extend(_take(book_id, rows, cursor, emitted))
        if not whole and cursor.open.get(book_id):
            # A broadcast seen in this window may already have its final
            # receipt, written without a lens basis: read the book whole
            # once more so this read point is complete.
            rows = _ledger_rows(client, book_id, None)
            if rows is not None:
                events.extend(_take(book_id, rows, cursor, emitted))
    return events, version


def _ledger_rows(client: KeeperClient, book_id: str, since: int | None) -> list[Any] | None:
    args: dict[str, Any] = {"book": book_id, "kind": "tx"}
    if since is not None:
        args["since"] = since
    try:
        frame = client.request("ledger", args)
    except KeeperError:
        return None
    data = frame.get("data") if frame.get("ok") else None
    rows = data.get("rows") if isinstance(data, dict) else None
    return rows if isinstance(rows, list) else None


def _take(
    book_id: str, rows: list[Any], cursor: LedgerCursor, emitted: set[str]
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    still_open = set(cursor.open.get(book_id, []))
    highest = cursor.blocks.get(book_id)
    for row in rows:
        if not isinstance(row, dict) or row.get("kind") != "tx":
            continue
        lens_block = row.get("lensBlock")
        if isinstance(lens_block, int) and not isinstance(lens_block, bool):
            highest = lens_block if highest is None else max(highest, lens_block)
        tx = row.get("tx")
        if not isinstance(tx, dict):
            continue
        tx_hash = tx.get("txHash")
        if not isinstance(tx_hash, str) or not tx_hash:
            continue
        stage = tx.get("stage")
        status = tx.get("status")
        if stage == "receipt" and status != "unconfirmed":
            still_open.discard(tx_hash)
            if tx_hash in emitted:
                continue
            emitted.add(tx_hash)
            events.append(_event(book_id, row, tx))
        elif stage in ("broadcast", "receipt") and tx_hash not in emitted:
            still_open.add(tx_hash)
    if highest is not None:
        cursor.blocks[book_id] = highest
    if still_open:
        cursor.open[book_id] = sorted(still_open)
    else:
        cursor.open.pop(book_id, None)
    return events


def _event(book_id: str, row: dict[str, Any], tx: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "book": book_id,
        "tx_hash": tx["txHash"],
        "action": str(tx.get("system") or ""),
        "status": str(tx.get("status") or ""),
        "initiator": "keeper",
        "source": "keeper",
    }
    for key, name in (("block", "block"), ("nonce", "nonce"), ("gasUsed", "gas_used")):
        value = tx.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            fields[name] = value
    if isinstance(row.get("at"), str):
        fields["ledger_ts"] = row["at"]
    if isinstance(tx.get("stage"), str):
        fields["receipt_stage"] = tx["stage"]
    return fields
