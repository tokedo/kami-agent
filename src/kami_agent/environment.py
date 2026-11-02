"""The environment container of the ``shell`` rungs (SPEC D8, P10, P1).

On ``shell`` and ``shell_keeper`` the agent has a persistent machine of
its own: one container per run, created by the deployment
(``deploy/environment/``), alive across every session, stopped at
``run_complete``. Its ``/workspace`` is the run directory's
``workspace/`` — the same tree the ``workspace_*`` tools serve — so files
are one filesystem seen two ways. It has no network and holds no key.

This module is the scaffold's side of it, over the Docker Engine API
(:mod:`kami_agent.docker_api`):

- :meth:`Environment.shell` — one ``shell`` tool call: ``docker exec``
  as uid 1000 in ``/workspace`` under ``timeout -k 5 <t> bash -c``; the
  two streams capped separately for the result and kept whole in the
  run directory's ``shell/`` log (P10);
- :meth:`Environment.snapshot` — the record at each session boundary
  (``shell_state``, P9): running, restarted, the process table, the
  socket probes. Started again here if it stopped (a host reboot, an
  OOM kill);
- :meth:`Environment.bring_up_checks` — ``init``'s assertions (P12):
  a mismatch between the manifest's rung and the container starts no run;
- :meth:`Environment.stop` — the end of the run (``run_complete``): no
  process the agent left behind survives it.

Nothing here reaches the model except the ``shell`` result text and the
docker daemon's own words when a call fails.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kami_agent.docker_api import DockerClient, DockerError
from kami_agent.tools.truncation import truncation_marker

# The environment's user, working directory and home (SPEC D8): the image
# creates uid 1000, and HOME is the workspace so what a tool writes "at
# home" persists with it.
SHELL_USER = "1000:1000"
SHELL_WORKDIR = "/workspace"
SHELL_ENV = ["HOME=/workspace"]

# The wrapper's kill grace: `timeout -k 5 <t>` sends TERM at t and KILL
# five seconds later. 124 = the command was TERMed at t; 137 = it was
# KILLed after the grace (128 + 9).
KILL_GRACE_S = 5
TIMED_OUT_EXIT_CODES = (124, 137)

# Upper bound of one call's timeout, the manifest's caps.shell_timeout_s.
DEFAULT_SHELL_TIMEOUT_S = 120.0
MIN_SHELL_TIMEOUT_S = 1.0

# The run-dir internal the full output lands in (P12): never mounted into
# the environment, pulled at close-out.
SHELL_LOG_DIR = "shell"

# What one stream may put on disk per call. The result the model sees is
# capped far below this; the log exists to keep what the result cut, and
# this bound exists so one command that writes forever cannot fill the
# run's disk. The true total is always recorded (stdout_bytes /
# stderr_bytes), so a cut log is visible as one.
SHELL_LOG_MAX_BYTES = 32 * 1024 * 1024

# Room kept for the result's header lines and the two truncation markers,
# so the two halves never meet the loop's own tool-result cap.
SHELL_RESULT_RESERVE_BYTES = 1024

# The stop grace at the end of the run (`docker stop -t 10`).
STOP_GRACE_S = 10

# The process table on shell_state (P9): bounded, each args string cut.
TOP_PS_ARGS = "-eo pid,comm,args"
TOP_MAX_PROCESSES = 50
TOP_ARGS_MAX_CHARS = 200

# Socket probes, from INSIDE the environment (as the agent would reach
# them). The lens CLI's `health` prints the daemon's state, or NO_DAEMON /
# TIMEOUT / ERROR when nothing answered; the keeper CLI exits 0 or 1 when
# a frame came back and 3 when no keeper is there.
LENS_PROBE = ["sh", "-c", "test -S /srv/lens/kami-lens.sock && kami-lens health"]
LENS_PROBE_UNANSWERED = frozenset({"", "NO_DAEMON", "TIMEOUT", "ERROR"})
KEEPER_PROBE = ["keeper", "status"]
KEEPER_PROBE_ANSWERED = (0, 1)
KEEPER_PRESENT_PROBE = ["sh", "-c", "command -v keeper"]
PROBE_TIMEOUT_S = 40

# The keeper profile the shell_keeper rung requires (the keeper's SPEC P10).
KEEPER_PROFILE = "lab"


class EnvironmentCheckError(Exception):
    """A bring-up assertion failed: the run must not start (P12)."""


@dataclass(frozen=True, slots=True)
class ShellResult:
    """One finished ``shell`` call: the agent-visible text and its record."""

    text: str
    exit_code: int | None
    timed_out: bool
    stdout_bytes: int
    stderr_bytes: int
    log_rel: str
    duration_s: float


@dataclass
class Snapshot:
    """The environment at one session boundary (``shell_state``, P9)."""

    running: bool
    restarted: bool
    process_count: int
    restart_count: int | None = None
    image: str | None = None
    processes: list[dict[str, Any]] = field(default_factory=list)
    lens_socket_ok: bool | None = None
    keeper_socket_ok: bool | None = None

    def fields(self, phase: str, container: str, workspace_bytes: int) -> dict[str, Any]:
        out: dict[str, Any] = {
            "phase": phase,
            "container": container,
            "running": self.running,
            "restarted": self.restarted,
            "process_count": self.process_count,
            "workspace_bytes": workspace_bytes,
            "processes": list(self.processes),
        }
        if self.restart_count is not None:
            out["restart_count"] = self.restart_count
        if self.lens_socket_ok is not None:
            out["lens_socket_ok"] = self.lens_socket_ok
        if self.keeper_socket_ok is not None:
            out["keeper_socket_ok"] = self.keeper_socket_ok
        return out


class _StreamSink:
    """One stream of one call: the head for the result, the body for the log."""

    def __init__(self, path: Path, head_bytes: int) -> None:
        self.path = path
        self.head_bytes = head_bytes
        self.head = bytearray()
        self.logged = 0
        self._file = path.open("wb")

    def __call__(self, chunk: bytes) -> None:
        if len(self.head) < self.head_bytes:
            self.head.extend(chunk[: self.head_bytes - len(self.head)])
        room = SHELL_LOG_MAX_BYTES - self.logged
        if room > 0:
            part = chunk[:room]
            self._file.write(part)
            self.logged += len(part)

    def close(self) -> None:
        self._file.close()


class Environment:
    """The run's environment container, as the scaffold drives it."""

    def __init__(
        self,
        docker: DockerClient,
        container: str,
        run_dir: str | Path,
        *,
        keeper_rung: bool = False,
        keeper_container: str | None = None,
    ) -> None:
        self.docker = docker
        self.container = container
        self.run_dir = Path(run_dir)
        self.keeper_rung = keeper_rung
        self.keeper_container = keeper_container

    # --- the shell tool (P10) -----------------------------------------------------

    def shell(
        self,
        command: str,
        timeout_s: float,
        *,
        session: int,
        call_seq: int,
        result_max_bytes: int,
    ) -> ShellResult:
        """Run one command; raise :class:`DockerError` if docker could not.

        A command that RAN is a result whatever its exit code: a non-zero
        exit is the agent's command speaking, not the scaffold failing.
        """
        log_dir = self.run_dir / SHELL_LOG_DIR
        log_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{session:03d}-{call_seq:04d}"
        log_path = log_dir / f"{stem}.log"
        half = max(0, (result_max_bytes - SHELL_RESULT_RESERVE_BYTES) // 2)
        out = _StreamSink(log_dir / f"{stem}.stdout.part", half)
        err = _StreamSink(log_dir / f"{stem}.stderr.part", half)
        cmd = ["timeout", "-k", str(KILL_GRACE_S), f"{timeout_s:g}", "bash", "-c", command]
        try:
            result = self.docker.exec_run(
                self.container,
                cmd,
                user=SHELL_USER,
                workdir=SHELL_WORKDIR,
                env=list(SHELL_ENV),
                timeout_s=timeout_s + KILL_GRACE_S + 5,
                on_stdout=out,
                on_stderr=err,
            )
        except DockerError:
            out.close()
            err.close()
            for part in (out.path, err.path):
                part.unlink(missing_ok=True)
            raise
        out.close()
        err.close()
        timed_out = result.exit_code in TIMED_OUT_EXIT_CODES and result.duration_s >= timeout_s
        header = [
            f"exit_code: {_exit_text(result.exit_code)}",
            f"timed_out: {'true' if timed_out else 'false'}",
            f"duration_s: {result.duration_s:.1f}",
        ]
        text = "\n".join(
            [
                *header,
                f"--- stdout ({result.stdout_bytes} bytes) ---",
                _stream_text(bytes(out.head), result.stdout_bytes, half),
                f"--- stderr ({result.stderr_bytes} bytes) ---",
                _stream_text(bytes(err.head), result.stderr_bytes, half),
            ]
        )
        self._write_log(log_path, command, timeout_s, header, result, out, err)
        return ShellResult(
            text=text,
            exit_code=result.exit_code,
            timed_out=timed_out,
            stdout_bytes=result.stdout_bytes,
            stderr_bytes=result.stderr_bytes,
            log_rel=str(log_path.relative_to(self.run_dir)),
            duration_s=result.duration_s,
        )

    def _write_log(
        self,
        log_path: Path,
        command: str,
        timeout_s: float,
        header: list[str],
        result: Any,
        out: _StreamSink,
        err: _StreamSink,
    ) -> None:
        with log_path.open("wb") as log:
            log.write(f"--- command (timeout_s {timeout_s:g}) ---\n".encode())
            log.write(command.encode("utf-8", errors="replace") + b"\n")
            log.write(("\n".join(header) + "\n").encode())
            for name, sink, total in (
                ("stdout", out, result.stdout_bytes),
                ("stderr", err, result.stderr_bytes),
            ):
                cut = "" if sink.logged >= total else f", the first {sink.logged} kept"
                log.write(f"--- {name} ({total} bytes{cut}) ---\n".encode())
                with sink.path.open("rb") as part:
                    while chunk := part.read(1 << 20):
                        log.write(chunk)
                log.write(b"\n")
                sink.path.unlink(missing_ok=True)

    # --- session boundaries (P1, P9 shell_state) ----------------------------------

    def snapshot(
        self,
        *,
        last_restart_count: int | None,
        start_if_stopped: bool = True,
        probe: bool = True,
    ) -> Snapshot:
        """Inspect, start if needed, list processes, probe the sockets.

        Never raises: an unreadable docker API records the container as
        not running (nothing could confirm it was), and the session
        proceeds — the ``shell`` tool then fails visibly on its own.
        """
        started = False
        try:
            info = self.docker.inspect(self.container)
            if not _running(info) and start_if_stopped:
                started = self.docker.start(self.container)
                info = self.docker.inspect(self.container)
        except DockerError:
            return Snapshot(running=False, restarted=started, process_count=0)
        running = _running(info)
        restart_count = info.get("RestartCount")
        restart_count = restart_count if isinstance(restart_count, int) else None
        moved = (
            last_restart_count is not None
            and restart_count is not None
            and restart_count != last_restart_count
        )
        snap = Snapshot(
            running=running,
            restarted=started or moved,
            process_count=0,
            restart_count=restart_count,
            image=_image_of(info),
        )
        if running:
            snap.processes, snap.process_count = self._processes()
            if probe:
                snap.lens_socket_ok = self._lens_answers()
                if self.keeper_rung:
                    code, _ = self._probe(KEEPER_PROBE)
                    snap.keeper_socket_ok = code in KEEPER_PROBE_ANSWERED
        return snap

    def _processes(self) -> tuple[list[dict[str, Any]], int]:
        try:
            top = self.docker.top(self.container, TOP_PS_ARGS)
        except DockerError:
            return [], 0
        rows = top.get("Processes")
        rows = rows if isinstance(rows, list) else []
        listed: list[dict[str, Any]] = []
        for row in rows[:TOP_MAX_PROCESSES]:
            if not isinstance(row, list) or len(row) < 3:
                continue
            try:
                pid = int(row[0])
            except (TypeError, ValueError):
                continue
            listed.append(
                {"pid": pid, "comm": str(row[1]), "args": str(row[2])[:TOP_ARGS_MAX_CHARS]}
            )
        return listed, len(rows)

    def _probe(self, cmd: list[str]) -> tuple[int | None, str]:
        """Run a probe as the agent's user; (exit code, stdout). Never raises."""
        chunks: list[bytes] = []
        wrapped = ["timeout", "-k", "2", str(PROBE_TIMEOUT_S), *cmd]
        try:
            result = self.docker.exec_run(
                self.container,
                wrapped,
                user=SHELL_USER,
                workdir=SHELL_WORKDIR,
                env=list(SHELL_ENV),
                timeout_s=PROBE_TIMEOUT_S + 5,
                on_stdout=chunks.append,
                on_stderr=lambda _chunk: None,
            )
        except DockerError:
            return None, ""
        return result.exit_code, b"".join(chunks).decode("utf-8", errors="replace")

    def _lens_answers(self) -> bool:
        _, stdout = self._probe(LENS_PROBE)
        lines = stdout.strip().splitlines()
        state = lines[-1].strip() if lines else ""
        return state not in LENS_PROBE_UNANSWERED

    # --- bring-up (P12) ------------------------------------------------------------

    def bring_up_checks(self, *, keeper_sha: str | None) -> list[str]:
        """``init``'s assertions at ≥ ``shell``; raises on any mismatch.

        The I30 pattern applied to the environment: the manifest's rung
        and the container must agree before a run exists. The plain
        ``shell`` rung must not even have a keeper client — the control
        arm of the keeper must not learn that one exists.
        """
        lines: list[str] = []
        try:
            info = self.docker.inspect(self.container)
        except DockerError as exc:
            raise EnvironmentCheckError(
                f"environment container {self.container!r}: {exc.message}"
            ) from exc
        if not _running(info):
            raise EnvironmentCheckError(f"environment container {self.container!r} is not running")
        lines.append(f"environment ok ({self.container}, image {_image_of(info)})")
        code, _ = self._probe(KEEPER_PRESENT_PROBE)
        if self.keeper_rung and code != 0:
            raise EnvironmentCheckError(
                "scaffold_profile shell_keeper needs the keeper command in the environment, "
                f"and {self.container!r} has none (an image built for the shell rung?)"
            )
        if not self.keeper_rung and code == 0:
            raise EnvironmentCheckError(
                "scaffold_profile shell must not have a keeper command in the environment, "
                f"and {self.container!r} has one (an image built for shell_keeper?)"
            )
        if self.keeper_rung:
            lines.append(self._keeper_checks(keeper_sha))
        else:
            lines.append("environment: no keeper command (rung shell)")
        if not self._lens_answers():
            raise EnvironmentCheckError(
                "kami-lens did not answer from inside the environment "
                "(test -S /srv/lens/kami-lens.sock && kami-lens health)"
            )
        lines.append("environment: kami-lens answers from inside")
        return lines

    def _keeper_checks(self, keeper_sha: str | None) -> str:
        code, stdout = self._probe(KEEPER_PROBE)
        try:
            frame = json.loads(stdout)
        except ValueError:
            frame = None
        meta = frame.get("meta") if isinstance(frame, dict) else None
        profile = meta.get("profile") if isinstance(meta, dict) else None
        if code != 0 or not isinstance(frame, dict) or not frame.get("ok"):
            raise EnvironmentCheckError(
                f"keeper status from inside the environment did not answer ok (exit {code})"
            )
        if profile != KEEPER_PROFILE:
            raise EnvironmentCheckError(
                f"the keeper answers with profile {profile!r}; "
                f"shell_keeper needs {KEEPER_PROFILE!r}"
            )
        if not keeper_sha:
            raise EnvironmentCheckError("scaffold_profile shell_keeper needs pins.keeper_sha")
        if not self.keeper_container:
            raise EnvironmentCheckError(
                "scaffold_profile shell_keeper needs environment.keeper_container"
            )
        try:
            image = _image_of(self.docker.inspect(self.keeper_container)) or ""
        except DockerError as exc:
            raise EnvironmentCheckError(
                f"keeper container {self.keeper_container!r}: {exc.message}"
            ) from exc
        if not image_matches_pin(image, keeper_sha):
            raise EnvironmentCheckError(
                f"keeper container {self.keeper_container!r} runs image {image!r}, "
                f"which does not name pins.keeper_sha {keeper_sha!r}"
            )
        version = meta.get("keeperVersion") if isinstance(meta, dict) else None
        return f"keeper ok (profile {profile}, version {version}, image {image})"

    # --- the end of the run (P1, run_complete) -------------------------------------

    def stop(self) -> bool:
        """``docker stop`` with the grace period; True once it is stopped."""
        try:
            self.docker.stop(self.container, STOP_GRACE_S)
        except DockerError:
            return False
        return True


def clamp_shell_timeout(requested: Any, upper: float) -> float:
    """``timeout_s`` clamped to ``[1, upper]``; absent means the upper bound."""
    if requested is None:
        return float(upper)
    value = float(requested)
    if math.isnan(value) or math.isinf(value):
        raise ValueError("timeout_s must be a finite number")
    return min(max(value, MIN_SHELL_TIMEOUT_S), float(upper))


def image_matches_pin(image: str, pin: str) -> bool:
    """Does an image reference's tag name the pinned commit?

    The deployment tags the keeper image with its commit
    (``kami-keeper:<sha>``); a pin may be short or full, so one must be a
    prefix of the other, and both at least seven characters.
    """
    reference = image.split("@", 1)[0]
    tag = reference.rsplit(":", 1)[1] if ":" in reference.rsplit("/", 1)[-1] else ""
    pin = pin.strip().lower()
    tag = tag.strip().lower()
    if len(pin) < 7 or len(tag) < 7:
        return False
    return tag.startswith(pin) or pin.startswith(tag)


def _running(info: dict[str, Any]) -> bool:
    state = info.get("State")
    return bool(state.get("Running")) if isinstance(state, dict) else False


def _image_of(info: dict[str, Any]) -> str | None:
    config = info.get("Config")
    image = config.get("Image") if isinstance(config, dict) else None
    if isinstance(image, str) and image:
        return image
    fallback = info.get("Image")
    return fallback if isinstance(fallback, str) and fallback else None


def _exit_text(code: int | None) -> str:
    return str(code) if code is not None else "unknown"


def _stream_text(head: bytes, total: int, cap: int) -> str:
    if total <= cap:
        return head.decode("utf-8", errors="replace")
    return head[:cap].decode("utf-8", errors="ignore") + truncation_marker(cap, total)
