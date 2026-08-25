"""On-disk provider-error artifact: run/errors.jsonl (SPEC P14).

The persistent answer to "what did the provider say?". A model call that
fails leaves an ``llm_call`` row carrying a cut-down status/type/message
(P9), but telemetry is pulled from a host that may be gone by the time
anyone asks, the process may die before the pull, and the host keeps no
agent-side journal of its own. This file is written at the moment of the
error, before anything else about that attempt is recorded, so the
evidence exists even when nothing else survives.

Three deliberate differences from :mod:`kami_agent.telemetry`, all in the
same direction — a diagnostic must never become a failure mode:

- **No schema.** This is an operator artifact, not a downstream contract,
  so there is no version to bump and no validator to refuse a line.
- **Never raises.** Every write is best-effort: a full disk, a read-only
  mount, a permission error, an unserializable value — all are swallowed.
  An error log that can end a session is worse than no error log.
- **Longer text.** ``error_text`` is kept at ``ERROR_TEXT_LOG_CHARS``
  here against ``ERROR_TEXT_TELEMETRY_CHARS`` in the stream: this file is
  read one incident at a time, where the detail is the whole point, while
  llm_call rows are read in bulk.

Never agent-visible (I1): it lives beside ``telemetry.jsonl`` in the run
directory, which no agent-supplied path can reach (P11).
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

ERRORS_FILENAME = "errors.jsonl"


class ErrorLog:
    """Append-only provider-error log for one run directory (SPEC P14).

    Unbounded on purpose, on the same terms as ``telemetry.jsonl``: a run
    directory already holds transcripts that dwarf it, and the failure
    this file exists to document is exactly the one that produces many
    rows. Truncating it would discard the middle of an outage.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        run_id: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._path = Path(path)
        self._run_id = run_id
        self._clock = clock or (lambda: datetime.now(UTC))
        # Opened on the FIRST error, not at construction: the file's
        # existence then means something failed, which is the question an
        # operator opens a run directory to answer. An empty errors.jsonl
        # in every run directory would answer it wrongly.
        self._file: IO[str] | None = None
        self._broken = False

    @property
    def path(self) -> Path:
        return self._path

    def _open(self) -> IO[str] | None:
        if self._file is not None or self._broken:
            return self._file
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._file = self._path.open("a", encoding="utf-8")
        except OSError:
            # Best-effort like writing: a run whose directory refuses this
            # file still runs, it just has no artifact. Remembered, so a
            # storm of errors does not retry the open on every one.
            self._broken = True
        return self._file

    def record(self, *, session: int, **fields: Any) -> None:
        """Append one error record, flushed and fsynced. Never raises."""
        handle = self._open()
        if handle is None:
            return
        record = {
            "ts": self._clock().isoformat(),
            "run_id": self._run_id,
            "session": session,
            **{k: v for k, v in fields.items() if v is not None},
        }
        try:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        except (OSError, ValueError, TypeError):
            return

    def close(self) -> None:
        if self._file is None:
            return
        try:
            self._file.close()
        except OSError:
            pass
        self._file = None

    def __enter__(self) -> ErrorLog:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
