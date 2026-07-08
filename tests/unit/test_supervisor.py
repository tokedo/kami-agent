"""Supervisor: PID+age lockfile with staleness breaking, cron installer (SPEC §2)."""

import json
from datetime import UTC, datetime, timedelta

from kami_agent.supervisor import (
    CRON_TAG,
    acquire_lock,
    cron_line,
    install_cron,
    release_lock,
    uninstall_cron,
)

NOW = datetime(2026, 7, 8, 12, 0, 0, tzinfo=UTC)


def clock_at(dt):
    return lambda: dt


def test_acquire_and_release(tmp_path):
    lock = tmp_path / "run.lock"
    assert acquire_lock(lock, stale_s=100, clock=clock_at(NOW)) is True
    data = json.loads(lock.read_text())
    assert data["created"] == NOW.isoformat()
    assert data["pid"] > 0
    release_lock(lock)
    assert not lock.exists()
    release_lock(lock)  # idempotent


def test_live_lock_is_respected(tmp_path):
    lock = tmp_path / "run.lock"
    assert acquire_lock(lock, stale_s=100, clock=clock_at(NOW))  # our own live pid
    assert acquire_lock(lock, stale_s=100, clock=clock_at(NOW)) is False


def test_dead_pid_lock_is_broken(tmp_path):
    lock = tmp_path / "run.lock"
    # PID 2**22-ish beyond typical ranges; craft a definitely-dead pid by
    # using an impossible value the kill probe rejects.
    lock.write_text(json.dumps({"pid": 2**30, "created": NOW.isoformat()}))
    assert acquire_lock(lock, stale_s=100_000, clock=clock_at(NOW)) is True


def test_age_stale_lock_is_broken(tmp_path):
    lock = tmp_path / "run.lock"
    old = (NOW - timedelta(hours=3)).isoformat()
    lock.write_text(json.dumps({"pid": 1, "created": old}))  # pid 1 is alive (launchd/init)
    assert acquire_lock(lock, stale_s=7200, clock=clock_at(NOW)) is True


def test_fresh_lock_of_live_foreign_pid_is_respected(tmp_path):
    lock = tmp_path / "run.lock"
    lock.write_text(json.dumps({"pid": 1, "created": NOW.isoformat()}))
    assert acquire_lock(lock, stale_s=7200, clock=clock_at(NOW)) is False


def test_corrupt_lock_is_broken(tmp_path):
    lock = tmp_path / "run.lock"
    lock.write_text("not json at all")
    assert acquire_lock(lock, stale_s=100, clock=clock_at(NOW)) is True


# --- cron ----------------------------------------------------------------------


class FakeCrontab:
    def __init__(self, initial=""):
        self.table = initial

    def read(self):
        return self.table

    def write(self, table):
        self.table = table


def test_cron_line_shape():
    line = cron_line("kami-agent run-session --run-dir /srv/run", 5)
    assert line.startswith("*/5 * * * * kami-agent run-session")
    assert line.endswith(CRON_TAG)


def test_install_preserves_other_entries_and_replaces_own(tmp_path):
    tab = FakeCrontab("0 4 * * * /usr/local/bin/backup\n")
    install_cron("cmd-a", 5, read_crontab=tab.read, write_crontab=tab.write)
    install_cron("cmd-b", 10, read_crontab=tab.read, write_crontab=tab.write)
    lines = tab.table.splitlines()
    assert lines[0] == "0 4 * * * /usr/local/bin/backup"
    assert sum(CRON_TAG in line for line in lines) == 1
    assert "*/10 * * * * cmd-b" in lines[1]


def test_uninstall_removes_only_own_entry():
    tab = FakeCrontab()
    install_cron("cmd", read_crontab=tab.read, write_crontab=tab.write)
    tab.table = "0 4 * * * backup\n" + tab.table
    uninstall_cron(read_crontab=tab.read, write_crontab=tab.write)
    assert tab.table == "0 4 * * * backup\n"
    # Uninstalling an empty table is fine.
    empty = FakeCrontab()
    uninstall_cron(read_crontab=empty.read, write_crontab=empty.write)
    assert empty.table == ""
