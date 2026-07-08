"""Supervisor: fixed-cadence cron installer and PID+age lockfile (SPEC §2).

The supervisor is a fixed-cadence poller: cron fires every
``poll_cadence`` (default 5 min); the runner exits immediately unless
``now >= next_wake_at`` and no other session holds the lock. Effective
wake resolution is therefore ``poll_cadence`` (``wake_min >= poll_cadence``).

A lock whose PID is dead or whose age exceeds ``lock_stale_s`` (default
2x the session wall-clock ceiling) is stale and is broken with a logged
warning — a crashed session can never deadlock the run.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_POLL_CADENCE_MIN = 5
DEFAULT_LOCK_STALE_S = 7200.0  # 2 x a one-hour session wall-clock ceiling
CRON_TAG = "# kami-agent-supervisor"

LOCK_FILENAME = "run.lock"


def acquire_lock(
    path: str | Path,
    *,
    stale_s: float = DEFAULT_LOCK_STALE_S,
    clock: Callable[[], datetime] | None = None,
    pid: int | None = None,
) -> bool:
    """Take the run lock; False if a live session holds it.

    A lock is stale — and is broken with a logged warning — when its PID
    is dead, its age exceeds ``stale_s``, or its content is unreadable.
    """
    lock_path = Path(path)
    now = (clock or (lambda: datetime.now(UTC)))()
    if lock_path.exists():
        holder = _read_lock(lock_path)
        if holder is not None:
            held_pid, created = holder
            age_s = (now - created).total_seconds()
            if _pid_alive(held_pid) and age_s <= stale_s:
                return False
            logger.warning(
                "breaking stale lock %s (pid=%s alive=%s age=%.0fs stale_after=%.0fs)",
                lock_path,
                held_pid,
                _pid_alive(held_pid),
                age_s,
                stale_s,
            )
        else:
            logger.warning("breaking unreadable lock %s", lock_path)
        lock_path.unlink(missing_ok=True)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps({"pid": pid if pid is not None else os.getpid(), "created": now.isoformat()}),
        encoding="utf-8",
    )
    return True


def release_lock(path: str | Path) -> None:
    Path(path).unlink(missing_ok=True)


def _read_lock(path: Path) -> tuple[int, datetime] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return int(data["pid"]), datetime.fromisoformat(data["created"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OverflowError, ValueError):
        return False
    return True


# --- cron ---------------------------------------------------------------------


def cron_line(command: str, poll_cadence_min: int = DEFAULT_POLL_CADENCE_MIN) -> str:
    return f"*/{poll_cadence_min} * * * * {command} {CRON_TAG}"


def install_cron(
    command: str,
    poll_cadence_min: int = DEFAULT_POLL_CADENCE_MIN,
    *,
    read_crontab: Callable[[], str] | None = None,
    write_crontab: Callable[[str], None] | None = None,
) -> str:
    """Install (or replace) the supervisor entry in the user's crontab."""
    read = read_crontab or _read_crontab
    write = write_crontab or _write_crontab
    kept = [line for line in read().splitlines() if CRON_TAG not in line]
    kept.append(cron_line(command, poll_cadence_min))
    table = "\n".join(kept) + "\n"
    write(table)
    return table


def uninstall_cron(
    *,
    read_crontab: Callable[[], str] | None = None,
    write_crontab: Callable[[str], None] | None = None,
) -> str:
    """Remove the supervisor entry (SPEC §3 step 3: disable on run_complete)."""
    read = read_crontab or _read_crontab
    write = write_crontab or _write_crontab
    kept = [line for line in read().splitlines() if CRON_TAG not in line]
    table = ("\n".join(kept) + "\n") if kept else ""
    write(table)
    return table


def _read_crontab() -> str:
    proc = subprocess.run(["crontab", "-l"], capture_output=True, text=True, check=False)
    return proc.stdout if proc.returncode == 0 else ""


def _write_crontab(table: str) -> None:
    subprocess.run(["crontab", "-"], input=table, text=True, check=True)
