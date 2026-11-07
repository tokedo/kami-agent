"""Which lens daemon served a run, recorded on every session_start (SPEC D7, P9).

A run's live daemon version was recorded NOWHERE. The manifest pins a
lens SHA that nothing verifies, so a host serving a different build than
its pin claims was invisible in the record — the last run's
version-scramble had to be caught by a gate on the VM while the run was
still up, which is not a thing the record can do afterwards.

The roster brief's own envelope cannot answer this: its meta carries
block number, staleness and mode, and no identity at all. The daemon's
`status` query does, so provenance is one extra query — operator-side,
never injected, never a tool_call row, never anything the agent sees.
"""

import json
import sys
from pathlib import Path

import pytest

from kami_agent.adapters.base import (
    AdapterResponse,
    SamplingParams,
    StopReason,
    ToolCall,
    ToolDef,
    Usage,
)
from kami_agent.governor import PriceTable
from kami_agent.harness import HarnessClient
from kami_agent.lens import LensQueryError, LensUnavailableError
from kami_agent.loop import GameToolResult, LoopCaps
from kami_agent.runner import SESSION_RAN, RunConfig, run_session
from kami_agent.telemetry import read_events, validate_event
from kami_agent.tools.errors import ToolError

PRICES = PriceTable(input_usd_per_mtok=3.0, output_usd_per_mtok=15.0)

STATUS_ENVELOPE = {
    "data": {
        "version": "0.4.0",
        "upstreamPin": "8302734d",
        "state": "LIVE",
        "config": {
            "chainId": 1001,
            "enrich": True,
            "defaultOperator": 3374,
            "chatEnabled": False,
        },
    },
    "untrusted": [],
    "meta": {"blockNumber": 32545805, "stale": False, "mode": "daemon"},
}


class FakeLens:
    """Answers `status` and refuses anything else; records what it was asked.

    Refusing is the point: from 0.6.0 provenance is the ONLY thing the
    scaffold asks the daemon for, and a second query would be a
    regression toward the direct-read shape the roster brief left behind.
    """

    def __init__(self, *, status=STATUS_ENVELOPE, status_error=None):
        self._status = status
        self._status_error = status_error
        self.queries = []

    def query(self, name, args=None):
        self.queries.append(name)
        if name == "status":
            if self._status_error is not None:
                raise self._status_error
            return self._status
        raise AssertionError(f"the scaffold asked the daemon for {name!r}")


class ScriptedAdapter:
    def __init__(self, *responses):
        self._responses = list(responses)

    def complete(self, system, messages, tools, params):
        return self._responses.pop(0)


def response():
    return AdapterResponse(
        text_blocks=(),
        tool_calls=(ToolCall(id="e", name="end_session", args={"reason": "done"}),),
        stop_reason=StopReason.TOOL_USE,
        usage=Usage(input_tokens=10, output_tokens=5),
    )


@pytest.fixture
def run_dir(tmp_path):
    (tmp_path / "prompts").mkdir()
    assets = Path(__file__).parents[2] / "prompts"
    for name in ("system.txt", "kickoff.txt", "continue.txt", "orientation.txt", "planning.txt"):
        (tmp_path / "prompts" / name).write_text(
            (assets / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    return tmp_path


def config_for(run_dir):
    return RunConfig(
        run_dir=Path(run_dir),
        run_id="run-001",
        model="test-model",
        prices=PRICES,
        caps=LoopCaps(session_token_cap=100_000),
        params=SamplingParams(max_tokens=4096),
    )


def session_start(run_dir):
    return [
        e for e in read_events(Path(run_dir) / "telemetry.jsonl") if e["event"] == "session_start"
    ][0]


def test_the_serving_daemons_identity_lands_on_session_start(run_dir):
    lens = FakeLens()
    outcome = run_session(
        config_for(run_dir), ScriptedAdapter(response()), lens_factory=lambda: lens
    )
    assert outcome == SESSION_RAN
    start = session_start(run_dir)
    validate_event(start)
    assert start["lens_version"] == "0.4.0"
    assert start["lens_upstream_pin"] == "8302734d"
    assert start["lens_default_operator"] == "3374"
    # ONE query per session, and it is this one. From 0.6.0 the roster
    # comes off the harness surface, so provenance is the only thing the
    # scaffold still asks the daemon directly (D7).
    assert lens.queries == ["status"]


def test_the_enrichment_flag_makes_a_mis_provisioned_rung_detectable(run_dir):
    """The daemon half of the `pushed` rung, recorded — never asserted (X25, N10).

    `pushed` changes nothing agent-side in this scaffold, so an arm
    pinned to it against a daemon running with enrichment OFF used to be
    undetectable from telemetry and could only be caught by inspecting
    the host. Two facts now sit in the record for analysis to compare;
    the scaffold still refuses nothing.
    """
    off = {**STATUS_ENVELOPE, "data": {**STATUS_ENVELOPE["data"], "config": {"enrich": False}}}
    run_session(
        config_for(run_dir), ScriptedAdapter(response()), lens_factory=lambda: FakeLens(status=off)
    )
    assert session_start(run_dir)["lens_enrich"] is False


@pytest.mark.parametrize(
    "failure",
    [
        LensUnavailableError("no such file or directory"),
        LensQueryError("NOT_FOUND", "mirror not initialized yet"),
        RuntimeError("something the client never anticipated"),
    ],
)
def test_a_daemon_that_cannot_answer_costs_nothing(run_dir, failure):
    """Absence means 'not recorded', never agreement with the manifest's pin."""
    lens = FakeLens(status_error=failure)
    outcome = run_session(
        config_for(run_dir), ScriptedAdapter(response()), lens_factory=lambda: lens
    )
    assert outcome == SESSION_RAN
    start = session_start(run_dir)
    validate_event(start)
    for field in ("lens_version", "lens_upstream_pin", "lens_enrich", "lens_default_operator"):
        assert field not in start
    # The session still ran; nothing else was asked of the daemon.
    assert lens.queries == ["status"]


def test_a_daemon_serving_a_shape_we_did_not_expect_records_what_it_can(run_dir):
    """Nothing is validated: an old daemon with no version is not an error."""
    partial = {"data": {"config": {"enrich": False}}, "meta": {}}
    run_session(
        config_for(run_dir),
        ScriptedAdapter(response()),
        lens_factory=lambda: FakeLens(status=partial),
    )
    start = session_start(run_dir)
    assert "lens_version" not in start
    assert start["lens_enrich"] is False


def test_no_daemon_means_no_provenance_and_no_query(run_dir):
    outcome = run_session(config_for(run_dir), ScriptedAdapter(response()), lens_factory=None)
    assert outcome == SESSION_RAN
    assert "lens_version" not in session_start(run_dir)


def test_provenance_is_never_an_agent_visible_channel(run_dir):
    """session_start is telemetry; the agent sees none of this (I1, P9)."""
    run_session(config_for(run_dir), ScriptedAdapter(response()), lens_factory=lambda: FakeLens())
    transcript = (run_dir / "transcripts" / "session-0001.jsonl").read_text(encoding="utf-8")
    for value in ("0.4.0", "8302734d", "upstreamPin", "enrich"):
        assert value not in transcript
    # And it produced no tool_call row of its own. No harness is
    # configured here, so the only injection is the journal read.
    tool_calls = [e for e in read_events(run_dir / "telemetry.jsonl") if e["event"] == "tool_call"]
    assert [e["tool"] for e in tool_calls if e["initiator"] == "scaffold"] == ["workspace_read"]


# --- 0.8.2 (O-TS26): the freshness every lens answer stated, on its own row ---------------
#
# The harness passes the daemon's envelope through, so every lens_* answer the
# model reads carries meta.stale / meta.blockNumber. Through 0.8.1 only the
# session-start brief's row recorded them; every model-initiated lens_* row
# had neither, so the record of the flag the model saw was partial.

FAKE_SERVER = Path(__file__).parent / "fake_mcp_server.py"
EMPTY = {"type": "object", "properties": {}}


def calls(*tool_calls):
    return AdapterResponse(
        text_blocks=(),
        tool_calls=tuple(tool_calls),
        stop_reason=StopReason.TOOL_USE,
        usage=Usage(input_tokens=10, output_tokens=5),
    )


def tool_rows(run_dir):
    rows = [e for e in read_events(Path(run_dir) / "telemetry.jsonl") if e["event"] == "tool_call"]
    for row in rows:
        validate_event(row)
    return rows


def test_a_model_lens_read_records_the_freshness_its_envelope_stated(run_dir):
    """Against the fake MCP server: the model's lens_party row carries the
    envelope's meta.stale / meta.blockNumber; its echo row carries neither;
    the brief's row still carries its own."""
    adapter = ScriptedAdapter(
        calls(
            ToolCall(id="p", name="lens_party", args={}),
            ToolCall(id="x", name="echo", args={"text": "hi"}),
        ),
        response(),
    )
    outcome = run_session(
        config_for(run_dir),
        adapter,
        harness_factory=lambda: HarnessClient(
            sys.executable, [str(FAKE_SERVER)], handshake_timeout_s=30
        ),
    )
    assert outcome == SESSION_RAN
    rows = {(e["tool"], e["initiator"]): e for e in tool_rows(run_dir)}
    party = rows[("lens_party", "model")]
    assert party["source"] == "harness"
    assert party["lens_stale"] is False
    assert party["lens_block"] == 41
    echo = rows[("echo", "model")]
    assert "lens_stale" not in echo
    assert "lens_block" not in echo
    brief = rows[("lens_roster", "scaffold")]
    assert brief["lens_stale"] is False
    assert brief["lens_block"] == 8814052


def envelope(stale, block):
    return {"data": {"x": 1}, "untrusted": [], "meta": {"blockNumber": block, "stale": stale}}


class EnvelopeHarness:
    """lens_* tools answer the daemon's envelope; one non-lens tool's answer
    happens to carry a `meta` too; one lens read raises the harness's error."""

    tool_defs = [
        ToolDef(name=name, description="d", input_schema=EMPTY)
        for name in ("lens_roster", "lens_node", "lens_kami", "get_item_orderbook")
    ]

    def execute(self, name, args):
        if name == "lens_kami":
            raise ToolError("NOT_FOUND: no kami 5")
        answer = {
            "lens_roster": envelope(False, 700),
            "lens_node": envelope(True, 701),
            "get_item_orderbook": {"orders": [], "meta": {"stale": True, "blockNumber": 9}},
        }[name]
        return GameToolResult(content=json.dumps(answer))

    def close(self):
        pass


def test_every_lens_backed_row_and_only_those_carry_the_freshness_fields(run_dir):
    adapter = ScriptedAdapter(
        calls(
            ToolCall(id="n", name="lens_node", args={}),
            ToolCall(id="k", name="lens_kami", args={}),
            ToolCall(id="o", name="get_item_orderbook", args={}),
            ToolCall(id="r", name="lens_roster", args={}),
        ),
        response(),
    )
    run_session(config_for(run_dir), adapter, harness_factory=EnvelopeHarness)
    rows = {(e["tool"], e["initiator"]): e for e in tool_rows(run_dir)}
    node = rows[("lens_node", "model")]
    assert (node["lens_stale"], node["lens_block"]) == (True, 701)
    roster = rows[("lens_roster", "model")]
    assert (roster["lens_stale"], roster["lens_block"]) == (False, 700)
    # A raised lens error is the harness's words, no envelope: nothing recorded.
    failed = rows[("lens_kami", "model")]
    assert failed["ok"] is False
    assert "lens_stale" not in failed and "lens_block" not in failed
    # Not a lens tool: its own `meta` is not the daemon's freshness.
    book = rows[("get_item_orderbook", "model")]
    assert "lens_stale" not in book and "lens_block" not in book
