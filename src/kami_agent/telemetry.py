"""Telemetry: append-only JSONL event stream, synchronous flush, schema validation (SPEC §8).

telemetry.jsonl is the source of truth for all accounting (SPEC §7.1);
state.json is a rebuildable cache. Every event is validated against
the §8 schema (``kami_agent/schema/telemetry.json``) and flushed to
disk synchronously
(write → flush → fsync) before the action it describes is considered
complete (SPEC §1.4), so a crash at any point loses at most the event
being written.

Telemetry is not an agent-visible channel (D12): budget fields recorded
here never reach the agent.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from functools import cache
from importlib import resources
from pathlib import Path
from typing import IO, Any

import jsonschema

# The schema ships inside the wheel (kami_agent/schema) so validation works
# under any install, including the Docker image; the repo-root schema/ copy
# remains for the brief's layout and is kept byte-identical by a unit test.
SCHEMA_PATH = Path(str(resources.files("kami_agent") / "schema" / "telemetry.json"))

_COMMON_FIELDS = frozenset({"ts", "run_id", "session", "event"})


class TelemetryError(Exception):
    """Base error for the telemetry subsystem."""


class UnknownEventError(TelemetryError):
    """Event type not defined in the telemetry schema."""


class EventValidationError(TelemetryError):
    """Event payload failed schema validation."""


@cache
def _event_validators(schema_path: Path) -> dict[str, jsonschema.Draft202012Validator]:
    """One validator per §8 event type, resolved within the schema document."""
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    defs = schema["$defs"]
    return {
        name: jsonschema.Draft202012Validator({"$ref": f"#/$defs/{name}", "$defs": defs})
        for name in defs
        if name != "common"
    }


def event_types(schema_path: Path = SCHEMA_PATH) -> frozenset[str]:
    """The event types the schema defines."""
    return frozenset(_event_validators(schema_path))


def validate_event(event: dict[str, Any], schema_path: Path = SCHEMA_PATH) -> None:
    """Validate one event dict against the §8 schema; raise on any mismatch."""
    name = event.get("event")
    validators = _event_validators(schema_path)
    if name not in validators:
        raise UnknownEventError(f"unknown telemetry event type: {name!r}")
    errors = sorted(validators[name].iter_errors(event), key=lambda e: list(e.absolute_path))
    if errors:
        detail = "; ".join(e.message for e in errors)
        raise EventValidationError(f"invalid {name} event: {detail}")


class TelemetryWriter:
    """Appends schema-validated events to a JSONL file with synchronous flush."""

    def __init__(
        self,
        path: str | Path,
        run_id: str,
        *,
        schema_path: Path = SCHEMA_PATH,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._path = Path(path)
        self._run_id = run_id
        self._schema_path = schema_path
        self._clock = clock or (lambda: datetime.now(UTC))
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file: IO[str] = self._path.open("a", encoding="utf-8")

    @property
    def path(self) -> Path:
        return self._path

    def emit(self, event: str, *, session: int, **fields: Any) -> dict[str, Any]:
        """Validate, append, and synchronously flush one event; return the record."""
        reserved = _COMMON_FIELDS & fields.keys()
        if reserved:
            raise TelemetryError(f"fields shadow common telemetry fields: {sorted(reserved)}")
        record: dict[str, Any] = {
            "ts": self._clock().isoformat(),
            "run_id": self._run_id,
            "session": session,
            "event": event,
            **fields,
        }
        validate_event(record, schema_path=self._schema_path)
        self._file.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._file.flush()
        os.fsync(self._file.fileno())
        return record

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> TelemetryWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def read_events(path: str | Path) -> Iterator[dict[str, Any]]:
    """Yield events from a telemetry JSONL file in order. No validation."""
    with Path(path).open(encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if stripped:
                yield json.loads(stripped)
