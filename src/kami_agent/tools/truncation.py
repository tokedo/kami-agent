"""Tool-result cap with an explicit truncation marker (D19, SPEC §5.3).

Applied uniformly to scaffold and harness results before they are
inserted into context. The marker states the original size and — for
file reads — that the content can be re-read in slices.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_TOOL_RESULT_MAX_BYTES = 65536


@dataclass(frozen=True, slots=True)
class CappedResult:
    content: str
    truncated: bool
    original_bytes: int


def cap_tool_result(
    content: str,
    max_bytes: int = DEFAULT_TOOL_RESULT_MAX_BYTES,
    *,
    path: str | None = None,
) -> CappedResult:
    """Cap ``content`` at ``max_bytes`` of UTF-8, appending a marker when cut.

    ``path`` is set for file-read results, where the marker can point at
    byte-sliced re-reads via workspace_read (D19).
    """
    raw = content.encode("utf-8")
    original = len(raw)
    if original <= max_bytes:
        return CappedResult(content=content, truncated=False, original_bytes=original)
    head = raw[:max_bytes].decode("utf-8", errors="ignore")
    hint = ""
    if path is not None:
        hint = f" Re-read it in slices with workspace_read(path={path!r}, offset, length)."
    marker = f"\n[truncated: showing the first {max_bytes} bytes of {original}.{hint}]"
    return CappedResult(content=head + marker, truncated=True, original_bytes=original)
