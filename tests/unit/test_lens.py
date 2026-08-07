"""kami-lens client: wire framing, envelope pass-through, failure classes (SPEC D7).

Every test here runs against a real AF_UNIX socket server, because the
thing under test IS the framing: a mock would assert the shape this
module already believes in.
"""

import json
import os
import socket
import tempfile
import threading

import pytest

from kami_agent.lens import (
    CODE_UNAVAILABLE,
    LensClient,
    LensQueryError,
    LensUnavailableError,
    default_socket_path,
    resolve_socket_path,
)

ENVELOPE = {
    "data": {"account": {"index": 4271, "roomIndex": 11}, "kamis": []},
    "untrusted": [],
    "meta": {
        "servedAt": "2026-08-07T12:00:00.000Z",
        "blockNumber": 8814052,
        "stale": False,
        "mode": "daemon",
    },
}


class FakeDaemon:
    """One-line-in / one-line-out unix socket server, as the daemon speaks."""

    def __init__(self, responder):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "kami-lens.sock")
        self.requests = []
        self._responder = responder
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self.path)
        self._server.listen(4)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            with conn:
                buffer = b""
                while b"\n" not in buffer:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buffer += chunk
                if not buffer:
                    continue
                request = json.loads(buffer.split(b"\n", 1)[0])
                self.requests.append(request)
                reply = self._responder(request)
                if reply is not None:
                    conn.sendall(reply)

    def close(self):
        self._server.close()


def ok_responder(request):
    return (json.dumps({"id": request.get("id"), "ok": True, **ENVELOPE}) + "\n").encode()


@pytest.fixture
def daemon():
    made = []

    def build(responder=ok_responder):
        d = FakeDaemon(responder)
        made.append(d)
        return d

    yield build
    for d in made:
        d.close()


# --- wire framing --------------------------------------------------------------


def test_request_framing_is_one_json_line(daemon):
    d = daemon()
    LensClient(d.path).query("roster")
    assert d.requests == [{"id": 1, "query": "roster"}]


def test_the_brief_sends_no_arguments(daemon):
    """The account index is the DAEMON's to prefill (D7).

    An empty argument list is what selects that prefill; sending an index
    would mean the scaffold had decided which account the run is, which it
    has no way to know.
    """
    d = daemon()
    LensClient(d.path).query("roster")
    assert "args" not in d.requests[0]


def test_arguments_cross_the_wire_as_positional_strings(daemon):
    d = daemon()
    LensClient(d.path).query("party", [4271])
    assert d.requests[0]["args"] == ["4271"]


def test_name_free_mode_is_forwarded(daemon):
    d = daemon()
    LensClient(d.path, no_authored=True).query("roster")
    assert d.requests[0]["noAuthored"] is True
    d2 = daemon()
    LensClient(d2.path).query("roster")
    assert "noAuthored" not in d2.requests[0]


def test_envelope_is_returned_verbatim_minus_transport_keys(daemon):
    d = daemon()
    assert LensClient(d.path).query("roster") == ENVELOPE


def test_a_response_longer_than_one_recv_is_reassembled(daemon):
    big = {**ENVELOPE, "data": {"kamis": [{"index": i} for i in range(20000)]}}
    d = daemon(lambda req: (json.dumps({"id": 1, "ok": True, **big}) + "\n").encode())
    assert LensClient(d.path).query("roster")["data"]["kamis"][-1]["index"] == 19999


# --- failure classes -----------------------------------------------------------


def test_query_error_passes_the_daemons_code_and_message_through(daemon):
    d = daemon(
        lambda req: (
            json.dumps(
                {
                    "id": 1,
                    "ok": False,
                    "error": {"code": "BAD_ARGS", "message": "account index must be an integer"},
                }
            )
            + "\n"
        ).encode()
    )
    with pytest.raises(LensQueryError) as exc:
        LensClient(d.path).query("roster")
    assert exc.value.code == "BAD_ARGS"
    assert exc.value.message == "account index must be an integer"
    # The injected record is the daemon's words, not ours.
    assert json.loads(exc.value.as_record()) == {
        "error": {"code": "BAD_ARGS", "message": "account index must be an integer"}
    }


def test_absent_socket_is_unavailable_not_an_empty_answer(tmp_path):
    with pytest.raises(LensUnavailableError) as exc:
        LensClient(str(tmp_path / "nothing.sock")).query("roster")
    assert exc.value.code == CODE_UNAVAILABLE
    assert json.loads(exc.value.as_record())["error"]["code"] == CODE_UNAVAILABLE


def test_a_daemon_that_closes_without_answering_is_unavailable(daemon):
    d = daemon(lambda req: None)
    with pytest.raises(LensUnavailableError) as exc:
        LensClient(d.path).query("roster")
    assert "closed the connection" in exc.value.message


def test_a_silent_daemon_times_out(daemon):
    def hang(request):
        threading.Event().wait(5)
        return None

    d = daemon(hang)
    with pytest.raises(LensUnavailableError) as exc:
        LensClient(d.path, timeout_s=0.2).query("roster")
    assert "did not answer within" in exc.value.message


def test_an_unparseable_line_is_unavailable_not_a_crash(daemon):
    d = daemon(lambda req: b"this is not json\n")
    with pytest.raises(LensUnavailableError) as exc:
        LensClient(d.path).query("roster")
    assert "unparseable JSON" in exc.value.message


def test_every_failure_renders_the_same_record_shape(daemon):
    """One shape for both classes: a reader never parses prose to tell them apart."""
    d = daemon(lambda req: (json.dumps({"id": 1, "ok": False}) + "\n").encode())
    with pytest.raises(LensQueryError) as query_error:
        LensClient(d.path).query("roster")
    with pytest.raises(LensUnavailableError) as transport_error:
        LensClient("/nonexistent/kami-lens.sock").query("roster")
    for exc in (query_error.value, transport_error.value):
        record = json.loads(exc.as_record())
        assert set(record) == {"error"}
        assert set(record["error"]) == {"code", "message"}


# --- socket path resolution ----------------------------------------------------


def test_configured_path_wins_over_environment(monkeypatch):
    monkeypatch.setenv("KAMI_LENS_SOCKET", "/from/env.sock")
    assert resolve_socket_path("/from/manifest.sock") == "/from/manifest.sock"


def test_environment_wins_over_the_platform_default(monkeypatch):
    monkeypatch.setenv("KAMI_LENS_SOCKET", "/from/env.sock")
    assert resolve_socket_path(None) == "/from/env.sock"


def test_platform_default_is_the_daemons_own(monkeypatch):
    monkeypatch.delenv("KAMI_LENS_SOCKET", raising=False)
    # The harness resolves the same three levels for its own world-state
    # reads; a mismatch here would point the brief at a different daemon
    # than every other read in the run.
    assert resolve_socket_path(None) == default_socket_path()
    assert default_socket_path().endswith("kami-lens.sock")
