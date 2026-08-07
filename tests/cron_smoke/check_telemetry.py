"""Cron-smoke telemetry assertions: one clean session, expected end reason.

Run after stub_session.py against the same run directory. Fails (nonzero
exit) unless the telemetry stream holds exactly one session_start /
session_end pair, the session_end reason is an expected value, the
session-start brief was issued exactly once against the stand-in
world-state daemon, every model request is paired with its outcome, and
the agent-set wake was scheduled. Every event is re-validated against the
telemetry schema.
"""

from __future__ import annotations

import sys
from pathlib import Path

from kami_agent.loop import BRIEF_TOOL
from kami_agent.telemetry import read_events, validate_event

EXPECTED_END_REASONS = {"agent"}


def main() -> int:
    run_dir = Path(sys.argv[1])
    events = list(read_events(run_dir / "telemetry.jsonl"))
    for event in events:
        validate_event(event)

    kinds = [e["event"] for e in events]
    starts = [e for e in events if e["event"] == "session_start"]
    ends = [e for e in events if e["event"] == "session_end"]
    assert kinds[0] == "run_start", f"first event {kinds[0]!r}, expected run_start"
    assert len(starts) == 1, f"expected exactly one session_start, got {len(starts)}"
    assert len(ends) == 1, f"expected exactly one session_end, got {len(ends)}"
    assert ends[0]["reason"] in EXPECTED_END_REASONS, (
        f"session_end reason {ends[0]['reason']!r} not in {sorted(EXPECTED_END_REASONS)}"
    )
    assert any(e["event"] == "llm_call" for e in events), "no llm_call recorded"

    # The session-start brief (SPEC P1.12, D7): one scaffold-initiated query
    # to the world-state daemon, issued before the first model call, which
    # succeeded over the real socket protocol. Ordering is the claim under
    # test — a brief emitted after the first llm_call would have missed the
    # context it exists to seed.
    tool_calls = [e for e in events if e["event"] == "tool_call"]
    assert all("initiator" in e for e in tool_calls), "tool_call without initiator"
    assert all("call_seq" in e for e in tool_calls), "tool_call without call_seq"
    seqs = [e["call_seq"] for e in tool_calls]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), f"call_seq not 1:1: {seqs}"
    briefs = [e for e in tool_calls if e["initiator"] == "scaffold"]
    assert len(briefs) == 1, f"expected exactly one scaffold-initiated call, got {len(briefs)}"
    assert briefs[0]["tool"] == BRIEF_TOOL, f"brief called {briefs[0]['tool']!r}"
    assert briefs[0]["source"] == "lens", "the brief must be a daemon query, not a harness call"
    assert briefs[0]["ok"] is True, f"brief failed: {briefs[0].get('error')}"
    kinds_before_first_llm = kinds[: kinds.index("llm_call")]
    assert kinds_before_first_llm.count("tool_call") == 1, (
        f"expected the brief as the only tool_call before the first model call, "
        f"got {kinds_before_first_llm}"
    )

    # Write-ahead pairing (SPEC P3): every model request that was sent has an
    # outcome row. An unpaired one in a clean session would mean the pairing
    # itself is broken, since nothing crashed here.
    requested = {e["request_seq"] for e in events if e["event"] == "llm_request"}
    completed = {e["request_seq"] for e in events if e["event"] == "llm_call"}
    assert requested, "no llm_request written ahead of any model call"
    assert requested == completed, f"unpaired model requests: {sorted(requested ^ completed)}"
    assert not [e for e in events if e.get("phantom")], "phantom row in a clean session"

    schedules = [e for e in events if e["event"] == "schedule_next"]
    assert len(schedules) == 1, f"expected exactly one schedule_next, got {len(schedules)}"
    assert schedules[0]["source"] == "agent", f"schedule source {schedules[0]['source']!r}"
    assert schedules[0]["clamped_min"] == 30.0

    print(
        "cron-smoke telemetry OK: "
        f"{len(events)} events, session_end reason={ends[0]['reason']}, "
        f"brief={briefs[0]['tool']} ok, "
        f"next wake in {schedules[0]['clamped_min']:g} min"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
