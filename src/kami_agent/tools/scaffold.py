"""Scaffold tools (SPEC §4): workspace/reference file tools, scheduling, status, end_session.

All strings the model can see — tool names, descriptions, results, error
messages — are mechanism-only: no budget, spend, horizon, or cap
information (D12), no strategy or memory-structure hints (hard rule 2).
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kami_agent.adapters.base import ToolDef
from kami_agent.tools.errors import ToolError
from kami_agent.tools.sandbox import resolve_path

DEFAULT_WORKSPACE_QUOTA_BYTES = 10 * 1024 * 1024
DEFAULT_WAKE_MIN_MINUTES = 5.0
DEFAULT_WAKE_MAX_MINUTES = 24 * 60.0

SCAFFOLD_TOOL_DEFS: list[ToolDef] = [
    ToolDef(
        name="workspace_write",
        description=(
            "Write a file under workspace/. Creates parent directories and replaces the whole file."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    ),
    ToolDef(
        name="workspace_read",
        description=(
            "Read a file under workspace/ or reference/. Optional offset and "
            "length select a byte range."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer"},
                "length": {"type": "integer"},
            },
            "required": ["path"],
        },
    ),
    ToolDef(
        name="workspace_list",
        description=(
            "List files and sizes. Without a path, lists all of workspace/ and "
            "a one-line summary of reference/. With a path, lists that file or "
            "subtree."
        ),
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
        },
    ),
    ToolDef(
        name="workspace_delete",
        description="Delete a file under workspace/.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    ),
    ToolDef(
        name="set_next_wake",
        description=(
            "Choose when the next session starts, in minutes from now. Values "
            "outside the allowed range are clamped. The last call in a session "
            "wins."
        ),
        input_schema={
            "type": "object",
            "properties": {"minutes_from_now": {"type": "number"}},
            "required": ["minutes_from_now"],
        },
    ),
    ToolDef(
        name="get_status",
        description=("Return the current UTC time, the session number, and workspace usage."),
        input_schema={"type": "object", "properties": {}},
    ),
    ToolDef(
        name="end_session",
        description="End this session.",
        input_schema={
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
    ),
]

SCAFFOLD_TOOL_NAMES = frozenset(tool.name for tool in SCAFFOLD_TOOL_DEFS)


class ScaffoldTools:
    """Executes the scaffold tools against a run directory.

    One instance per session. ``set_next_wake`` and ``end_session``
    accumulate their effect on the instance (``requested_wake_min`` /
    ``clamped_wake_min`` / ``session_ended`` / ``end_reason``) for the
    runner to apply. ``emit`` receives the SPEC §8 ``workspace_write`` /
    ``workspace_delete`` telemetry payloads.
    """

    def __init__(
        self,
        run_dir: str | Path,
        *,
        session_number: int = 0,
        workspace_quota_bytes: int = DEFAULT_WORKSPACE_QUOTA_BYTES,
        wake_min_minutes: float = DEFAULT_WAKE_MIN_MINUTES,
        wake_max_minutes: float = DEFAULT_WAKE_MAX_MINUTES,
        budget_visible: bool = False,
        budget_remaining_usd: float | None = None,
        clock: Callable[[], datetime] | None = None,
        emit: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.run_dir = Path(run_dir)
        self.session_number = session_number
        self.workspace_quota_bytes = workspace_quota_bytes
        self.wake_min_minutes = wake_min_minutes
        self.wake_max_minutes = wake_max_minutes
        # Mechanism for a future budget-visible arm; pinned False for 001 (D12).
        self.budget_visible = budget_visible
        self.budget_remaining_usd = budget_remaining_usd
        self._clock = clock or (lambda: datetime.now(UTC))
        self._emit = emit or (lambda event, fields: None)

        self.workspace_root = self.run_dir / "workspace"
        self.reference_root = self.run_dir / "reference"
        self.workspace_root.mkdir(parents=True, exist_ok=True)

        self.requested_wake_min: float | None = None
        self.clamped_wake_min: float | None = None
        self.session_ended = False
        self.end_reason: str | None = None

    def execute(self, name: str, args: dict[str, Any]) -> str:
        if name not in SCAFFOLD_TOOL_NAMES:
            raise ToolError(f"unknown tool: {name}")
        handler = getattr(self, name)
        try:
            return handler(**args)
        except TypeError as exc:
            raise ToolError(f"invalid arguments for {name}: {exc}") from exc

    # --- file tools ---------------------------------------------------------

    def workspace_write(self, path: str, content: str) -> str:
        resolved, root = resolve_path(self.run_dir, path)
        if root != "workspace":
            raise ToolError("workspace_write only writes under workspace/; reference/ is read-only")
        if resolved == self.workspace_root.resolve() or resolved.is_dir():
            raise ToolError(f"path is a directory: {path!r}")
        data = content.encode("utf-8")
        existing = resolved.stat().st_size if resolved.is_file() else 0
        projected = self.workspace_bytes_used() - existing + len(data)
        if projected > self.workspace_quota_bytes:
            raise ToolError(
                "write rejected: workspace quota exceeded "
                f"({projected} of {self.workspace_quota_bytes} bytes)"
            )
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_bytes(data)
        except OSError as exc:
            raise ToolError(f"cannot write {path!r}: {exc.strerror or exc}") from exc
        rel = self._rel(resolved)
        self._emit(
            "workspace_write",
            {"path": rel, "bytes": len(data), "workspace_total_bytes": self.workspace_bytes_used()},
        )
        return f"Wrote {len(data)} bytes to {rel}."

    def workspace_read(
        self, path: str, offset: int | None = None, length: int | None = None
    ) -> str:
        resolved, _root = resolve_path(self.run_dir, path)
        if not resolved.is_file():
            raise ToolError(f"no such file: {path!r}")
        if offset is not None and offset < 0:
            raise ToolError("offset must be >= 0")
        if length is not None and length < 0:
            raise ToolError("length must be >= 0")
        data = resolved.read_bytes()
        start = offset or 0
        end = None if length is None else start + length
        return data[start:end].decode("utf-8", errors="replace")

    def workspace_list(self, path: str | None = None) -> str:
        if path is None:
            lines = self._tree_lines(self.workspace_root, "workspace")
            if not lines:
                lines = ["workspace/ (empty)"]
            lines.append(self._reference_summary())
            return "\n".join(lines)
        resolved, _root = resolve_path(self.run_dir, path)
        if resolved.is_file():
            return f"{self._rel(resolved)} {resolved.stat().st_size}"
        if not resolved.is_dir():
            raise ToolError(f"no such path: {path!r}")
        lines = self._tree_lines(resolved, self._rel(resolved))
        return "\n".join(lines) if lines else f"{self._rel(resolved)}/ (empty)"

    def workspace_delete(self, path: str) -> str:
        resolved, root = resolve_path(self.run_dir, path)
        if root != "workspace":
            raise ToolError(
                "workspace_delete only deletes under workspace/; reference/ is read-only"
            )
        if resolved.is_dir():
            raise ToolError(f"path is a directory, not a file: {path!r}")
        if not resolved.is_file():
            raise ToolError(f"no such file: {path!r}")
        resolved.unlink()
        rel = self._rel(resolved)
        self._emit(
            "workspace_delete",
            {"path": rel, "workspace_total_bytes": self.workspace_bytes_used()},
        )
        return f"Deleted {rel}."

    # --- scheduling / status / termination -----------------------------------

    def set_next_wake(self, minutes_from_now: float) -> str:
        try:
            minutes = float(minutes_from_now)
        except (TypeError, ValueError) as exc:
            raise ToolError("minutes_from_now must be a number") from exc
        if math.isnan(minutes) or math.isinf(minutes):
            raise ToolError("minutes_from_now must be a finite number")
        clamped = min(max(minutes, self.wake_min_minutes), self.wake_max_minutes)
        self.requested_wake_min = minutes
        self.clamped_wake_min = clamped
        return f"Next session in {clamped:g} minutes."

    def get_status(self) -> str:
        # Exactly these fields and nothing else (D12): no budget, spend,
        # token counts, elapsed-run figures, or T_max.
        status: dict[str, Any] = {
            "current_time_utc": self._clock().isoformat(),
            "session_number": self.session_number,
            "workspace_bytes_used": self.workspace_bytes_used(),
            "workspace_quota_bytes": self.workspace_quota_bytes,
        }
        if self.budget_visible and self.budget_remaining_usd is not None:
            status["budget_remaining_usd"] = self.budget_remaining_usd
        return json.dumps(status)

    def end_session(self, reason: str) -> str:
        self.session_ended = True
        self.end_reason = str(reason)
        return "Session ended."

    # --- helpers -------------------------------------------------------------

    def workspace_bytes_used(self) -> int:
        return sum(f.stat().st_size for f in self.workspace_root.rglob("*") if f.is_file())

    def _rel(self, resolved: Path) -> str:
        return str(resolved.relative_to(self.run_dir.resolve()))

    def _tree_lines(self, root: Path, prefix: str) -> list[str]:
        if not root.is_dir():
            return []
        files = sorted(p for p in root.rglob("*") if p.is_file())
        return [f"{prefix}/{p.relative_to(root)} {p.stat().st_size}" for p in files]

    def _reference_summary(self) -> str:
        if not self.reference_root.is_dir():
            return "reference/ 0 files, 0 bytes, read-only"
        files = [p for p in self.reference_root.rglob("*") if p.is_file()]
        total = sum(p.stat().st_size for p in files)
        return f"reference/ {len(files)} files, {total} bytes, read-only"
