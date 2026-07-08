"""Scaffold tools (SPEC §4): file tools, quota, scheduling, status minimalism (D12)."""

import json
from datetime import UTC, datetime

import pytest

from kami_agent.tools.errors import ToolError
from kami_agent.tools.scaffold import (
    SCAFFOLD_TOOL_DEFS,
    SCAFFOLD_TOOL_NAMES,
    ScaffoldTools,
)

FIXED_NOW = datetime(2026, 7, 8, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def run_dir(tmp_path):
    (tmp_path / "reference" / "world").mkdir(parents=True)
    (tmp_path / "reference" / "intro.md").write_text("x" * 100)
    (tmp_path / "reference" / "world" / "map.md").write_text("y" * 50)
    return tmp_path


@pytest.fixture
def emitted():
    return []


@pytest.fixture
def tools(run_dir, emitted):
    return ScaffoldTools(
        run_dir,
        session_number=7,
        workspace_quota_bytes=1000,
        clock=lambda: FIXED_NOW,
        emit=lambda event, fields: emitted.append((event, fields)),
    )


# --- workspace_write ---------------------------------------------------------


def test_write_creates_parents_and_reports(tools, run_dir, emitted):
    result = tools.execute("workspace_write", {"path": "workspace/a/b/notes.md", "content": "hi"})
    assert (run_dir / "workspace" / "a" / "b" / "notes.md").read_text() == "hi"
    assert result == "Wrote 2 bytes to workspace/a/b/notes.md."
    assert emitted == [
        (
            "workspace_write",
            {"path": "workspace/a/b/notes.md", "bytes": 2, "workspace_total_bytes": 2},
        )
    ]


def test_write_overwrites_whole_file(tools, run_dir):
    tools.execute("workspace_write", {"path": "workspace/f.txt", "content": "long old content"})
    tools.execute("workspace_write", {"path": "workspace/f.txt", "content": "new"})
    assert (run_dir / "workspace" / "f.txt").read_text() == "new"


def test_write_rejected_under_reference(tools, run_dir):
    with pytest.raises(ToolError, match="read-only"):
        tools.execute("workspace_write", {"path": "reference/intro.md", "content": "vandalism"})
    assert (run_dir / "reference" / "intro.md").read_text() == "x" * 100


def test_write_quota_enforced(tools):
    tools.execute("workspace_write", {"path": "workspace/big.txt", "content": "x" * 900})
    with pytest.raises(ToolError, match="quota"):
        tools.execute("workspace_write", {"path": "workspace/more.txt", "content": "y" * 200})
    # Replacing the big file counts the delta, not the sum.
    tools.execute("workspace_write", {"path": "workspace/big.txt", "content": "z" * 990})
    with pytest.raises(ToolError, match="quota"):
        tools.execute("workspace_write", {"path": "workspace/big.txt", "content": "z" * 1001})


def test_rejected_write_leaves_no_partial_file(tools, run_dir):
    with pytest.raises(ToolError):
        tools.execute("workspace_write", {"path": "workspace/toobig.txt", "content": "x" * 1500})
    assert not (run_dir / "workspace" / "toobig.txt").exists()


# --- workspace_read ----------------------------------------------------------


def test_read_whole_file_and_reference(tools):
    tools.execute("workspace_write", {"path": "workspace/n.md", "content": "hello world"})
    assert tools.execute("workspace_read", {"path": "workspace/n.md"}) == "hello world"
    assert tools.execute("workspace_read", {"path": "reference/intro.md"}) == "x" * 100


def test_read_byte_slicing(tools):
    tools.execute("workspace_write", {"path": "workspace/n.md", "content": "0123456789"})
    assert tools.execute("workspace_read", {"path": "workspace/n.md", "offset": 3}) == "3456789"
    assert (
        tools.execute("workspace_read", {"path": "workspace/n.md", "offset": 2, "length": 4})
        == "2345"
    )
    assert tools.execute("workspace_read", {"path": "workspace/n.md", "length": 2}) == "01"
    assert tools.execute("workspace_read", {"path": "workspace/n.md", "offset": 50}) == ""


def test_read_errors(tools):
    with pytest.raises(ToolError, match="no such file"):
        tools.execute("workspace_read", {"path": "workspace/absent.md"})
    with pytest.raises(ToolError, match="offset"):
        tools.execute("workspace_read", {"path": "reference/intro.md", "offset": -1})
    with pytest.raises(ToolError, match="length"):
        tools.execute("workspace_read", {"path": "reference/intro.md", "length": -5})


# --- workspace_list ----------------------------------------------------------


def test_list_no_path_full_workspace_and_collapsed_reference(tools):
    tools.execute("workspace_write", {"path": "workspace/b.md", "content": "bbbb"})
    tools.execute("workspace_write", {"path": "workspace/a/x.md", "content": "1"})
    listing = tools.execute("workspace_list", {})
    assert listing.splitlines() == [
        "workspace/a/x.md 1",
        "workspace/b.md 4",
        "reference/ 2 files, 150 bytes, read-only",
    ]


def test_list_empty_workspace(tools):
    listing = tools.execute("workspace_list", {})
    assert listing.splitlines() == [
        "workspace/ (empty)",
        "reference/ 2 files, 150 bytes, read-only",
    ]


def test_list_reference_subtree_is_expandable(tools):
    listing = tools.execute("workspace_list", {"path": "reference"})
    assert listing.splitlines() == [
        "reference/intro.md 100",
        "reference/world/map.md 50",
    ]


def test_list_single_file_and_missing_path(tools):
    assert tools.execute("workspace_list", {"path": "reference/intro.md"}) == (
        "reference/intro.md 100"
    )
    with pytest.raises(ToolError, match="no such path"):
        tools.execute("workspace_list", {"path": "workspace/nope"})


# --- workspace_delete --------------------------------------------------------


def test_delete_file_and_emit(tools, run_dir, emitted):
    tools.execute("workspace_write", {"path": "workspace/dead.md", "content": "bye"})
    result = tools.execute("workspace_delete", {"path": "workspace/dead.md"})
    assert result == "Deleted workspace/dead.md."
    assert not (run_dir / "workspace" / "dead.md").exists()
    assert emitted[-1] == (
        "workspace_delete",
        {"path": "workspace/dead.md", "workspace_total_bytes": 0},
    )


def test_delete_rejections(tools, run_dir):
    with pytest.raises(ToolError, match="read-only"):
        tools.execute("workspace_delete", {"path": "reference/intro.md"})
    with pytest.raises(ToolError, match="no such file"):
        tools.execute("workspace_delete", {"path": "workspace/ghost.md"})
    tools.execute("workspace_write", {"path": "workspace/d/f.md", "content": "x"})
    with pytest.raises(ToolError, match="directory"):
        tools.execute("workspace_delete", {"path": "workspace/d"})


# --- set_next_wake -----------------------------------------------------------


def test_set_next_wake_clamps_and_last_call_wins(tools):
    tools.execute("set_next_wake", {"minutes_from_now": 1})
    assert (tools.requested_wake_min, tools.clamped_wake_min) == (1, 5.0)
    tools.execute("set_next_wake", {"minutes_from_now": 100000})
    assert (tools.requested_wake_min, tools.clamped_wake_min) == (100000, 1440.0)
    result = tools.execute("set_next_wake", {"minutes_from_now": 90})
    assert (tools.requested_wake_min, tools.clamped_wake_min) == (90, 90.0)
    assert result == "Next session in 90 minutes."


def test_set_next_wake_rejects_non_finite(tools):
    with pytest.raises(ToolError):
        tools.execute("set_next_wake", {"minutes_from_now": float("nan")})
    with pytest.raises(ToolError):
        tools.execute("set_next_wake", {"minutes_from_now": float("inf")})


# --- get_status (D12 minimalism) ---------------------------------------------


def test_get_status_exactly_four_fields(tools):
    tools.execute("workspace_write", {"path": "workspace/n.md", "content": "12345"})
    status = json.loads(tools.execute("get_status", {}))
    assert status == {
        "current_time_utc": "2026-07-08T12:00:00+00:00",
        "session_number": 7,
        "workspace_bytes_used": 5,
        "workspace_quota_bytes": 1000,
    }


def test_budget_visible_mechanism_pinned_off_by_default(run_dir):
    # The flag exists as mechanism for a future arm (D12); when enabled the
    # budget field is appended — for experiment 001 it never is.
    visible = ScaffoldTools(
        run_dir, budget_visible=True, budget_remaining_usd=42.5, clock=lambda: FIXED_NOW
    )
    assert json.loads(visible.execute("get_status", {}))["budget_remaining_usd"] == 42.5
    hidden = ScaffoldTools(run_dir, budget_remaining_usd=42.5, clock=lambda: FIXED_NOW)
    assert "budget_remaining_usd" not in json.loads(hidden.execute("get_status", {}))


# --- end_session -------------------------------------------------------------


def test_end_session_sets_flags(tools):
    assert tools.session_ended is False
    result = tools.execute("end_session", {"reason": "done for today"})
    assert result == "Session ended."
    assert tools.session_ended is True
    assert tools.end_reason == "done for today"


# --- dispatch + tool defs ----------------------------------------------------


def test_unknown_tool_and_bad_args(tools):
    with pytest.raises(ToolError, match="unknown tool"):
        tools.execute("shell_exec", {"cmd": "rm -rf /"})
    with pytest.raises(ToolError, match="invalid arguments"):
        tools.execute("workspace_read", {"file": "workspace/x"})


def test_tool_defs_cover_spec_surface():
    assert SCAFFOLD_TOOL_NAMES == {
        "workspace_write",
        "workspace_read",
        "workspace_list",
        "workspace_delete",
        "set_next_wake",
        "get_status",
        "end_session",
    }
    for tool in SCAFFOLD_TOOL_DEFS:
        assert tool.input_schema["type"] == "object"
        # Schemas stay within the tri-provider subset (SPEC §5.1).
        assert not {"oneOf", "anyOf", "allOf"} & tool.input_schema.keys()


def test_no_apparatus_leaks_in_agent_visible_tool_strings():
    # D12 / hard rule 2: nothing about budget, spend, horizon, caps, or the
    # study in any string the model can see.
    forbidden = [
        "budget",
        "spend",
        "usd",
        "cost",
        "token",
        "horizon",
        "t_max",
        "study",
        "experiment",
        "cap ",
        "capped",
    ]
    for tool in SCAFFOLD_TOOL_DEFS:
        visible = (tool.name + " " + tool.description + " " + json.dumps(tool.input_schema)).lower()
        for word in forbidden:
            assert word not in visible, f"{tool.name} leaks {word!r}"
