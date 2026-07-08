"""Tool-result cap + truncation marker (D19, SPEC §5.3)."""

from kami_agent.tools.truncation import DEFAULT_TOOL_RESULT_MAX_BYTES, cap_tool_result


def test_under_cap_passes_through():
    result = cap_tool_result("short result", 100)
    assert result.content == "short result"
    assert result.truncated is False
    assert result.original_bytes == len(b"short result")


def test_exactly_at_cap_passes_through():
    content = "x" * 100
    result = cap_tool_result(content, 100)
    assert result.content == content
    assert result.truncated is False


def test_over_cap_truncates_with_marker():
    result = cap_tool_result("a" * 250, 100)
    assert result.truncated is True
    assert result.original_bytes == 250
    assert result.content.startswith("a" * 100)
    assert "[truncated: showing the first 100 bytes of 250." in result.content
    # No file path → no re-read hint.
    assert "workspace_read" not in result.content


def test_file_results_point_at_sliced_rereads():
    result = cap_tool_result("b" * 300, 100, path="reference/gdd.md")
    assert result.truncated is True
    assert "workspace_read(path='reference/gdd.md', offset, length)" in result.content


def test_multibyte_boundary_is_safe():
    # 3-byte characters; a 100-byte cut lands mid-character and must not
    # produce a decode error or replacement garbage.
    content = "€" * 50  # 150 bytes
    result = cap_tool_result(content, 100)
    head = result.content.split("\n[truncated")[0]
    assert head == "€" * 33  # 99 bytes; the split 100th byte is dropped
    assert result.original_bytes == 150


def test_default_cap_is_spec_default():
    assert DEFAULT_TOOL_RESULT_MAX_BYTES == 65536
