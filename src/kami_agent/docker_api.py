"""Docker Engine API client over a unix socket — the scaffold's only docker path (SPEC D8).

The ``shell`` rungs (SPEC D3, P10) run the agent's commands inside a
persistent environment container. The scaffold reaches that container
through the Docker Engine HTTP API on the socket mounted into its own
container, and through nothing else: there is no docker CLI in the agent
image, and no third-party client library. The raw API was chosen over the
``docker`` Python package because the scaffold uses seven endpoints, the
standard library speaks HTTP/1.1 over a unix socket in a few lines, and a
new dependency would have to be pinned by hash into every image that
installs this package for exactly those seven calls.

Endpoints used (unversioned paths: the daemon serves its own API version):

    GET  /containers/{name}/json           inspect (running, RestartCount, image)
    POST /containers/{name}/start          start a stopped container
    POST /containers/{name}/stop?t=N       stop with a grace period
    GET  /containers/{name}/top?ps_args=…  the process table
    POST /containers/{name}/exec           create an exec instance
    POST /exec/{id}/start                  run it; stdout/stderr come back multiplexed
    GET  /exec/{id}/json                   its exit code

Nothing here is agent-visible except through the caller: the ``shell``
tool turns a :class:`DockerError` into a tool error whose text is the
daemon's own message.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import struct
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

DEFAULT_DOCKER_SOCKET = "/var/run/docker.sock"
DOCKER_HOST_ENV = "DOCKER_HOST"

DEFAULT_TIMEOUT_S = 30.0

# Multiplexed-stream frame header (Tty=false): 1 byte stream type, 3 zero
# bytes, 4 bytes big-endian payload size.
_FRAME_HEADER = struct.Struct(">BxxxL")
_STREAM_STDOUT = 1
_STREAM_STDERR = 2

_RECV_BYTES = 65536


class DockerError(Exception):
    """The docker API refused, or could not be reached.

    ``status`` is the HTTP status the daemon answered (None when there was
    no answer); ``message`` is the daemon's own text when it served one,
    else the operating system's.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        self.status = status
        self.message = message
        super().__init__(message)


def resolve_docker_socket(configured: str | None = None) -> str:
    """Configured path, else ``DOCKER_HOST`` (``unix://`` only), else the default."""
    if configured:
        return configured
    host = os.environ.get(DOCKER_HOST_ENV, "")
    if host.startswith("unix://"):
        return host[len("unix://") :]
    return DEFAULT_DOCKER_SOCKET


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._socket_path)
        self.sock = sock


@dataclass(frozen=True, slots=True)
class ExecResult:
    """One finished exec: the exit code as observed, and the byte totals."""

    exit_code: int | None
    stdout_bytes: int
    stderr_bytes: int
    duration_s: float


class DockerClient:
    """Synchronous Docker Engine API client; one connection per request."""

    def __init__(self, socket_path: str | None = None, *, timeout_s: float = DEFAULT_TIMEOUT_S):
        self.socket_path = resolve_docker_socket(socket_path)
        self.timeout_s = timeout_s

    # --- JSON endpoints ------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        timeout_s: float | None = None,
    ) -> tuple[int, bytes]:
        conn = _UnixHTTPConnection(self.socket_path, timeout_s or self.timeout_s)
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Host": "docker", "Connection": "close"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        try:
            conn.request(method, path, body=payload, headers=headers)
            response = conn.getresponse()
            return response.status, response.read()
        except (OSError, http.client.HTTPException) as exc:
            raise DockerError(f"docker API unreachable at {self.socket_path}: {exc}") from exc
        finally:
            conn.close()

    def _call(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        ok: tuple[int, ...] = (200,),
        timeout_s: float | None = None,
    ) -> tuple[int, Any]:
        status, raw = self._request(method, path, body, timeout_s=timeout_s)
        if status not in ok:
            raise DockerError(_daemon_message(raw, status), status)
        if not raw:
            return status, None
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, None

    def inspect(self, name: str) -> dict[str, Any]:
        _, data = self._call("GET", f"/containers/{_q(name)}/json")
        return data if isinstance(data, dict) else {}

    def start(self, name: str) -> bool:
        """Start the container; True if this call started it (204), False if it ran (304)."""
        status, _ = self._call("POST", f"/containers/{_q(name)}/start", ok=(204, 304))
        return status == 204

    def stop(self, name: str, grace_s: int = 10) -> bool:
        """Stop with a grace period; True if stopped by this call, False if already stopped."""
        status, _ = self._call(
            "POST",
            f"/containers/{_q(name)}/stop?t={int(grace_s)}",
            ok=(204, 304),
            timeout_s=self.timeout_s + grace_s,
        )
        return status == 204

    def top(self, name: str, ps_args: str = "-eo pid,comm,args") -> dict[str, Any]:
        query = urllib.parse.urlencode({"ps_args": ps_args})
        _, data = self._call("GET", f"/containers/{_q(name)}/top?{query}")
        return data if isinstance(data, dict) else {}

    # --- exec -------------------------------------------------------------------

    def exec_run(
        self,
        name: str,
        cmd: list[str],
        *,
        user: str,
        workdir: str,
        env: list[str],
        timeout_s: float,
        on_stdout: Callable[[bytes], None],
        on_stderr: Callable[[bytes], None],
    ) -> ExecResult:
        """Run ``cmd`` in the container and stream its output to the two sinks.

        ``timeout_s`` bounds the whole exchange on this side; the caller
        bounds the process itself (the ``timeout`` wrapper), so this is
        a backstop that turns a hung stream into a :class:`DockerError`
        rather than a hung session.
        """
        start = time.monotonic()
        _, created = self._call(
            "POST",
            f"/containers/{_q(name)}/exec",
            {
                "AttachStdin": False,
                "AttachStdout": True,
                "AttachStderr": True,
                "Tty": False,
                "Privileged": False,
                "User": user,
                "WorkingDir": workdir,
                "Env": env,
                "Cmd": cmd,
            },
            ok=(201,),
        )
        exec_id = created.get("Id") if isinstance(created, dict) else None
        if not isinstance(exec_id, str) or not exec_id:
            raise DockerError("docker API created no exec instance")
        out_total, err_total = self._stream_exec(exec_id, timeout_s, on_stdout, on_stderr)
        exit_code = self._exit_code(exec_id)
        return ExecResult(
            exit_code=exit_code,
            stdout_bytes=out_total,
            stderr_bytes=err_total,
            duration_s=time.monotonic() - start,
        )

    def _stream_exec(
        self,
        exec_id: str,
        timeout_s: float,
        on_stdout: Callable[[bytes], None],
        on_stderr: Callable[[bytes], None],
    ) -> tuple[int, int]:
        """POST /exec/{id}/start and demultiplex the raw stream until it closes.

        The daemon hijacks the connection: after the response head
        (``101`` when the upgrade is asked for, ``200`` from older
        daemons) the body is the raw multiplexed stream with no framing
        of HTTP's own, ending when the process does. ``http.client`` does
        not read a hijacked body, so this one exchange is spoken by hand.
        """
        body = json.dumps({"Detach": False, "Tty": False}).encode("utf-8")
        head = (
            f"POST /exec/{_q(exec_id)}/start HTTP/1.1\r\n"
            "Host: docker\r\n"
            "Content-Type: application/json\r\n"
            "Connection: Upgrade\r\n"
            "Upgrade: tcp\r\n"
            f"Content-Length: {len(body)}\r\n\r\n"
        ).encode("ascii")
        deadline = time.monotonic() + timeout_s
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        out_total = err_total = 0
        try:
            sock.settimeout(self.timeout_s)
            sock.connect(self.socket_path)
            sock.sendall(head + body)
            buffer = b""
            while b"\r\n\r\n" not in buffer:
                chunk = _recv(sock, deadline)
                if not chunk:
                    raise DockerError("docker API closed the exec stream before answering")
                buffer += chunk
            head_bytes, buffer = buffer.split(b"\r\n\r\n", 1)
            status = _status_of(head_bytes)
            if status not in (101, 200):
                rest = buffer
                while True:
                    chunk = _recv(sock, deadline)
                    if not chunk:
                        break
                    rest += chunk
                raise DockerError(_daemon_message(rest, status), status)
            while True:
                while len(buffer) >= _FRAME_HEADER.size:
                    stream, size = _FRAME_HEADER.unpack_from(buffer)
                    if len(buffer) < _FRAME_HEADER.size + size:
                        break
                    payload = buffer[_FRAME_HEADER.size : _FRAME_HEADER.size + size]
                    buffer = buffer[_FRAME_HEADER.size + size :]
                    if stream == _STREAM_STDOUT:
                        out_total += len(payload)
                        on_stdout(payload)
                    elif stream == _STREAM_STDERR:
                        err_total += len(payload)
                        on_stderr(payload)
                chunk = _recv(sock, deadline)
                if not chunk:
                    break
                buffer += chunk
        except DockerError:
            raise
        except TimeoutError:
            raise DockerError(
                f"the command's output stream did not end within {timeout_s:g} seconds"
            ) from None
        except OSError as exc:
            raise DockerError(f"docker API unreachable at {self.socket_path}: {exc}") from exc
        finally:
            sock.close()
        return out_total, err_total

    def _exit_code(self, exec_id: str) -> int | None:
        """The exec's exit code; polled briefly, since the stream can close first."""
        for _ in range(20):
            _, data = self._call("GET", f"/exec/{_q(exec_id)}/json")
            if isinstance(data, dict) and not data.get("Running"):
                code = data.get("ExitCode")
                return code if isinstance(code, int) else None
            time.sleep(0.05)
        return None


def _recv(sock: socket.socket, deadline: float) -> bytes:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    sock.settimeout(remaining)
    return sock.recv(_RECV_BYTES)


def _status_of(head: bytes) -> int:
    line = head.split(b"\r\n", 1)[0].decode("latin-1")
    parts = line.split(" ", 2)
    try:
        return int(parts[1])
    except (IndexError, ValueError):
        raise DockerError(f"docker API answered an unreadable status line: {line!r}") from None


def _daemon_message(raw: bytes, status: int | None) -> str:
    """The daemon's own error text (``{"message": …}``), else the raw body."""
    text = raw.decode("utf-8", errors="replace").strip()
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict) and isinstance(parsed.get("message"), str):
        text = parsed["message"]
    return text or f"docker API answered HTTP {status}"


def _q(part: str) -> str:
    return urllib.parse.quote(part, safe="")
