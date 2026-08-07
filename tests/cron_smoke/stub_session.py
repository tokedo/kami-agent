"""Cron-environment session driver: init + one full run-session, no provider keys.

Invoked by the CI cron-smoke job under a cron-like environment
(``env -i PATH=/usr/bin:/bin HOME=<explicit> sh -c ...``) using the
venv interpreter by absolute path. It drives the *real* CLI code paths
(`kami-agent init` then `run-session`) with exactly one substitution: a
scripted stub adapter replaces the provider adapter, so no provider key
is needed. The harness is the fake MCP stdio server fixture, spawned by
absolute interpreter path — cron resolves nothing via PATH.

A stand-in kami-lens daemon runs on a unix socket for the duration, so
the session-start brief (SPEC P1.12, D7) is exercised over its real wire
protocol here rather than only in unit tests — including under the
stripped cron environment, where a socket path resolved from ``HOME``
is exactly the kind of assumption this job exists to catch.

Exit code is the CLI's own; the workflow judges the printed markers
("initialized ...", "session_ran") and telemetry via check_telemetry.py,
never through a pipe.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
from pathlib import Path

import yaml

from kami_agent import cli
from kami_agent.adapters.base import AdapterResponse, StopReason, ToolCall, Usage

REPO_ROOT = Path(__file__).resolve().parents[2]

# What the stand-in daemon answers the roster query with: the compact
# shape, carrying no authored strings, exactly as the pinned daemon's
# checked-in schema describes it.
ROSTER_ENVELOPE = {
    "data": {
        "account": {"index": 4271, "roomIndex": 11},
        "kamis": [
            {"index": 1041, "state": "HARVESTING", "hp": [48, 60]},
            {"index": 1054, "state": "RESTING", "hp": [33, 81]},
        ],
    },
    "untrusted": [],
    "meta": {
        "servedAt": "2026-08-07T12:00:00.000Z",
        "blockNumber": 8814052,
        "stale": False,
        "mode": "daemon",
    },
}


class StubLensDaemon:
    """One-line-in / one-line-out unix socket server, as the daemon speaks."""

    def __init__(self, path: Path) -> None:
        self.path = str(path)
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self.path)
        self._server.listen(4)
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
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
                # The registry decides what a query name means; anything
                # else answers with an error, as the daemon would.
                if request.get("query") == "roster":
                    reply = {"id": request.get("id"), "ok": True, **ROSTER_ENVELOPE}
                else:
                    reply = {
                        "id": request.get("id"),
                        "ok": False,
                        "error": {"code": "BAD_ARGS", "message": "unknown query"},
                    }
                conn.sendall((json.dumps(reply) + "\n").encode("utf-8"))

    def close(self) -> None:
        self._server.close()


class StubAdapter:
    """Two-turn scripted session: read/write/harness-call, then wake + end."""

    def __init__(self) -> None:
        self._turn = 0

    def complete(self, system, messages, tools, params):
        self._turn += 1
        if self._turn == 1:
            calls = (
                ToolCall(id="c1", name="get_status", args={}),
                ToolCall(id="c2", name="echo", args={"text": "cron"}),
                # Bare path: exercises workspace-root-relative resolution.
                ToolCall(
                    id="c3",
                    name="workspace_write",
                    args={"path": "notes.md", "content": "cron smoke"},
                ),
            )
        elif self._turn == 2:
            calls = (
                ToolCall(id="c4", name="set_next_wake", args={"minutes_from_now": 30}),
                ToolCall(id="c5", name="end_session", args={"reason": "cron smoke complete"}),
            )
        else:
            raise AssertionError("stub adapter called after end_session")
        return AdapterResponse(
            text_blocks=(),
            tool_calls=calls,
            stop_reason=StopReason.TOOL_USE,
            usage=Usage(input_tokens=1200, output_tokens=60),
        )


def write_manifest(path: Path, lens_socket: str) -> None:
    manifest = {
        "run_id": "cron-smoke-001",
        "provider": "anthropic",  # never constructed: the stub adapter is injected
        "model": "stub-model",
        "price_table": {"input_usd_per_mtok": 1.0, "output_usd_per_mtok": 5.0},
        "params": {"max_tokens": 1024},
        "caps": {"session_token_cap": 100000},
        "budget_usd": 10.0,
        "harness": {
            # Absolute interpreter path: cron environments resolve nothing
            # via PATH — this is the defect class under test.
            "command": sys.executable,
            "args": [str(REPO_ROOT / "tests" / "unit" / "fake_mcp_server.py")],
            "cwd": str(REPO_ROOT),
            "handshake_timeout_s": 60,
        },
        # Pinned explicitly: left unset it would resolve from HOME, and the
        # point of this job is that nothing silently depends on the ambient
        # environment.
        "lens": {"socket_path": lens_socket, "timeout_s": 30},
        "pins": {"agent_sha": "", "harness_sha": "fixture", "gdd_sha": ""},
    }
    path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")


def main() -> int:
    run_dir = Path(sys.argv[1])
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = run_dir.parent / "manifest.yaml"
    daemon = StubLensDaemon(run_dir.parent / "lens.sock")
    write_manifest(manifest_path, daemon.path)
    (run_dir / "reference").mkdir(exist_ok=True)
    (run_dir / "reference" / "gdd.md").write_text("lore\n", encoding="utf-8")

    try:
        rc = cli.main(
            [
                "init",
                "--manifest",
                str(manifest_path),
                "--run-dir",
                str(run_dir),
                "--skip-connectivity",
            ]
        )
        if rc != 0:
            return rc
        cli.build_adapter = lambda manifest: StubAdapter()  # the one substitution
        return cli.main(["run-session", "--run-dir", str(run_dir), "--manual"])
    finally:
        daemon.close()


if __name__ == "__main__":
    sys.exit(main())
