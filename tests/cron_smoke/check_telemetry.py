"""Cron-smoke telemetry assertions: one clean session, expected end reason.

Run after stub_session.py against the same run directory, with the same
scaffold profile as its second argument. Fails (nonzero exit) unless the
telemetry stream holds exactly one session_start / session_end pair, the
session_end reason is an expected value, the profile's session-start
injections all ran exactly once and in order before the first model call,
every model request is paired with its outcome, and the agent-set wake was
scheduled. Every event is re-validated against the telemetry schema.
"""

from __future__ import annotations

import sys
from pathlib import Path

from kami_agent.loop import BALANCE_TOOL, BRIEF_TOOL, PLAN_PATH, PLAN_TOOL
from kami_agent.telemetry import read_events, validate_event

EXPECTED_END_REASONS = {"agent"}

# The session-start injections each profile performs, in order (SPEC
# P1.12): the roster from the daemon and the wallets' gas balances from the
# harness on every profile, plus the plan file on `planning`.
INJECTIONS = {
    "control": [(BRIEF_TOOL, "lens"), (BALANCE_TOOL, "harness")],
    "orientation": [(BRIEF_TOOL, "lens"), (BALANCE_TOOL, "harness")],
    "search": [(BRIEF_TOOL, "lens"), (BALANCE_TOOL, "harness")],
    "pushed": [(BRIEF_TOOL, "lens"), (BALANCE_TOOL, "harness")],
    "planning": [
        (BRIEF_TOOL, "lens"),
        (BALANCE_TOOL, "harness"),
        (PLAN_TOOL, "scaffold"),
    ],
}


def main() -> int:
    run_dir = Path(sys.argv[1])
    profile = sys.argv[2] if len(sys.argv) > 2 else "control"
    expected = INJECTIONS[profile]
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

    # The recorded rung must be the one that was asked for: the profile
    # decides the surface, the prompt and the injections below (SPEC D3).
    assert starts[0]["scaffold_profile"] == profile, (
        f"session_start recorded profile {starts[0]['scaffold_profile']!r}, ran {profile!r}"
    )

    # The session-start injections (SPEC P1.12, D1, D7): scaffold-initiated
    # reads issued before the first model call — the roster over the daemon's
    # real socket, the wallets' gas balances through the harness child, and on
    # the planning profile the agent's own plan file. Ordering is the claim
    # under test: an injection emitted after the first llm_call would have
    # missed the context it exists to seed.
    tool_calls = [e for e in events if e["event"] == "tool_call"]
    assert all("initiator" in e for e in tool_calls), "tool_call without initiator"
    assert all("call_seq" in e for e in tool_calls), "tool_call without call_seq"
    seqs = [e["call_seq"] for e in tool_calls]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), f"call_seq not 1:1: {seqs}"
    injections = [e for e in tool_calls if e["initiator"] == "scaffold"]
    got = [(e["tool"], e["source"]) for e in injections]
    assert got == expected, f"{profile}: injections {got}, expected {expected}"
    for event in injections:
        assert event["ok"] is True, f"{event['tool']} failed: {event.get('error')}"
    plan_rows = [e for e in injections if e["tool"] == PLAN_TOOL]
    assert all(e.get("path") == PLAN_PATH for e in plan_rows), "plan injection lost its path"
    kinds_before_first_llm = kinds[: kinds.index("llm_call")]
    assert kinds_before_first_llm.count("tool_call") == len(expected), (
        f"expected the {len(expected)} injections as the only tool_calls before the "
        f"first model call, got {kinds_before_first_llm}"
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
        f"{len(events)} events, profile={profile}, "
        f"session_end reason={ends[0]['reason']}, "
        f"injections={[e['tool'] for e in injections]} all ok, "
        f"next wake in {schedules[0]['clamped_min']:g} min"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
