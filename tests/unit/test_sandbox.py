"""Path sandbox: no traversal escape, ever (SPEC P11, I11)."""

import tempfile
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kami_agent.tools.errors import ToolError
from kami_agent.tools.sandbox import SandboxError, resolve_path


@pytest.fixture(scope="module")
def run_dir():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "workspace" / "notes").mkdir(parents=True)
        (root / "workspace" / "notes" / "a.md").write_text("hello")
        (root / "reference").mkdir()
        (root / "reference" / "gdd.md").write_text("world")
        (root / "telemetry.jsonl").write_text("")
        (root / "state.json").write_text("{}")
        yield root


def test_workspace_and_reference_paths_resolve(run_dir):
    resolved, root = resolve_path(run_dir, "workspace/notes/a.md")
    assert root == "workspace"
    assert resolved == (run_dir / "workspace" / "notes" / "a.md").resolve()
    resolved, root = resolve_path(run_dir, "reference/gdd.md")
    assert root == "reference"


def test_bare_paths_are_workspace_relative(run_dir):
    # Paths are relative to the workspace root: "notes/a.md" and
    # "workspace/notes/a.md" name the same file.
    bare = resolve_path(run_dir, "notes/a.md")
    prefixed = resolve_path(run_dir, "workspace/notes/a.md")
    assert bare == prefixed
    assert bare[1] == "workspace"


def test_exactly_one_leading_workspace_segment_is_stripped(run_dir):
    # A workspace subtree literally named workspace/ stays addressable.
    resolved, root = resolve_path(run_dir, "workspace/workspace/n.md")
    assert root == "workspace"
    assert resolved == (run_dir / "workspace" / "workspace" / "n.md").resolve()
    # A workspace subtree named reference/ is only reachable via the
    # workspace/ prefix; a bare "reference/..." addresses the read-only tree.
    resolved, root = resolve_path(run_dir, "workspace/reference/n.md")
    assert root == "workspace"
    assert resolved == (run_dir / "workspace" / "reference" / "n.md").resolve()


def test_roots_themselves_resolve(run_dir):
    assert resolve_path(run_dir, "workspace")[1] == "workspace"
    assert resolve_path(run_dir, "reference")[1] == "reference"


def test_internal_dot_segments_that_stay_inside_are_fine(run_dir):
    resolved, root = resolve_path(run_dir, "workspace/notes/../notes/./a.md")
    assert root == "workspace"
    assert resolved.name == "a.md"


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "~/secrets",
        "~",
        "..",
        "../x",
        "workspace/../telemetry.jsonl",
        "workspace/../../etc/passwd",
        "reference/../state.json",
        "workspace/../prompts/system.txt",
        "workspace/notes/../../../kami",
        "bad\x00path",
    ],
)
def test_escapes_rejected(run_dir, path):
    with pytest.raises(SandboxError):
        resolve_path(run_dir, path)


@pytest.mark.parametrize("path", ["telemetry.jsonl", "state.json", "prompts/system.txt"])
def test_bare_paths_cannot_reach_run_dir_internals(run_dir, path):
    # Workspace-relative bare paths land inside workspace/ — never on the
    # run-directory files they happen to be named after.
    resolved, root = resolve_path(run_dir, path)
    assert root == "workspace"
    assert resolved.is_relative_to((run_dir / "workspace").resolve())
    assert resolved != (run_dir / path).resolve()


_SEGMENTS = st.one_of(
    st.sampled_from(["..", ".", "workspace", "reference", "a", "b.txt", "...", "~", " "]),
    st.text(
        alphabet="abcXYZ019._-~ ",
        min_size=1,
        max_size=10,
    ),
)


@given(st.lists(_SEGMENTS, min_size=1, max_size=6), st.sampled_from(["", "/"]))
def test_property_resolution_never_escapes(run_dir, segments, prefix):
    """Any input either raises SandboxError or lands strictly inside a root."""
    path = prefix + "/".join(segments)
    try:
        resolved, root = resolve_path(run_dir, path)
    except SandboxError:
        return
    assert root in ("workspace", "reference")
    root_dir = (run_dir / root).resolve()
    assert resolved == root_dir or resolved.is_relative_to(root_dir)


def test_symlink_escaping_the_root_is_rejected(run_dir):
    link = run_dir / "workspace" / "sneaky"
    link.symlink_to(run_dir / "state.json")
    try:
        with pytest.raises(SandboxError):
            resolve_path(run_dir, "workspace/sneaky")
    finally:
        link.unlink()


# --- P11 through the TOOLS, not just the resolver ------------------------------
#
# The resolver tests above prove one leading `workspace/` segment is
# stripped. What was never asserted is that every workspace tool agrees —
# and a run-006 analysis concluded, from telemetry alone, that an arm had
# split its notes across `notes/` and `workspace/notes/` as two parallel
# trees. The archived workspace showed ONE tree: the same 102 writes had
# simply been issued 41 times bare and 61 times prefixed, and the analysis
# had grouped on `tool_call.path`, which is the agent's raw argument. The
# claim held; the reading of it did not. These tests pin the claim at the
# tool level so it can never quietly stop holding, and P9 now carries
# `path_resolved` so the reading has a field that cannot mislead.


def test_the_two_spellings_are_one_file_through_every_workspace_tool(run_dir):
    from kami_agent.tools.scaffold import ScaffoldTools

    tools = ScaffoldTools(run_dir)
    tools.execute("workspace_write", {"path": "notes/session01.md", "content": "first"})
    tools.execute("workspace_write", {"path": "workspace/notes/session01.md", "content": "second"})

    # One write replaced the other: the prefixed spelling named the same file.
    assert tools.execute("workspace_read", {"path": "notes/session01.md"}) == "second"
    assert tools.execute("workspace_read", {"path": "workspace/notes/session01.md"}) == "second"

    # One tree on disk, and one entry in the listing — never workspace/workspace/.
    listing = tools.execute("workspace_list", {}).splitlines()
    assert [line for line in listing if "session01" in line] == ["workspace/notes/session01.md 6"]
    assert not (run_dir / "workspace" / "workspace").exists()

    # Either spelling deletes it, and there is nothing left to delete.
    tools.execute("workspace_delete", {"path": "workspace/notes/session01.md"})
    with pytest.raises(ToolError):
        tools.execute("workspace_read", {"path": "notes/session01.md"})


def test_a_subtree_listing_agrees_across_both_spellings(run_dir):
    from kami_agent.tools.scaffold import ScaffoldTools

    tools = ScaffoldTools(run_dir)
    tools.execute("workspace_write", {"path": "state/a.json", "content": "1"})
    tools.execute("workspace_write", {"path": "workspace/state/b.json", "content": "22"})
    bare = tools.execute("workspace_list", {"path": "state"})
    prefixed = tools.execute("workspace_list", {"path": "workspace/state"})
    assert bare == prefixed
    assert bare.splitlines() == ["workspace/state/a.json 1", "workspace/state/b.json 2"]


def test_telemetry_carries_both_the_argument_and_the_file_it_named(run_dir):
    """`path` is what the agent typed; `path_resolved` is what it named (P9)."""
    from kami_agent.tools.scaffold import ScaffoldTools

    tools = ScaffoldTools(run_dir)
    assert tools.resolve_rel("notes/x.md") == "workspace/notes/x.md"
    assert tools.resolve_rel("workspace/notes/x.md") == "workspace/notes/x.md"
    assert tools.resolve_rel("reference/gdd.md") == "reference/gdd.md"
    # Nothing to resolve, nothing recorded.
    assert tools.resolve_rel("/etc/passwd") is None
    assert tools.resolve_rel("../../telemetry.jsonl") is None
