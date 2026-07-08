"""Path sandbox: every file path must resolve under workspace/ or reference/ (SPEC §4)."""

from __future__ import annotations

from pathlib import Path

from kami_agent.tools.errors import ToolError

ROOTS = ("workspace", "reference")


class SandboxError(ToolError):
    """Path falls outside the sandbox roots."""


def resolve_path(run_dir: Path, path: str) -> tuple[Path, str]:
    """Resolve an agent-supplied path to ``(absolute_path, root_name)``.

    ``root_name`` is ``"workspace"`` or ``"reference"``. Raises
    :class:`SandboxError` for absolute paths and for anything that
    resolves (after ``..``, ``.``, and symlinks) outside both roots —
    including other run-directory files like state.json or telemetry.jsonl.
    """
    if "\x00" in path:
        raise SandboxError("invalid path")
    candidate = Path(path)
    if candidate.is_absolute() or path.startswith("~"):
        raise SandboxError(f"path must be relative, starting with one of {ROOTS}: {path!r}")
    resolved = (run_dir / candidate).resolve()
    for root_name in ROOTS:
        root = (run_dir / root_name).resolve()
        if resolved == root or resolved.is_relative_to(root):
            return resolved, root_name
    raise SandboxError(f"path is outside workspace/ and reference/: {path!r}")
