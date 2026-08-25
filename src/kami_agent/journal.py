"""Machine-written session journal: run/journal/sessions.jsonl (SPEC P15).

A session that acts but writes nothing makes the agent's own past self an
unknown actor: a successor finds its harvest stopped, a quest complete, an
item gained, and no record that it was the one who did it. And a provider
outage is invisible from inside — sessions resume with no sense that a day
passed, because nothing in the world says so.

So the scaffold writes one compact entry per session **regardless of what
the model writes**: what session it was, when it started and ended, how
long since the previous session ended, which tools it called and how
often, the transaction hashes its results carried, and the roster it was
shown at the start.

What this is not, and the boundaries are the whole design:

- **Facts only.** No advice, no summary, no judgement about what happened.
  The scaffold reports; interpretation is the agent's.
- **Never apparatus** (I1). No budget, spend, tokens, caps, or `t_max`,
  and — the one that is easy to get wrong — **never the reason a session
  ended**. P5's forced endings are silent, and a journal that named them
  would disclose through the back door exactly what the silence protects.
  Elapsed wall time is not apparatus: `get_status` already serves
  `current_time_utc`, so how much time passed is world-observable.
- **The agent's own calls only.** Tool counts cover `initiator: model`
  rows; the session-start injections are reads the agent did not choose
  (P9) and would read as things it did.
- **Derived, never authoritative.** `telemetry.jsonl` is the source of
  truth (P3, I7). This file is a view of it kept for the agent, which is
  what makes trimming it for retention legitimate.

**Retention.** The file is rolled to ``journal_max_bytes`` by dropping
whole oldest entries. The default is chosen against one number and not
arbitrarily: it is below ``tool_result_max_bytes``, so reading the WHOLE
journal in one call is never truncated. The agent can always see all of
what it kept.

**Tool counts and I1.** A session's executed-call total approaching the
same number every time would let an agent infer that a per-session tool
cap exists. It is recorded anyway, and the reason is that this is not a
new channel: an agent can already count its own calls inside a session
from its own context, so the journal saves it the bookkeeping rather than
telling it something it could not otherwise know. Recorded here so the
judgement is visible rather than implicit.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

JOURNAL_ROOT = "journal"
JOURNAL_FILENAME = "sessions.jsonl"

# The agent-facing path, workspace-root-relative like every other path the
# agent sees (P11).
JOURNAL_PATH = f"{JOURNAL_ROOT}/{JOURNAL_FILENAME}"

# Rolling byte bound (caps.journal_max_bytes). Below tool_result_max_bytes
# (65536) on purpose — see the retention note above.
DEFAULT_JOURNAL_MAX_BYTES = 32768

# Per-entry bound on transaction evidence: a session that submitted many
# transactions would otherwise push every earlier session out of the file
# by itself. The count is kept when the list is cut, so the entry never
# claims fewer transactions than there were.
MAX_TX_HASHES = 20


def journal_path(run_dir: str | Path) -> Path:
    return Path(run_dir) / JOURNAL_ROOT / JOURNAL_FILENAME


def build_entry(
    *,
    session: int,
    started_at: str,
    ended_at: str,
    previous_ended_at: str | None,
    tools: dict[str, int],
    tx_hashes: list[str],
    roster: Any | None = None,
) -> dict[str, Any]:
    """One journal entry. Facts only; no ending reason, no accounting.

    ``previous_ended_at`` None means there is no previous entry (the first
    session, or the first after the retention window dropped it), and the
    elapsed field is then absent rather than zero — zero would claim the
    sessions were adjacent.
    """
    entry: dict[str, Any] = {
        "session": session,
        "started_at": started_at,
        "ended_at": ended_at,
    }
    elapsed = _elapsed_seconds(previous_ended_at, started_at)
    if elapsed is not None:
        entry["seconds_since_previous_session_end"] = elapsed
    entry["tools"] = dict(sorted(tools.items()))
    kept = tx_hashes[:MAX_TX_HASHES]
    entry["tx_hashes"] = kept
    if len(tx_hashes) > len(kept):
        entry["tx_hashes_total"] = len(tx_hashes)
    if roster is not None:
        entry["roster"] = roster
    return entry


def _elapsed_seconds(previous_ended_at: str | None, started_at: str) -> float | None:
    if not previous_ended_at:
        return None
    from datetime import datetime

    try:
        previous = datetime.fromisoformat(previous_ended_at)
        start = datetime.fromisoformat(started_at)
    except ValueError:
        return None
    return round((start - previous).total_seconds(), 3)


def read_entries(run_dir: str | Path) -> list[dict[str, Any]]:
    """Every entry currently retained, oldest first. Unreadable lines are skipped."""
    path = journal_path(run_dir)
    if not path.is_file():
        return []
    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            entries.append(parsed)
    return entries


def last_entry_slice(run_dir: str | Path) -> tuple[int, int] | None:
    """Byte ``(offset, length)`` of the last entry's line, or None if empty.

    Byte-addressed so the session-start injection can be an ordinary
    ``workspace_read(path, offset, length)`` — the same call the agent can
    make itself, which is what keeps the injection out of X22's
    special-path exception.
    """
    path = journal_path(run_dir)
    if not path.is_file():
        return None
    raw = path.read_bytes()
    stripped = raw.rstrip(b"\n")
    if not stripped:
        return None
    start = stripped.rfind(b"\n") + 1
    return start, len(stripped) - start


def has_session(run_dir: str | Path, session: int) -> bool:
    """Is this session already journaled? (Recovery must be idempotent, P3.)"""
    return any(entry.get("session") == session for entry in read_entries(run_dir))


def last_ended_at(run_dir: str | Path) -> str | None:
    entries = read_entries(run_dir)
    if not entries:
        return None
    ended = entries[-1].get("ended_at")
    return ended if isinstance(ended, str) else None


def append(
    run_dir: str | Path,
    entry: dict[str, Any],
    *,
    max_bytes: int = DEFAULT_JOURNAL_MAX_BYTES,
) -> None:
    """Append one entry and roll the file to ``max_bytes``, oldest first."""
    path = journal_path(run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
    existing = path.read_bytes() if path.is_file() else b""
    data = existing + line.encode("utf-8")
    path.write_bytes(_roll(data, max_bytes))


def _roll(data: bytes, max_bytes: int) -> bytes:
    """Drop whole oldest lines until the file fits.

    The newest entry is always kept, even alone over the bound: a journal
    that dropped the session that just happened would be worse than a
    journal slightly over its size.
    """
    while len(data) > max_bytes:
        cut = data.find(b"\n")
        if cut == -1 or cut + 1 >= len(data):
            break
        data = data[cut + 1 :]
    return data
