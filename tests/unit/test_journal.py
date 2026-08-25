"""Machine-written session journal: entry shape, retention, injection (SPEC P15, P1.12).

What makes this worth testing precisely is not the file format — it is
the two boundaries. The journal must say enough that an agent's past self
stops being an unknown actor and a long gap becomes perceptible, and it
must say nothing about the apparatus: no ending reason, no accounting,
no caps. Both halves are asserted here.
"""

import json

import pytest

from kami_agent import journal
from kami_agent.adapters.base import (
    AdapterResponse,
    AssistantMessage,
    SamplingParams,
    StopReason,
    ToolCall,
    ToolResultMessage,
    Usage,
)
from kami_agent.governor import PriceTable
from kami_agent.journal import JOURNAL_PATH
from kami_agent.loop import JOURNAL_CALL_ID, JOURNAL_TOOL, AgentLoop, LoopCaps
from kami_agent.telemetry import TelemetryWriter, read_events
from kami_agent.tools.scaffold import ScaffoldTools

PRICES = PriceTable(input_usd_per_mtok=3.0, output_usd_per_mtok=15.0)
PARAMS = SamplingParams(max_tokens=4096)


@pytest.fixture
def run_dir(tmp_path):
    (tmp_path / "workspace").mkdir()
    return tmp_path


def response(*tool_calls):
    return AdapterResponse(
        text_blocks=(),
        tool_calls=tuple(tool_calls),
        stop_reason=StopReason.TOOL_USE if tool_calls else StopReason.END_TURN,
        usage=Usage(input_tokens=10, output_tokens=5),
    )


def end_call():
    return ToolCall(id="t-end", name="end_session", args={"reason": "done"})


class ScriptedAdapter:
    def __init__(self, *responses):
        self._responses = list(responses)
        self.requests = []

    def complete(self, system, messages, tools, params):
        self.requests.append(list(messages))
        return self._responses.pop(0)


def make_loop(run_dir, adapter, *, session=1):
    return AgentLoop(
        adapter=adapter,
        model="test-model",
        system="system prompt",
        kickoff_text="Session start.",
        continuation_text="Continue.",
        scaffold=ScaffoldTools(run_dir, session_number=session),
        game=None,
        telemetry=TelemetryWriter(run_dir / "telemetry.jsonl", run_id="test-run"),
        session=session,
        params=PARAMS,
        prices=PRICES,
        caps=LoopCaps(session_token_cap=100_000),
        sleep=lambda s: None,
    )


# --- the entry (P15) ----------------------------------------------------------


def test_the_entry_carries_the_facts_a_successor_needs():
    entry = journal.build_entry(
        session=7,
        started_at="2026-08-25T12:00:00+00:00",
        ended_at="2026-08-25T12:04:00+00:00",
        previous_ended_at="2026-08-24T19:00:00+00:00",
        tools={"harvest_start": 2, "get_status": 1},
        tx_hashes=["0xaaa", "0xbbb"],
        roster={"kamis": [{"index": 1, "state": "HARVESTING"}]},
    )
    assert entry["session"] == 7
    assert entry["started_at"] == "2026-08-25T12:00:00+00:00"
    assert entry["ended_at"] == "2026-08-25T12:04:00+00:00"
    # 17 hours between sessions — the quantity a provider outage made
    # invisible to every arm of run 006.
    assert entry["seconds_since_previous_session_end"] == 17 * 3600.0
    assert entry["tools"] == {"get_status": 1, "harvest_start": 2}
    assert entry["tx_hashes"] == ["0xaaa", "0xbbb"]
    assert entry["roster"] == {"kamis": [{"index": 1, "state": "HARVESTING"}]}


def test_no_previous_entry_means_no_elapsed_figure_rather_than_zero():
    """Zero would claim the two sessions were adjacent; absence claims nothing."""
    entry = journal.build_entry(
        session=1,
        started_at="2026-08-25T12:00:00+00:00",
        ended_at="2026-08-25T12:01:00+00:00",
        previous_ended_at=None,
        tools={},
        tx_hashes=[],
    )
    assert "seconds_since_previous_session_end" not in entry


def test_the_entry_never_names_the_apparatus():
    """I1 and the P5 silence contract, on the one surface that could break both.

    An ending reason here would disclose that forced endings exist, which
    is exactly what P5's silence protects; accounting fields would be the
    apparatus itself. Neither is a field this builder has.
    """
    entry = journal.build_entry(
        session=3,
        started_at="2026-08-25T12:00:00+00:00",
        ended_at="2026-08-25T12:01:00+00:00",
        previous_ended_at="2026-08-25T11:00:00+00:00",
        tools={"wait": 4},
        tx_hashes=[],
    )
    text = json.dumps(entry).lower()
    for forbidden in (
        "reason",
        "budget",
        "cost",
        "usd",
        "spend",
        "token",
        "cap",
        "t_max",
        "repetition",
        "llm",
    ):
        assert forbidden not in text, f"journal entry leaks {forbidden!r}"


def test_a_flood_of_transactions_cannot_crowd_out_every_other_session():
    entry = journal.build_entry(
        session=2,
        started_at="2026-08-25T12:00:00+00:00",
        ended_at="2026-08-25T12:01:00+00:00",
        previous_ended_at=None,
        tools={},
        tx_hashes=[f"0x{i:064x}" for i in range(200)],
    )
    assert len(entry["tx_hashes"]) == journal.MAX_TX_HASHES
    # The count survives the cut, so the entry never understates what
    # happened — it only stops quoting every hash.
    assert entry["tx_hashes_total"] == 200


# --- retention (P15) ----------------------------------------------------------


def test_retention_drops_oldest_entries_and_keeps_the_file_readable_whole(run_dir):
    """The bound exists so reading the WHOLE journal is never truncated."""
    for session in range(1, 400):
        journal.append(
            run_dir,
            journal.build_entry(
                session=session,
                started_at="2026-08-25T12:00:00+00:00",
                ended_at="2026-08-25T12:01:00+00:00",
                previous_ended_at=None,
                tools={"get_status": 1},
                tx_hashes=[],
                roster={"kamis": [{"index": i, "state": "RESTING"} for i in range(5)]},
            ),
            max_bytes=journal.DEFAULT_JOURNAL_MAX_BYTES,
        )
    size = journal.journal_path(run_dir).stat().st_size
    assert size <= journal.DEFAULT_JOURNAL_MAX_BYTES
    assert size < LoopCaps(session_token_cap=1).tool_result_max_bytes
    entries = journal.read_entries(run_dir)
    # Newest kept, oldest dropped, order preserved.
    assert entries[-1]["session"] == 399
    assert entries[0]["session"] > 1
    assert [e["session"] for e in entries] == sorted(e["session"] for e in entries)


def test_the_newest_entry_is_kept_even_when_it_alone_exceeds_the_bound(run_dir):
    """A journal that dropped what just happened would be worse than a big one."""
    journal.append(
        run_dir,
        journal.build_entry(
            session=1,
            started_at="2026-08-25T12:00:00+00:00",
            ended_at="2026-08-25T12:01:00+00:00",
            previous_ended_at=None,
            tools={"get_status": 1},
            tx_hashes=[],
            roster={"blob": "x" * 5000},
        ),
        max_bytes=100,
    )
    assert [e["session"] for e in journal.read_entries(run_dir)] == [1]


def test_has_session_makes_a_second_write_detectable(run_dir):
    journal.append(
        run_dir,
        journal.build_entry(
            session=4,
            started_at="2026-08-25T12:00:00+00:00",
            ended_at="2026-08-25T12:01:00+00:00",
            previous_ended_at=None,
            tools={},
            tx_hashes=[],
        ),
    )
    assert journal.has_session(run_dir, 4)
    assert not journal.has_session(run_dir, 5)


# --- the read-only tree and its discoverability (P11, P15) --------------------


def test_the_journal_is_readable_and_not_writable(run_dir):
    tools = ScaffoldTools(run_dir)
    journal.append(
        run_dir,
        journal.build_entry(
            session=1,
            started_at="2026-08-25T12:00:00+00:00",
            ended_at="2026-08-25T12:01:00+00:00",
            previous_ended_at=None,
            tools={"get_status": 1},
            tx_hashes=[],
        ),
    )
    served = tools.execute("workspace_read", {"path": JOURNAL_PATH})
    assert json.loads(served.strip())["session"] == 1
    for name in ("workspace_write", "workspace_delete"):
        args = {"path": JOURNAL_PATH}
        if name == "workspace_write":
            args["content"] = "tampered"
        with pytest.raises(Exception) as excinfo:
            tools.execute(name, args)
        assert "read-only" in str(excinfo.value)
    # The rejection is real, not cosmetic.
    assert json.loads(tools.execute("workspace_read", {"path": JOURNAL_PATH}).strip())


def test_the_file_index_names_the_journal_with_its_size(run_dir):
    """The ONLY place the journal is named — no prompt mentions it (I3).

    Discoverability without advice: a listed path with a byte count is a
    fact, on the same footing as every workspace file in the index.
    """
    tools = ScaffoldTools(run_dir)
    assert "journal/ 0 files, 0 bytes, read-only" in tools.execute("workspace_list", {})
    journal.append(
        run_dir,
        journal.build_entry(
            session=1,
            started_at="2026-08-25T12:00:00+00:00",
            ended_at="2026-08-25T12:01:00+00:00",
            previous_ended_at=None,
            tools={},
            tx_hashes=[],
        ),
    )
    listing = tools.execute("workspace_list", {}).splitlines()
    entry_line = next(line for line in listing if line.startswith(JOURNAL_PATH))
    assert int(entry_line.split()[-1]) == journal.journal_path(run_dir).stat().st_size


def test_the_journal_does_not_count_against_the_workspace_quota(run_dir):
    tools = ScaffoldTools(run_dir)
    before = tools.workspace_bytes_used()
    journal.append(
        run_dir,
        journal.build_entry(
            session=1,
            started_at="2026-08-25T12:00:00+00:00",
            ended_at="2026-08-25T12:01:00+00:00",
            previous_ended_at=None,
            tools={},
            tx_hashes=[],
        ),
    )
    assert tools.workspace_bytes_used() == before


# --- the session-start injection (P1.12) --------------------------------------


def test_the_last_entry_is_injected_as_a_readable_byte_slice(run_dir):
    """The injection is a call the agent could make itself (X22 does not apply)."""
    for session in (1, 2):
        journal.append(
            run_dir,
            journal.build_entry(
                session=session,
                started_at=f"2026-08-2{session}T12:00:00+00:00",
                ended_at=f"2026-08-2{session}T12:01:00+00:00",
                previous_ended_at=None,
                tools={"get_status": session},
                tx_hashes=[],
            ),
        )
    adapter = ScriptedAdapter(response(end_call()))
    make_loop(run_dir, adapter, session=3).run()

    injected = [
        m
        for m in adapter.requests[0]
        if isinstance(m, AssistantMessage) and m.initiator == "scaffold"
    ]
    call = injected[0].tool_calls[0]
    assert (call.id, call.name) == (JOURNAL_CALL_ID, JOURNAL_TOOL)
    assert call.args["path"] == JOURNAL_PATH
    result = next(
        m
        for m in adapter.requests[0]
        if isinstance(m, ToolResultMessage) and m.tool_call_id == JOURNAL_CALL_ID
    )
    # The LAST entry only, and exactly it.
    assert json.loads(result.content)["session"] == 2
    # The same call re-run by the agent returns the same bytes: the
    # offset/length are real, not a private slice.
    tools = ScaffoldTools(run_dir)
    assert tools.execute(JOURNAL_TOOL, dict(call.args)) == result.content


def test_the_first_session_gets_the_ordinary_not_found_result(run_dir):
    """The same visible shape a missing plan file has (P1.12.3, X21)."""
    adapter = ScriptedAdapter(response(end_call()))
    make_loop(run_dir, adapter).run()
    result = next(
        m
        for m in adapter.requests[0]
        if isinstance(m, ToolResultMessage) and m.tool_call_id == JOURNAL_CALL_ID
    )
    assert result.is_error
    assert JOURNAL_PATH in result.content
    row = next(
        e
        for e in read_events(run_dir / "telemetry.jsonl")
        if e["event"] == "tool_call" and e["initiator"] == "scaffold"
    )
    assert row["ok"] is False


def test_the_journal_injection_bounds_nothing_the_agent_does(run_dir):
    """No session_tool_cap, no error counter, no repetition breaker (X20)."""
    calls = [ToolCall(id=f"t{i}", name="get_status", args={}) for i in range(3)]
    adapter = ScriptedAdapter(response(*calls), response(end_call()))
    loop = AgentLoop(
        adapter=adapter,
        model="test-model",
        system="s",
        kickoff_text="Session start.",
        continuation_text="Continue.",
        scaffold=ScaffoldTools(run_dir, session_number=1),
        game=None,
        telemetry=TelemetryWriter(run_dir / "telemetry.jsonl", run_id="r"),
        session=1,
        params=PARAMS,
        prices=PRICES,
        # A cap of 3 must still admit three agent calls after the injection,
        # and the session-1 injection FAILS — which must not advance the
        # consecutive-error counter either.
        caps=LoopCaps(session_token_cap=100_000, session_tool_cap=3, max_consecutive_errors=1),
        sleep=lambda s: None,
    )
    result = loop.run()
    # Three agent calls executed against a cap of three: had the injection
    # consumed one, only two would have run. And the session-1 injection
    # FAILS (no journal file yet) against max_consecutive_errors=1 — had
    # that advanced the counter, the session would have ended `errors`
    # before the agent acted at all.
    assert result.reason == "tool_cap"
    executed = [
        e
        for e in read_events(run_dir / "telemetry.jsonl")
        if e["event"] == "tool_call" and e["initiator"] == "model" and not e.get("skipped")
    ]
    assert len(executed) == 3
