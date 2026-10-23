"""A stand-in kami-keeper control socket (unit tier).

One JSON-lines request per connection, as the keeper's SPEC P1 frames it.
``answer(cmd, args)`` returns the frame's ``data`` (an ``ok`` frame) or a
:class:`Refuse` (an error frame); ``silent=True`` accepts connections and
never answers — the daemon that hangs.
"""

from __future__ import annotations

import json
import socketserver
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

KEEPER_VERSION = "1.0.0"


@dataclass
class Refuse:
    code: str
    message: str


def meta() -> dict[str, Any]:
    return {
        "keeperVersion": KEEPER_VERSION,
        "schemaVersion": "0.1.1",
        "servedAt": "2026-10-23T12:00:00.000Z",
        "profile": "lab",
        "lensBlock": None,
        "lensReconciledThrough": None,
        "lensAppliedThrough": None,
    }


class FakeKeeper:
    def __init__(
        self,
        answer: Callable[[str, dict[str, Any]], Any] | None = None,
        *,
        silent: bool = False,
    ) -> None:
        self.answer = answer or (lambda cmd, args: {})
        self.silent = silent
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self._dir = tempfile.mkdtemp(prefix="fkk")
        self.socket_path = str(Path(self._dir) / "keeper.sock")
        self._server: socketserver.ThreadingUnixStreamServer | None = None

    def __enter__(self) -> FakeKeeper:
        fake = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                line = self.rfile.readline()
                if not line:
                    return
                request = json.loads(line)
                fake.requests.append((request["cmd"], request.get("args") or {}))
                if fake.silent:
                    time.sleep(5)
                    return
                data = fake.answer(request["cmd"], request.get("args") or {})
                if isinstance(data, Refuse):
                    frame = {
                        "id": request.get("id"),
                        "ok": False,
                        "error": {"code": data.code, "message": data.message},
                        "meta": meta(),
                    }
                else:
                    frame = {"id": request.get("id"), "ok": True, "data": data, "meta": meta()}
                self.wfile.write((json.dumps(frame) + "\n").encode("utf-8"))

        server = socketserver.ThreadingUnixStreamServer(self.socket_path, Handler)
        server.daemon_threads = True
        self._server = server
        threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        ).start()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    @property
    def commands(self) -> list[str]:
        return [cmd for cmd, _ in self.requests]


def tx_row(
    book: str,
    tx_hash: str,
    *,
    stage: str,
    status: str,
    lens_block: int | None,
    block: int | None = None,
    nonce: int = 1,
    gas: int | None = 51234,
    system: str = "system.harvest.collect",
    at: str = "2026-10-23T11:00:00.000Z",
) -> dict[str, Any]:
    """A ledger ``tx`` row in the keeper's shape (its ledger.py / SPEC P5)."""
    return {
        "schemaVersion": "0.1.1",
        "kind": "tx",
        "at": at,
        "bookId": book,
        "account": "main",
        "lensBlock": lens_block,
        "lensReconciledThrough": None,
        "lensAppliedThrough": None,
        "degraded": [],
        "dryRun": False,
        "tx": {
            "txHash": tx_hash,
            "nonce": nonce,
            "block": block,
            "status": status,
            "gasUsed": gas if stage == "receipt" else None,
            "effectiveGasPrice": None,
            "account": "main",
            "kami": 7,
            "bookId": book,
            "cycleOpenedBy": "keeper",
            "musuCollected": None,
            "system": system,
            "stage": stage,
        },
    }


class Ledger:
    """A ledger the fake keeper serves, honouring ``since`` as the keeper does.

    ``since`` is a LENS-block lower bound, inclusive, and a row with no lens
    basis is returned only when no ``since`` is given (the keeper's
    ``Ledger.since``).
    """

    def __init__(self, books: dict[str, list[dict[str, Any]]]) -> None:
        self.books = books

    def __call__(self, cmd: str, args: dict[str, Any]) -> Any:
        if cmd == "books":
            return {"books": [{"id": b, "account": "main", "state": "running"} for b in self.books]}
        if cmd == "ledger":
            since = args.get("since")
            rows = []
            for row in self.books[args["book"]]:
                if args.get("kind") and row["kind"] != args["kind"]:
                    continue
                if since is not None and (row["lensBlock"] is None or row["lensBlock"] < since):
                    continue
                rows.append(row)
            return {"book": args["book"], "rows": rows}
        return {}
