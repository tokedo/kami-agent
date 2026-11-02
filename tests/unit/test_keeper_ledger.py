"""The keeper read: what the keeper signed, recorded and never shown (SPEC P9 keeper_tx, D8).

At every session start and at run_complete the scaffold reads the keeper's
ledger over its socket and writes one ``keeper_tx`` event per final
receipt it has not recorded yet. The keeper's ``ledger --since`` is a
LENS-block lower bound, inclusive; a receipt written late carries no lens
basis at all and is returned only by a whole read. None of it reaches the
model (I1): the agent learns what its keeper did by asking it.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fake_keeper import FakeKeeper, Ledger, tx_row

from kami_agent import cli
from kami_agent.adapters.base import AdapterResponse, SamplingParams, StopReason, ToolCall, Usage
from kami_agent.governor import PriceTable
from kami_agent.keeper import KeeperClient, LedgerCursor, read_keeper_receipts
from kami_agent.loop import LoopCaps
from kami_agent.runner import RunConfig, run_session
from kami_agent.state import keeper_tx_hashes, load_state
from kami_agent.telemetry import read_events, validate_event

PRICES = PriceTable(input_usd_per_mtok=3.0, output_usd_per_mtok=15.0)
T0 = datetime(2026, 10, 23, 12, 0, 0, tzinfo=UTC)
H = ["0x" + f"{i:02x}" * 32 for i in range(1, 9)]


def receipt(tx_hash, lens_block, status="success", block=None, book="auto-1"):
    return tx_row(
        book,
        tx_hash,
        stage="receipt",
        status=status,
        lens_block=lens_block,
        block=block if block is not None else (lens_block or 0) + 2,
    )


def broadcast(tx_hash, lens_block, book="auto-1"):
    return tx_row(book, tx_hash, stage="broadcast", status="unconfirmed", lens_block=lens_block)


def read(keeper_path, cursor, emitted):
    return read_keeper_receipts(KeeperClient(keeper_path), cursor, emitted)


# --- E.6 (P7) --------------------------------------------------------------------------


def test_three_receipts_and_one_unconfirmed_make_exactly_three_events():
    rows = [
        broadcast(H[0], 100),
        receipt(H[0], 100),
        broadcast(H[1], 105),
        receipt(H[1], 105, status="reverted"),
        broadcast(H[2], 110),
        receipt(H[2], 110),
        broadcast(H[3], 112),
    ]
    ledger = Ledger({"auto-1": rows})
    cursor, emitted = LedgerCursor(), set()
    with FakeKeeper(ledger) as keeper:
        found, version = read(keeper.socket_path, cursor, emitted)
    assert [e["tx_hash"] for e in found] == H[:3]
    assert [e["status"] for e in found] == ["success", "reverted", "success"]
    assert version == "1.0.0"
    assert cursor.blocks == {"auto-1": 112}
    assert cursor.open == {"auto-1": [H[3]]}


def test_a_re_read_from_the_same_cursor_emits_nothing_and_a_new_row_emits_one():
    rows = [broadcast(H[0], 100), receipt(H[0], 100), receipt(H[1], 104)]
    ledger = Ledger({"auto-1": rows})
    cursor, emitted = LedgerCursor(), set()
    with FakeKeeper(ledger) as keeper:
        first, _ = read(keeper.socket_path, cursor, emitted)
        again, _ = read(keeper.socket_path, cursor, emitted)
        rows.append(receipt(H[2], 104))  # same lens block as the cursor: inclusive
        rows.append(receipt(H[3], 120))
        new, _ = read(keeper.socket_path, cursor, emitted)
        sent = keeper.requests
    assert len(first) == 2
    assert again == []
    assert [e["tx_hash"] for e in new] == [H[2], H[3]]
    ledger_args = [args for cmd, args in sent if cmd == "ledger"]
    assert ledger_args[0] == {"book": "auto-1", "kind": "tx"}
    assert ledger_args[1] == {"book": "auto-1", "kind": "tx", "since": 104}


def test_an_unconfirmed_receipt_is_not_final_and_its_late_receipt_is_found():
    """The keeper writes `receipt unconfirmed` when its receipt read did not
    answer, then the real receipt later — with NO lens basis, which a
    `since` read never returns. The hash stays open until it closes."""
    rows = [broadcast(H[0], 100), receipt(H[0], 100, status="unconfirmed")]
    ledger = Ledger({"auto-1": rows})
    cursor, emitted = LedgerCursor(), set()
    with FakeKeeper(ledger) as keeper:
        first, _ = read(keeper.socket_path, cursor, emitted)
        rows.append(receipt(H[0], None, block=103))
        second, _ = read(keeper.socket_path, cursor, emitted)
        third, _ = read(keeper.socket_path, cursor, emitted)
    assert first == []
    assert [(e["tx_hash"], e["status"], e["block"]) for e in second] == [(H[0], "success", 103)]
    assert third == []
    assert cursor.open == {}


def test_a_broadcast_seen_in_a_window_is_closed_in_the_same_read():
    """A since-read that finds a new broadcast reads the book whole once
    more, so a late receipt already written is not left for later."""
    rows = [receipt(H[0], 100)]
    ledger = Ledger({"auto-1": rows})
    cursor, emitted = LedgerCursor(), set()
    with FakeKeeper(ledger) as keeper:
        read(keeper.socket_path, cursor, emitted)
        rows.append(broadcast(H[1], 130))
        rows.append(receipt(H[1], None, block=131))
        found, _ = read(keeper.socket_path, cursor, emitted)
    assert [e["tx_hash"] for e in found] == [H[1]]
    assert cursor.open == {}


def test_every_book_of_the_account_is_read_and_events_name_their_book():
    ledger = Ledger(
        {"auto-1": [receipt(H[0], 100)], "craft-2": [receipt(H[1], 90, book="craft-2")]}
    )
    with FakeKeeper(ledger) as keeper:
        found, _ = read(keeper.socket_path, LedgerCursor(), set())
        books_args = [args for cmd, args in keeper.requests if cmd == "books"]
    assert {(e["book"], e["tx_hash"]) for e in found} == {("auto-1", H[0]), ("craft-2", H[1])}
    assert books_args == [{"account": "main"}]


def test_a_keeper_that_does_not_answer_contributes_nothing():
    cursor = LedgerCursor(blocks={"auto-1": 7})
    with FakeKeeper(silent=True) as keeper:
        found, version = read_keeper_receipts(
            KeeperClient(keeper.socket_path, timeout_s=0.2), cursor, set()
        )
    assert (found, version) == ([], None)
    assert cursor.blocks == {"auto-1": 7}


def test_the_event_carries_the_ledgers_own_values():
    row = receipt(H[0], 100, block=102)
    with FakeKeeper(Ledger({"auto-1": [row]})) as keeper:
        (event,), _ = read(keeper.socket_path, LedgerCursor(), set())
    assert event == {
        "book": "auto-1",
        "tx_hash": H[0],
        "action": "system.harvest.collect",
        "status": "success",
        "initiator": "keeper",
        "source": "keeper",
        "block": 102,
        "nonce": 1,
        "gas_used": 51234,
        "ledger_ts": "2026-10-23T11:00:00.000Z",
        "receipt_stage": "receipt",
    }


# --- through the runner: the cursor, the dedup, the record ------------------------------


class ScriptedAdapter:
    def __init__(self, *responses):
        self._responses = list(responses)
        self.requests = []

    def complete(self, system, messages, tools, params):
        self.requests.append({"system": system, "messages": list(messages), "tools": tools})
        return self._responses.pop(0)


def end_response():
    return AdapterResponse(
        text_blocks=(),
        tool_calls=(ToolCall(id="t-end", name="end_session", args={"reason": "done"}),),
        stop_reason=StopReason.TOOL_USE,
        usage=Usage(input_tokens=10, output_tokens=5),
    )


@pytest.fixture
def run_dir(tmp_path):
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    source = cli._prompts_source()
    for name in cli.PROMPT_NAMES:
        (prompts / name).write_text((source / name).read_text(encoding="utf-8"), encoding="utf-8")
    return tmp_path


def session(run_dir, keeper, clock_now, adapter=None):
    adapter = adapter or ScriptedAdapter(end_response())
    run_session(
        RunConfig(
            run_dir=Path(run_dir),
            run_id="run-080",
            model="m",
            prices=PRICES,
            caps=LoopCaps(session_token_cap=100_000),
            params=SamplingParams(max_tokens=4096),
            scaffold_profile="shell_keeper",
        ),
        adapter,
        keeper_factory=lambda: KeeperClient(keeper.socket_path),
        clock=lambda: clock_now,
        trigger="manual",
    )
    return adapter


def telemetry(run_dir):
    return list(read_events(Path(run_dir) / "telemetry.jsonl"))


def test_keeper_tx_events_follow_session_start_validate_and_dedup_across_sessions(run_dir):
    rows = [broadcast(H[0], 100), receipt(H[0], 100), receipt(H[1], 101)]
    with FakeKeeper(Ledger({"auto-1": rows})) as keeper:
        session(run_dir, keeper, T0)
        rows.append(receipt(H[2], 140))
        session(run_dir, keeper, T0 + timedelta(hours=1))
    stream = telemetry(run_dir)
    txs = [e for e in stream if e["event"] == "keeper_tx"]
    for e in txs:
        validate_event(e)
    assert [(e["session"], e["tx_hash"]) for e in txs] == [(1, H[0]), (1, H[1]), (2, H[2])]
    kinds = [e["event"] for e in stream]
    assert kinds[kinds.index("session_start") + 1] == "keeper_tx"
    state = load_state(Path(run_dir) / "state.json")
    assert state.keeper_cursor == {"auto-1": 140}
    assert state.keeper_open == {}


def test_a_lost_cache_costs_a_whole_read_and_never_a_duplicate(run_dir):
    rows = [receipt(H[0], 100), receipt(H[1], 101)]
    with FakeKeeper(Ledger({"auto-1": rows})) as keeper:
        session(run_dir, keeper, T0)
        (Path(run_dir) / "state.json").unlink()
        session(run_dir, keeper, T0 + timedelta(hours=1))
        ledger_args = [args for cmd, args in keeper.requests if cmd == "ledger"]
    assert keeper_tx_hashes(telemetry(run_dir)) == {H[0], H[1]}
    assert len([e for e in telemetry(run_dir) if e["event"] == "keeper_tx"]) == 2
    assert "since" not in ledger_args[-1]


def test_nothing_of_the_keeper_reaches_the_model(run_dir):
    """I1: the book id, the hashes, the version and the keeper's own words
    are telemetry; no agent-visible string carries them. (The appendix
    names the keeper COMMAND — that is the rung's documentation.)"""
    rows = [receipt(H[0], 100), receipt(H[1], 101, status="reverted")]
    with FakeKeeper(Ledger({"auto-SECRETBOOK": rows})) as keeper:
        adapter = session(run_dir, keeper, T0)
    seen = []
    for request in adapter.requests:
        seen.append(request["system"])
        for message in request["messages"]:
            seen.append(repr(message))
        seen.extend(t.description for t in request["tools"])
    visible = "\n".join(seen)
    assert any(e["event"] == "keeper_tx" for e in telemetry(run_dir))
    for leak in (
        "auto-SECRETBOOK",
        H[0],
        H[1],
        "keeper_tx",
        "1.0.0",
        "system.harvest",
        "51234",
        "2026-10-23T11:00:00",
    ):
        assert leak not in visible, leak
    transcripts = (Path(run_dir) / "transcripts").glob("*.jsonl")
    for path in transcripts:
        text = path.read_text(encoding="utf-8")
        for leak in ("auto-SECRETBOOK", H[0], H[1]):
            assert leak not in text
