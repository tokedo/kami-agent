"""A stand-in Docker Engine API daemon on a real unix socket (unit tier, no docker).

It speaks the seven endpoints the scaffold uses (``kami_agent.docker_api``)
over real HTTP/1.1, including the hijacked, multiplexed ``exec start``
stream, so the scaffold's own client code runs unmodified against it. What
it does NOT do is run anything in a container: each container is a
:class:`FakeContainer` whose ``on_exec`` callback decides what a command
prints and how it exits. What a real container does is the smoke tier's
to prove (``tests/smoke/test_environment_docker.py``).
"""

from __future__ import annotations

import json
import socket
import socketserver
import struct
import tempfile
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ExecCall:
    """One exec, as the scaffold asked for it."""

    container: str
    cmd: list[str]
    user: str
    workdir: str
    env: list[str]
    body: dict[str, Any]

    @property
    def wrapped(self) -> tuple[float | None, str | None]:
        """(timeout, command) when the call is the shell wrapper, else (None, None)."""
        cmd = self.cmd
        if len(cmd) == 7 and cmd[:3] == ["timeout", "-k", "5"] and cmd[4:6] == ["bash", "-c"]:
            return float(cmd[3]), cmd[6]
        return None, None


@dataclass
class ExecReply:
    stdout: bytes = b""
    stderr: bytes = b""
    exit_code: int | None = 0
    delay_s: float = 0.0


@dataclass
class FakeContainer:
    name: str
    running: bool = True
    restart_count: int = 0
    image: str = "kami-agent-env:0.8.0-shell"
    processes: list[list[str]] = field(
        default_factory=lambda: [
            ["101", "docker-init", "/sbin/docker-init -- sleep infinity"],
            ["102", "sleep", "sleep infinity"],
        ]
    )
    on_exec: Callable[[FakeContainer, ExecCall], ExecReply] | None = None
    starts: int = 0
    stops: int = 0

    def handle(self, call: ExecCall) -> ExecReply:
        if self.on_exec is None:
            return ExecReply()
        return self.on_exec(self, call)


class FakeDocker:
    """The daemon. Use as a context manager; ``socket_path`` is where it listens."""

    def __init__(self, *containers: FakeContainer) -> None:
        self.containers = {c.name: c for c in containers}
        self.requests: list[tuple[str, str, dict[str, Any] | None]] = []
        self.execs: list[ExecCall] = []
        self._exec_bodies: dict[str, tuple[str, dict[str, Any]]] = {}
        self._exit_codes: dict[str, int | None] = {}
        self._seq = 0
        self._lock = threading.Lock()
        self._dir = tempfile.mkdtemp(prefix="fkd")
        self.socket_path = str(Path(self._dir) / "docker.sock")
        self._server: socketserver.ThreadingUnixStreamServer | None = None

    # --- lifecycle --------------------------------------------------------------

    def __enter__(self) -> FakeDocker:
        fake = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                fake._serve(self.rfile, self.connection)

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

    # --- the HTTP side ------------------------------------------------------------

    def _serve(self, rfile: Any, conn: socket.socket) -> None:
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = rfile.read1(65536) if hasattr(rfile, "read1") else rfile.read(1)
            if not chunk:
                return
            head += chunk
        head, rest = head.split(b"\r\n\r\n", 1)
        lines = head.decode("latin-1").split("\r\n")
        method, target, _ = lines[0].split(" ", 2)
        headers = {
            k.strip().lower(): v.strip() for k, _, v in (line.partition(":") for line in lines[1:])
        }
        length = int(headers.get("content-length", "0"))
        while len(rest) < length:
            rest += rfile.read(length - len(rest))
        body = json.loads(rest) if rest else None
        parsed = urllib.parse.urlsplit(target)
        path = urllib.parse.unquote(parsed.path)
        query = urllib.parse.parse_qs(parsed.query)
        with self._lock:
            self.requests.append((method, target, body))
        parts = path.strip("/").split("/")
        if parts[0] == "exec" and len(parts) == 3 and parts[2] == "start" and method == "POST":
            self._exec_start(parts[1], conn)
            return
        status, payload = self._route(method, parts, query, body)
        self._reply(conn, status, payload)

    def _route(
        self, method: str, parts: list[str], query: dict[str, list[str]], body: Any
    ) -> tuple[int, Any]:
        if parts[0] == "containers" and len(parts) == 3:
            name, action = parts[1], parts[2]
            container = self.containers.get(name)
            if container is None:
                return 404, {"message": f"No such container: {name}"}
            if action == "json" and method == "GET":
                return 200, {
                    "Name": f"/{name}",
                    "State": {"Running": container.running},
                    "RestartCount": container.restart_count,
                    "Config": {"Image": container.image},
                    "Image": "sha256:" + "e" * 64,
                }
            if action == "start" and method == "POST":
                if container.running:
                    return 304, None
                container.running = True
                container.starts += 1
                return 204, None
            if action == "stop" and method == "POST":
                if not container.running:
                    return 304, None
                container.running = False
                container.stops += 1
                container.processes = []
                return 204, None
            if action == "top" and method == "GET":
                if not container.running:
                    return 409, {"message": f"Container {name} is not running"}
                return 200, {
                    "Titles": ["PID", "COMMAND", "COMMAND"],
                    "Processes": container.processes,
                }
            if action == "exec" and method == "POST":
                if not container.running:
                    return 409, {"message": f"Container {name} is not running"}
                with self._lock:
                    self._seq += 1
                    exec_id = f"exec{self._seq:04d}"
                    self._exec_bodies[exec_id] = (name, body or {})
                return 201, {"Id": exec_id}
        if parts[0] == "exec" and len(parts) == 3 and parts[2] == "json":
            return 200, {"Running": False, "ExitCode": self._exit_codes.get(parts[1])}
        return 404, {"message": "page not found"}

    def _exec_start(self, exec_id: str, conn: socket.socket) -> None:
        name, body = self._exec_bodies[exec_id]
        call = ExecCall(
            container=name,
            cmd=list(body.get("Cmd") or []),
            user=str(body.get("User")),
            workdir=str(body.get("WorkingDir")),
            env=list(body.get("Env") or []),
            body=body,
        )
        with self._lock:
            self.execs.append(call)
        reply = self.containers[name].handle(call)
        self._exit_codes[exec_id] = reply.exit_code
        conn.sendall(
            b"HTTP/1.1 101 UPGRADED\r\nContent-Type: application/vnd.docker.raw-stream\r\n"
            b"Connection: Upgrade\r\nUpgrade: tcp\r\n\r\n"
        )
        if reply.delay_s:
            time.sleep(reply.delay_s)
        for stream, data in ((1, reply.stdout), (2, reply.stderr)):
            for i in range(0, len(data), 4096):
                piece = data[i : i + 4096]
                conn.sendall(struct.pack(">BxxxL", stream, len(piece)) + piece)
        conn.shutdown(socket.SHUT_WR)

    @staticmethod
    def _reply(conn: socket.socket, status: int, payload: Any) -> None:
        raw = b"" if payload is None else json.dumps(payload).encode("utf-8")
        reason = {200: "OK", 201: "Created", 204: "No Content", 304: "Not Modified"}.get(
            status, "Error"
        )
        head = (
            f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(raw)}\r\nConnection: close\r\n\r\n"
        ).encode("latin-1")
        conn.sendall(head + raw)


def scripted(table: dict[str, ExecReply]) -> Callable[[FakeContainer, ExecCall], ExecReply]:
    """An ``on_exec`` that answers by command text (the shell wrapper unwrapped)."""

    def on_exec(container: FakeContainer, call: ExecCall) -> ExecReply:
        _, command = call.wrapped
        key = command if command is not None else " ".join(call.cmd)
        for pattern, reply in table.items():
            if pattern in key:
                return reply
        return ExecReply(stderr=b"bash: command not found\n", exit_code=127)

    return on_exec
