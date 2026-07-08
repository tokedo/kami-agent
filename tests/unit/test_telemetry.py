"""Telemetry emitter + schema (SPEC §8): every event type, sync flush, validation."""

import json
import os
from datetime import UTC, datetime

import jsonschema
import pytest

from kami_agent.adapters.base import StopReason
from kami_agent.telemetry import (
    SCHEMA_PATH,
    EventValidationError,
    TelemetryError,
    TelemetryWriter,
    UnknownEventError,
    event_types,
    read_events,
    validate_event,
)

SPEC_EVENT_TYPES = {
    "run_start",
    "session_start",
    "llm_call",
    "tool_call",
    "workspace_write",
    "workspace_delete",
    "schedule_next",
    "session_end",
    "run_complete",
}

# One representative payload per §8 event type, optional fields included.
EXAMPLE_PAYLOADS = {
    "run_start": {
        "manifest_hash": "sha256:0f",
        "model": "provider-model-1",
        "harness_sha": "352da9b",
        "agent_sha": "8ba6c4f",
        "gdd_sha": "abc1234",
        "harness_tools": ["get_state", "move"],
        "price_table": {"input_usd_per_mtok": 3.0, "output_usd_per_mtok": 15.0},
    },
    "session_start": {
        "trigger": "scheduled",
        "budget_remaining_usd": 9.5,
        "wallclock_elapsed_s": 0,
        "tools_hash": "sha256:aa",
    },
    "llm_call": {
        "model": "provider-model-1",
        "input_tokens": 1200,
        "output_tokens": 340,
        "reasoning_tokens": 120,
        "cost_usd": 0.0087,
        "cumulative_usd": 0.0512,
        "cumulative_tokens": 15400,
        "latency_ms": 2310.5,
        "stop_reason": "tool_use",
        "retry_count": 0,
        "usage_unknown": False,
        "continuation": True,
    },
    "tool_call": {
        "tool": "workspace_read",
        "source": "scaffold",
        "path": "workspace/notes.md",
        "duration_ms": 1.7,
        "ok": True,
        "truncated": True,
        "original_bytes": 120000,
    },
    "workspace_write": {
        "path": "workspace/notes.md",
        "bytes": 42,
        "workspace_total_bytes": 42,
    },
    "workspace_delete": {
        "path": "workspace/notes.md",
        "workspace_total_bytes": 0,
    },
    "schedule_next": {
        "source": "agent",
        "requested_min": 3,
        "clamped_min": 5,
        "next_wake_at": "2026-07-08T12:00:00+00:00",
    },
    "session_end": {
        "reason": "agent",
        "llm_calls": 3,
        "tool_calls": 7,
        "session_cost_usd": 0.12,
        "session_tokens": 34560,
    },
    "run_complete": {
        "reason": "budget",
        "totals": {
            "sessions": 41,
            "llm_calls": 210,
            "cumulative_usd": 10.03,
            "cumulative_tokens": 2100000,
            "overspend_usd": 0.03,
        },
    },
}


@pytest.fixture
def writer(tmp_path):
    with TelemetryWriter(tmp_path / "telemetry.jsonl", run_id="run-001") as w:
        yield w


def test_schema_is_valid_draft_2020_12():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)


def test_schema_covers_exactly_the_spec_events():
    assert event_types() == SPEC_EVENT_TYPES
    assert set(EXAMPLE_PAYLOADS) == SPEC_EVENT_TYPES


@pytest.mark.parametrize("event", sorted(SPEC_EVENT_TYPES))
def test_root_schema_oneof_accepts_every_event(event):
    # Analysts validate raw telemetry lines against the root schema directly;
    # the top-level oneOf must accept exactly what the emitter produces.
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    record = {
        "ts": "2026-07-08T12:00:00+00:00",
        "run_id": "r",
        "session": 1,
        "event": event,
        **EXAMPLE_PAYLOADS[event],
    }
    validator.validate(record)
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({**record, "event": "not_an_event"})


def test_stop_reason_enum_matches_schema():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    schema_enum = schema["$defs"]["llm_call"]["properties"]["stop_reason"]["enum"]
    # "error" marks failed-but-logged retry attempts (SPEC §5.5), which have
    # no provider stop reason; it never appears in an AdapterResponse.
    assert set(schema_enum) == {r.value for r in StopReason} | {"error"}


@pytest.mark.parametrize("event", sorted(SPEC_EVENT_TYPES))
def test_every_event_type_emits_and_reads_back(writer, event):
    record = writer.emit(event, session=3, **EXAMPLE_PAYLOADS[event])
    events = list(read_events(writer.path))
    assert events[-1] == record
    assert record["run_id"] == "run-001"
    assert record["session"] == 3
    assert record["event"] == event


def test_ts_is_iso8601_utc(writer):
    record = writer.emit("workspace_delete", session=1, **EXAMPLE_PAYLOADS["workspace_delete"])
    parsed = datetime.fromisoformat(record["ts"])
    assert parsed.utcoffset() == UTC.utcoffset(None)


def test_clock_is_injectable(tmp_path):
    fixed = datetime(2026, 7, 8, 12, 0, 0, tzinfo=UTC)
    with TelemetryWriter(tmp_path / "t.jsonl", run_id="r", clock=lambda: fixed) as w:
        record = w.emit("workspace_delete", session=1, **EXAMPLE_PAYLOADS["workspace_delete"])
    assert record["ts"] == "2026-07-08T12:00:00+00:00"


def test_unknown_event_type_rejected(writer):
    with pytest.raises(UnknownEventError):
        writer.emit("budget_warning", session=1)
    assert list(read_events(writer.path)) == []


def test_missing_required_field_rejected(writer):
    payload = dict(EXAMPLE_PAYLOADS["session_end"])
    del payload["reason"]
    with pytest.raises(EventValidationError):
        writer.emit("session_end", session=1, **payload)
    assert list(read_events(writer.path)) == []


def test_bad_enum_value_rejected(writer):
    payload = dict(EXAMPLE_PAYLOADS["session_end"], reason="sigkill")
    with pytest.raises(EventValidationError):
        writer.emit("session_end", session=1, **payload)


def test_wrong_type_rejected(writer):
    payload = dict(EXAMPLE_PAYLOADS["llm_call"], input_tokens="lots")
    with pytest.raises(EventValidationError):
        writer.emit("llm_call", session=1, **payload)


def test_extra_unknown_field_rejected(writer):
    payload = dict(EXAMPLE_PAYLOADS["workspace_write"], mood="optimistic")
    with pytest.raises(EventValidationError):
        writer.emit("workspace_write", session=1, **payload)


def test_shadowing_common_fields_rejected(writer):
    with pytest.raises(TelemetryError):
        writer.emit(
            "workspace_delete",
            session=1,
            run_id="other-run",
            **EXAMPLE_PAYLOADS["workspace_delete"],
        )


def test_non_utc_timestamp_rejected(tmp_path):
    from datetime import timedelta, timezone

    est = timezone(timedelta(hours=-5))
    with TelemetryWriter(
        tmp_path / "t.jsonl", run_id="r", clock=lambda: datetime(2026, 7, 8, 7, 0, 0, tzinfo=est)
    ) as w:
        with pytest.raises(EventValidationError):
            w.emit("workspace_delete", session=1, **EXAMPLE_PAYLOADS["workspace_delete"])


def test_append_only_ordering(writer):
    for session in (1, 1, 2):
        writer.emit("workspace_delete", session=session, **EXAMPLE_PAYLOADS["workspace_delete"])
    assert [e["session"] for e in read_events(writer.path)] == [1, 1, 2]


def test_appends_across_writer_instances(tmp_path):
    path = tmp_path / "telemetry.jsonl"
    with TelemetryWriter(path, run_id="r") as w:
        w.emit("workspace_delete", session=1, **EXAMPLE_PAYLOADS["workspace_delete"])
    with TelemetryWriter(path, run_id="r") as w:
        w.emit("workspace_delete", session=2, **EXAMPLE_PAYLOADS["workspace_delete"])
    assert [e["session"] for e in read_events(path)] == [1, 2]


def test_emit_is_visible_on_disk_before_returning(writer):
    writer.emit("workspace_delete", session=1, **EXAMPLE_PAYLOADS["workspace_delete"])
    # Read through a separate handle while the writer is still open: the
    # synchronous flush contract (SPEC §1.4) means the line is already there.
    assert len(list(read_events(writer.path))) == 1


def test_emit_fsyncs_every_event(writer, monkeypatch):
    fsynced = []
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (fsynced.append(fd), real_fsync(fd)))
    writer.emit("workspace_delete", session=1, **EXAMPLE_PAYLOADS["workspace_delete"])
    writer.emit("workspace_delete", session=1, **EXAMPLE_PAYLOADS["workspace_delete"])
    assert len(fsynced) == 2


def test_validate_event_standalone():
    record = {
        "ts": "2026-07-08T12:00:00+00:00",
        "run_id": "r",
        "session": 0,
        "event": "run_start",
        **EXAMPLE_PAYLOADS["run_start"],
    }
    validate_event(record)  # should not raise
    with pytest.raises(EventValidationError):
        validate_event({**record, "harness_tools": "not-a-list"})


def test_optional_fields_can_be_omitted(writer):
    minimal = {
        "tool": "get_state",
        "source": "harness",
        "duration_ms": 15.0,
        "ok": False,
        "error": "timeout after 120s",
    }
    record = writer.emit("tool_call", session=4, **minimal)
    assert "truncated" not in record
    assert "tx_hash" not in record
