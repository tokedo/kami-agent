"""Telemetry schema 0.8.0 and the 5.0.0 surface (SPEC P9, D1; ARTEFACTS B / E.7).

Additive over 0.7.0 — two new events (`keeper_tx`, `shell_state`), the two
rung values, the `keeper` source and initiator, the shell and keeper
fields on `tool_call`, three provenance fields, two `run_complete` fields
— and `unevaluatedProperties: false` is what forces the bump.
"""

import json
from pathlib import Path

import pytest

from kami_agent.adapters.base import ToolDef
from kami_agent.harness import tools_hash
from kami_agent.telemetry import SCHEMA_PATH, EventValidationError, validate_event
from kami_agent.tools.scaffold import PROFILES, scaffold_tool_defs

FIXTURE = Path(__file__).parents[2] / "tests" / "smoke" / "fixtures" / "harness_tools.json"

COMMON = {"ts": "2026-10-23T12:00:00+00:00", "run_id": "r", "session": 3}

KEEPER_TX = {
    "event": "keeper_tx",
    "book": "auto-1",
    "tx_hash": "0x" + "ab" * 32,
    "action": "system.harvest.collect",
    "status": "success",
    "initiator": "keeper",
    "source": "keeper",
    "block": 1001,
    "nonce": 17,
    "gas_used": 51234,
    "ledger_ts": "2026-10-23T11:00:00.000Z",
    "receipt_stage": "receipt",
}

SHELL_STATE = {
    "event": "shell_state",
    "phase": "session_start",
    "container": "kami-env",
    "running": True,
    "restarted": False,
    "process_count": 3,
    "workspace_bytes": 2048,
    "processes": [{"pid": 501, "comm": "sleep", "args": "sleep 600"}],
    "restart_count": 0,
    "lens_socket_ok": True,
    "keeper_socket_ok": True,
}


def event(payload, **changes):
    return {**COMMON, **payload, **changes}


def test_the_version_reads_0_8_0():
    assert json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))["version"] == "0.8.0"


@pytest.mark.parametrize("payload", [KEEPER_TX, SHELL_STATE], ids=["keeper_tx", "shell_state"])
def test_every_new_event_validates(payload):
    validate_event(event(payload))


@pytest.mark.parametrize("payload", [KEEPER_TX, SHELL_STATE], ids=["keeper_tx", "shell_state"])
def test_an_unknown_field_is_rejected(payload):
    with pytest.raises(EventValidationError):
        validate_event(event(payload, surprise=1))


def test_keeper_tx_is_always_the_keepers():
    with pytest.raises(EventValidationError):
        validate_event(event(KEEPER_TX, initiator="model"))
    with pytest.raises(EventValidationError):
        validate_event(event(KEEPER_TX, source="harness"))
    minimal = {
        k: KEEPER_TX[k]
        for k in ("event", "book", "tx_hash", "action", "status", "initiator", "source")
    }
    validate_event(event(minimal))
    for key in ("book", "tx_hash", "action", "status"):
        with pytest.raises(EventValidationError):
            validate_event(event({k: v for k, v in minimal.items() if k != key}))


def test_shell_state_bounds_its_process_list():
    many = [{"pid": i, "comm": "x", "args": "x"} for i in range(51)]
    with pytest.raises(EventValidationError):
        validate_event(event(SHELL_STATE, processes=many))
    with pytest.raises(EventValidationError):
        validate_event(event(SHELL_STATE, processes=[{"pid": 1, "comm": "x", "args": "a" * 201}]))
    with pytest.raises(EventValidationError):
        validate_event(event(SHELL_STATE, phase="midway"))
    for phase in ("session_start", "session_end", "run_complete"):
        validate_event(event(SHELL_STATE, phase=phase))


SESSION_START = {
    "event": "session_start",
    "trigger": "scheduled",
    "budget_remaining_usd": 9.0,
    "wallclock_elapsed_s": 0,
    "tools_hash": "sha256:aa",
}


@pytest.mark.parametrize("rung", ["shell", "shell_keeper"])
def test_the_two_rung_values_and_their_provenance_fields(rung):
    validate_event(
        event(
            SESSION_START,
            scaffold_profile=rung,
            keeper_version="1.0.0",
            environment_image="kami-agent-env:0.8.0-shell_keeper",
        )
    )


def test_an_unknown_rung_is_still_rejected():
    with pytest.raises(EventValidationError):
        validate_event(event(SESSION_START, scaffold_profile="shell_plus"))


TOOL_CALL = {
    "event": "tool_call",
    "tool": "shell",
    "source": "scaffold",
    "duration_ms": 1.0,
    "ok": True,
}


def test_shell_fields_on_tool_call():
    validate_event(
        event(
            TOOL_CALL,
            initiator="model",
            exit_code=0,
            timed_out=False,
            stdout_bytes=3,
            stderr_bytes=0,
            shell_log="shell/001-0004.log",
        )
    )
    validate_event(event(TOOL_CALL, exit_code=None))


def test_the_keeper_rows_at_run_complete():
    for tool, extra in (
        ("keeper_stop_all", {"keeper_books_stopped": 1}),
        ("keeper_revoke", {"keeper_grants_after": 0}),
    ):
        validate_event(event(TOOL_CALL, tool=tool, source="keeper", initiator="scaffold", **extra))
    # The keeper initiator is in the enum, reserved for analysis joins.
    validate_event(event(TOOL_CALL, initiator="keeper"))


def test_run_start_and_run_complete_additions():
    validate_event(
        event(
            {
                "event": "run_start",
                "manifest_hash": "sha256:0f",
                "model": "m",
                "harness_sha": "h",
                "agent_sha": "a",
                "gdd_sha": "g",
                "harness_tools": [],
                "price_table": {},
                "keeper_sha": "",
            }
        )
    )
    totals = {
        "sessions": 1,
        "llm_calls": 1,
        "cumulative_usd": 1.0,
        "cumulative_tokens": 1,
        "overspend_usd": 0.0,
    }
    validate_event(
        event(
            {"event": "run_complete", "reason": "budget", "totals": totals},
            environment_stopped=True,
            keeper_grants_after=0,
        )
    )


# --- the recorded surface at kami-harness 5.0.0 (D1, E.7) ---------------------------------

# The scaffold's tools_hash per rung over the recorded 5.0.0 surface. Three
# distinct values: rungs that add no tool share their lower neighbour's
# hash, and the two shell rungs share one (X24).
PER_RUNG_TOOLS_HASH = {
    "control": "sha256:6c37a39141045189f2a08e359103fac762bb828635eee1e0e1f65c2ae55e51dc",
    "orientation": "sha256:6c37a39141045189f2a08e359103fac762bb828635eee1e0e1f65c2ae55e51dc",
    "search": "sha256:37e63fd5d3f24081a7456cd651133a7e9e0b87201b5d0021438db017bc41cb4d",
    "pushed": "sha256:37e63fd5d3f24081a7456cd651133a7e9e0b87201b5d0021438db017bc41cb4d",
    "planning": "sha256:37e63fd5d3f24081a7456cd651133a7e9e0b87201b5d0021438db017bc41cb4d",
    "shell": "sha256:00fc636c73fcf6f393e579098e1c44593a2bf9ed0c1a0899854fe55dbf2561a4",
    "shell_keeper": "sha256:00fc636c73fcf6f393e579098e1c44593a2bf9ed0c1a0899854fe55dbf2561a4",
}


def recorded():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_the_fixture_is_the_5_0_0_surface():
    surface = recorded()
    assert surface["harness"]["version"] == "5.0.0"
    assert surface["harness"]["schema_version"] == "5.0.0"
    assert surface["harness"]["recorded_under_python"] == "3.13"
    assert len(surface["tools"]) == 100
    assert surface["harness_published_tools_hash"] == (
        "3291008d8940b55823f2b052194107d226db8f0c620864f6e07910262e6ca02a"
    )
    names = {t["name"] for t in surface["tools"]}
    # The strategy-service family left the surface in 5.0.0.
    assert not names & {"register_kamibots", "start_strategy", "get_strategy_logs"}
    assert len(surface["standing_text"]) == 957


def test_the_per_rung_tools_hash_over_the_recorded_surface():
    game = [
        ToolDef(name=t["name"], description=t["description"], input_schema=t["input_schema"])
        for t in recorded()["tools"]
    ]
    got = {p: tools_hash(game + scaffold_tool_defs(p)) for p in PROFILES}
    assert got == PER_RUNG_TOOLS_HASH
    assert got["shell"] == got["shell_keeper"] != got["planning"]
