"""kami-lens client: one JSON-lines request over the daemon's unix socket (SPEC D7).

The scaffold consumes the world-state daemon **directly** for exactly one
thing: the session-start status brief (P1.12). Everything else the agent
perceives still arrives through the harness MCP surface (D1), which
speaks to the same daemon over the same socket.

This module is argument mapping + one socket round trip + envelope
pass-through, and nothing else. It never recomputes, summarizes, or
annotates an answer, and it owns no query semantics: the daemon's
registry decides what a query name means and what its arguments are.

Wire protocol (kami-lens DESIGN 3.6 / its query socket):

    request   {"id": 1, "query": <name>, "args"?: [str], "noAuthored"?: true}
    response  {"id", "ok": true, "data", "untrusted", "meta"}
            | {"id", "ok": false, "error": {"code", "message"}}

One request object per line in, one response per line out. The envelope
is ``{data, untrusted: [paths], meta}`` and is returned verbatim, minus
the ``id``/``ok`` transport keys.

**Arguments are positional and are deliberately omitted for the brief.**
The daemon prefills the account-index argument of an operator-argument
query from its own configured default operator when the argument list is
empty. That default is daemon-side configuration this repo neither owns
nor can verify, so a brief taken before it is set degrades visibly (D7)
rather than being papered over here.

Two failure classes, both non-fatal to a session (X21):

- ``LensQueryError`` — the daemon answered, with an error. Its code and
  message are the daemon's own and pass through untouched.
- ``LensUnavailableError`` — no answer: no socket, refused, timed out, a
  closed connection, or an unparseable line. There is no daemon text to
  quote, so the code is this module's and the message is the operating
  system's.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path
from typing import Any

# The daemon's socket file name inside its data directory. Mirrors the
# daemon's own constant and the harness client's default resolution, so
# the brief and the harness's world-state tools cannot end up talking to
# two different daemons on one host.
SOCKET_NAME = "kami-lens.sock"

# Override for the resolved default (same variable the harness reads).
LENS_SOCKET_ENV = "KAMI_LENS_SOCKET"

DEFAULT_TIMEOUT_S = 30.0

# Read buffer per recv; a response is one line, however long.
_RECV_BYTES = 65536

# The transport-failure code. There is no daemon to author one, so this
# is the single token the scaffold contributes to an agent-visible
# string; the rest of the record is the operating system's own message.
# Frozen: a unit test pins the exact record bytes.
CODE_UNAVAILABLE = "LENS_UNAVAILABLE"


def default_socket_path() -> str:
    """Platform data dir + socket name — the daemon's own default.

    Resolution order for a caller is: an explicit configured path, then
    ``KAMI_LENS_SOCKET``, then this.
    """
    home = Path.home()
    if sys.platform == "darwin":
        data_dir = home / "Library" / "Application Support" / "kami-lens"
    elif sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or str(home / "AppData" / "Local")
        data_dir = Path(base) / "kami-lens"
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(home / ".local" / "share")
        data_dir = Path(base) / "kami-lens"
    return str(data_dir / SOCKET_NAME)


def resolve_socket_path(configured: str | None = None) -> str:
    """Configured path, else ``KAMI_LENS_SOCKET``, else the platform default."""
    if configured:
        return configured
    return os.environ.get(LENS_SOCKET_ENV) or default_socket_path()


class LensError(Exception):
    """A lens query did not produce an envelope.

    ``as_record`` renders the failure as the minimal machine-shaped
    record injected into the session in place of the envelope. Nothing
    else about a failure is authored: for a query error both fields are
    the daemon's, and for a transport failure only ``code`` is ours.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")

    def as_record(self) -> str:
        return json.dumps(
            {"error": {"code": self.code, "message": self.message}}, ensure_ascii=False
        )


class LensQueryError(LensError):
    """The daemon answered ``ok: false``; its code and message pass through."""


class LensUnavailableError(LensError):
    """No answer from the daemon: unreachable, unresponsive, or unparseable."""

    def __init__(self, message: str) -> None:
        super().__init__(CODE_UNAVAILABLE, message)


class LensClient:
    """Synchronous JSON-lines client for the kami-lens daemon socket.

    Stateless: one connection per query, opened and closed inside
    ``query``. Construction therefore cannot fail and can never abort a
    session — an unreachable daemon is discovered at query time and
    degrades there (X21).
    """

    def __init__(
        self,
        socket_path: str | None = None,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        no_authored: bool = False,
    ) -> None:
        self.socket_path = resolve_socket_path(socket_path)
        self.timeout_s = timeout_s
        # Mirrors what the harness sends under its name-free presentation
        # mode. For the compact roster this is provably a no-op — the
        # answer carries no authored strings at all — but sending it keeps
        # the two request paths identical in kind rather than relying on
        # that property staying true.
        self.no_authored = no_authored

    def query(self, name: str, args: list[Any] | None = None) -> dict[str, Any]:
        """One round trip; returns the envelope or raises ``LensError``.

        Arguments cross the wire as strings, positionally, exactly as the
        daemon's argument parsers expect. An empty list is sent as no
        ``args`` key at all, which is what selects the daemon's
        default-operator prefill.
        """
        request: dict[str, Any] = {"id": 1, "query": name}
        if args:
            request["args"] = [str(a) for a in args]
        if self.no_authored:
            request["noAuthored"] = True
        line = self._round_trip(json.dumps(request) + "\n")
        try:
            response = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LensUnavailableError(f"the daemon answered with unparseable JSON: {exc}") from exc
        if not isinstance(response, dict):
            raise LensUnavailableError("the daemon answered with a non-object response")
        if not response.get("ok"):
            error = response.get("error")
            error = error if isinstance(error, dict) else {}
            raise LensQueryError(
                str(error.get("code") or "INTERNAL"), str(error.get("message") or "")
            )
        return {k: v for k, v in response.items() if k not in ("id", "ok")}

    def _round_trip(self, payload: str) -> str:
        """Send one line, read one line back; every fault is a LensError."""
        data = payload.encode("utf-8")
        try:
            conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                conn.settimeout(self.timeout_s)
                conn.connect(self.socket_path)
                conn.sendall(data)
                buffer = b""
                while b"\n" not in buffer:
                    chunk = conn.recv(_RECV_BYTES)
                    if not chunk:
                        raise LensUnavailableError(
                            "the daemon closed the connection before answering"
                        )
                    buffer += chunk
            finally:
                conn.close()
        except LensError:
            raise
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            raise LensUnavailableError(f"cannot connect to {self.socket_path}: {exc}") from exc
        except TimeoutError:  # socket.timeout is an alias of this
            raise LensUnavailableError(
                f"the daemon did not answer within {self.timeout_s:g}s"
            ) from None
        except OSError as exc:
            raise LensUnavailableError(f"socket error: {exc}") from exc
        return buffer.split(b"\n", 1)[0].decode("utf-8", errors="replace")

    def close(self) -> None:
        """No-op: connections do not outlive a query."""
