"""Path sandbox: every path resolves under workspace/, reference/ or journal/ (SPEC P11).

Paths are relative to the workspace root: a bare ``notes.md`` and a
prefixed ``workspace/notes.md`` name the same file (one leading
``workspace/`` segment is stripped). ``reference/...`` addresses the
read-only documentation tree and ``journal/...`` the read-only session
journal (P15). Bare paths therefore can never reach run-directory
internals (state.json, telemetry.jsonl, errors.jsonl, prompts/).
"""

from __future__ import annotations

from pathlib import Path

from kami_agent.tools.errors import ToolError

ROOTS = ("workspace", "reference", "journal")

# Roots that are addressed by their own name rather than relative to the
# workspace root, and that no tool may write to.
_READ_ONLY_ROOTS = ("reference", "journal")


class SandboxError(ToolError):
    """Path falls outside the sandbox roots."""


def resolve_path(run_dir: Path, path: str) -> tuple[Path, str]:
    """Resolve an agent-supplied path to ``(absolute_path, root_name)``.

    ``root_name`` is ``"workspace"``, ``"reference"`` or ``"journal"``.
    Raises :class:`SandboxError` for absolute paths and for anything that
    resolves (after ``..``, ``.``, and symlinks) outside every root —
    including other run-directory files like state.json, telemetry.jsonl
    or errors.jsonl.
    """
    if "\x00" in path:
        raise SandboxError("invalid path")
    candidate = Path(path)
    if candidate.is_absolute() or path.startswith("~"):
        raise SandboxError(f"path must be relative to the workspace root: {path!r}")
    parts = candidate.parts
    if parts and parts[0] == "workspace":
        # Strip exactly one leading "workspace/" segment; the remainder is
        # workspace-relative (so workspace/notes.md == notes.md).
        candidate = Path(*parts[1:]) if len(parts) > 1 else Path()
        resolved = ((run_dir / "workspace") / candidate).resolve()
    elif parts and parts[0] in _READ_ONLY_ROOTS:
        resolved = (run_dir / candidate).resolve()
    else:
        # Bare paths are relative to the workspace root.
        resolved = ((run_dir / "workspace") / candidate).resolve()
    for root_name in ROOTS:
        root = (run_dir / root_name).resolve()
        if resolved == root or resolved.is_relative_to(root):
            return resolved, root_name
    roots = ", ".join(f"{name}/" for name in ROOTS)
    raise SandboxError(f"path is outside {roots}: {path!r}")
