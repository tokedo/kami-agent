"""The session-start file index, bounded (SPEC P1 step 11; 0.8.0, ARTEFACTS D.4 / E.8).

From 0.8.0 the workspace is also the home of a persistent environment, and
a home accumulates what an index should not carry into every session: a
virtualenv is hundreds of files. The index leaves out hidden entries and
four build directories, and caps the rest with one "more" line. The
`workspace_list` TOOL still lists everything.
"""

import os
from pathlib import Path

from kami_agent.runner import RunConfig, run_session
from kami_agent.tools.scaffold import ScaffoldTools


def populate(workspace: Path, n: int) -> None:
    for i in range(n):
        path = workspace / f"notes/n{i:03d}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
    for hidden in (".venv/lib/site.py", ".bashrc", "notes/.cache", ".git/HEAD"):
        path = workspace / hidden
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("h")
    for build in (
        "__pycache__/a.pyc",
        "node_modules/m/i.js",
        "venv/bin/python",
        "tool/__pycache__/b.pyc",
    ):
        path = workspace / build
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("b")


def test_a_big_home_is_capped_at_two_hundred_entries_and_a_more_line(tmp_path):
    tools = ScaffoldTools(tmp_path)
    populate(tools.workspace_root, 300)
    lines = tools.file_index().splitlines()
    entries = [line for line in lines if line.startswith("workspace/")]
    assert len(entries) == 200
    assert entries[0] == "workspace/notes/n000.md 1"
    assert "… and 100 more entries" in lines
    assert lines.index("… and 100 more entries") == 200
    joined = "\n".join(lines)
    for skipped in (".venv", ".bashrc", ".cache", ".git", "__pycache__", "node_modules", "venv/"):
        assert skipped not in joined, skipped


def test_at_the_cap_there_is_no_more_line(tmp_path):
    tools = ScaffoldTools(tmp_path)
    populate(tools.workspace_root, 200)
    index = tools.file_index()
    assert "more entries" not in index
    assert len([line for line in index.splitlines() if line.startswith("workspace/")]) == 200


def test_the_journal_and_reference_lines_follow_as_before(tmp_path):
    (tmp_path / "reference").mkdir()
    (tmp_path / "reference" / "a.md").write_text("abc")
    tools = ScaffoldTools(tmp_path)
    (tools.workspace_root / "plan.md").write_text("p")
    assert tools.file_index().splitlines() == [
        "workspace/plan.md 1",
        "journal/ 0 files, 0 bytes, read-only",
        "reference/ 1 files, 3 bytes, read-only",
    ]
    empty = ScaffoldTools(tmp_path / "other")
    assert empty.file_index().splitlines()[0] == "workspace/ (empty)"


def test_the_workspace_list_tool_still_lists_everything(tmp_path):
    tools = ScaffoldTools(tmp_path)
    populate(tools.workspace_root, 300)
    listing = tools.execute("workspace_list", {})
    assert "workspace/.venv/lib/site.py 1" in listing
    assert "workspace/notes/n299.md 1" in listing
    assert "more entries" not in listing


def test_a_session_prompt_carries_the_bounded_index(tmp_path):
    from fake_adapter_support import end_only_adapter, materialized

    run_dir = materialized(tmp_path)
    populate(run_dir / "workspace", 250)
    adapter = end_only_adapter()
    run_session(
        RunConfig(
            run_dir=run_dir,
            run_id="r",
            model="m",
            prices=adapter.prices,
            caps=adapter.caps,
            scaffold_profile="planning",
        ),
        adapter,
    )
    system = adapter.requests[0]["system"]
    assert "… and 50 more entries" in system
    assert ".venv" not in system


def test_a_symlink_is_never_followed_by_a_listing_or_a_byte_count(tmp_path):
    """The agent's own shell can link to a file outside the workspace — a
    run-directory internal, say. Its size must not reach a listing the
    agent is shown, nor the workspace's byte count."""
    tools = ScaffoldTools(tmp_path)
    secret = tmp_path / "telemetry.jsonl"
    secret.write_text("z" * 4321)
    (tools.workspace_root / "real.md").write_text("r")
    os.symlink(secret, tools.workspace_root / "link.md")
    os.symlink(tmp_path, tools.workspace_root / "dirlink")
    assert "4321" not in tools.file_index()
    assert "link" not in tools.execute("workspace_list", {})
    assert tools.workspace_bytes_used() == 1
