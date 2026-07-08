"""State store: state.json cache and telemetry-fold recovery (SPEC §7.1).

state.json is a convenience CACHE. telemetry.jsonl is the source of
truth for all accounting: on recovery (SPEC §3 step 2) the cache is
rebuilt by folding the event stream, never trusted from disk.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

RUN_ACTIVE = "active"
RUN_COMPLETE = "complete"


@dataclass
class RunState:
    """The scaffold-owned cache fields (SPEC §7)."""

    session_counter: int = 0
    cumulative_usd: float = 0.0
    cumulative_tokens: int = 0
    next_wake_at: str | None = None
    run_status: str = RUN_ACTIVE
    first_session_at: str | None = None


def load_state(path: str | Path) -> RunState:
    """Read the cache; a missing file is a fresh run."""
    p = Path(path)
    if not p.exists():
        return RunState()
    return RunState(**json.loads(p.read_text(encoding="utf-8")))


def save_state(state: RunState, path: str | Path) -> None:
    """Write the cache atomically (tmp + fsync + rename)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(asdict(state), f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


def fold_telemetry(events: Iterable[dict[str, Any]]) -> RunState:
    """Rebuild the cache from the event stream — the source of truth (§7.1).

    - ``cumulative_usd`` / ``cumulative_tokens``: summed over every
      ``llm_call`` (failed-but-billed attempts appear as their own events;
      unknowable usage was logged at cost 0, SPEC §5.5).
    - ``session_counter``: highest session number seen (the counter is
      persisted before the first model call, so a crashed session still
      claims its number, SPEC §3 step 4).
    """
    state = RunState()
    for event in events:
        state.session_counter = max(state.session_counter, int(event.get("session", 0)))
        kind = event.get("event")
        if kind == "llm_call":
            state.cumulative_usd += event["cost_usd"]
            state.cumulative_tokens += event["input_tokens"] + event["output_tokens"]
        elif kind == "session_start" and state.first_session_at is None:
            state.first_session_at = event["ts"]
        elif kind == "schedule_next":
            state.next_wake_at = event["next_wake_at"]
        elif kind == "run_complete":
            state.run_status = RUN_COMPLETE
    return state


def crashed_session(events: Iterable[dict[str, Any]]) -> int | None:
    """Session number of a ``session_start`` with no matching ``session_end``.

    The lockfile serializes sessions, so at most one can be open; if the
    stream is somehow inconsistent, the highest open number is returned.
    """
    open_sessions: set[int] = set()
    for event in events:
        if event.get("event") == "session_start":
            open_sessions.add(event["session"])
        elif event.get("event") == "session_end":
            open_sessions.discard(event["session"])
    return max(open_sessions) if open_sessions else None


def session_totals(events: Iterable[dict[str, Any]], session: int) -> dict[str, Any]:
    """Fold one session's events into the ``session_end`` payload fields (§8).

    Used to write the synthetic ``session_end reason=crash`` during
    recovery (SPEC §3 step 2).
    """
    llm_calls = 0
    tool_calls = 0
    session_cost_usd = 0.0
    session_tokens = 0
    for event in events:
        if event.get("session") != session:
            continue
        if event.get("event") == "llm_call":
            llm_calls += 1
            session_cost_usd += event["cost_usd"]
            session_tokens += event["input_tokens"] + event["output_tokens"]
        elif event.get("event") == "tool_call":
            tool_calls += 1
    return {
        "llm_calls": llm_calls,
        "tool_calls": tool_calls,
        "session_cost_usd": session_cost_usd,
        "session_tokens": session_tokens,
    }
