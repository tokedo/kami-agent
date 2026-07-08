"""Path sandbox: no traversal escape, ever (SPEC §4, brief §3.4)."""

import tempfile
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

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
        "telemetry.jsonl",
        "state.json",
        "prompts/system.txt",
        "",
        ".",
        "workspace/notes/../../../kami",
        "bad\x00path",
    ],
)
def test_escapes_and_run_dir_internals_rejected(run_dir, path):
    with pytest.raises(SandboxError):
        resolve_path(run_dir, path)


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
