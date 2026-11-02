"""The two capability rungs, `shell` and `shell_keeper` (SPEC D3, P10, P13; 0.8.0).

`shell` = `planning` + the `shell` tool + its appendix; `shell_keeper` =
`shell` + a keeper reachable from that shell + its appendix, and no tool
of its own. The appendices and the tool description are frozen byte-exact
here, and scrubbed for policy language with the keeper's own list (E.4).
"""

import json
import re
from pathlib import Path

import pytest

from kami_agent import cli
from kami_agent.adapters.base import ToolDef
from kami_agent.harness import tools_hash
from kami_agent.runner import _load_prompts
from kami_agent.tools import scaffold

PROMPTS = Path(__file__).parents[2] / "prompts"

GAME_DEFS = [
    ToolDef(name="lens_node", description="d", input_schema={"type": "object", "properties": {}})
]

# The frozen texts (design sitting 2, ARTEFACTS A.1–A.3; A.1 amended to
# name procps, which the environment image installs). Any change is a
# re-freeze in the same commit, and a change to what the shell arms are
# measured on.
SHELL = (
    "The shell tool runs a bash command in an environment of your own. Its working "
    "directory is /workspace, the same directory the workspace tools read and write; "
    "/reference holds the same documentation the workspace tools read, and /journal the "
    "same session journal. Files, installed state and background processes in the "
    "environment persist between calls and sessions until the run ends. The environment "
    "has no network and holds no wallet key. Installed: bash, coreutils, procps, grep, sed, "
    "awk, jq, git, sqlite3, python3, node, less, nano, and the kami-lens command, whose "
    "--help documents the world-state reader it reaches.\n"
)
SHELL_KEEPER = (
    "A keeper daemon is reachable from the shell as the keeper command. keeper --help and "
    "keeper describe are its documentation. Once granted for this run's account, it signs "
    "with the account's operator key; a grant is made and removed through it.\n"
)
SHELL_DESCRIPTION = (
    "Run a bash command in your environment. The working directory is the workspace; "
    "files, installed state and background processes persist between calls and sessions "
    "until the run ends. Output is truncated past a size limit. A timeout in seconds "
    "bounds the foreground command; values outside the allowed range are clamped."
)
SHELL_SCHEMA = {
    "type": "object",
    "properties": {"command": {"type": "string"}, "timeout_s": {"type": "number"}},
    "required": ["command"],
}

# E.4: the keeper's own forbidden-word list and phrase patterns (its
# keeper/scrub.py, whole-word and case-insensitive), plus four words of
# advice. A frozen text that carries any of them is policy, not mechanism.
KEEPER_FORBIDDEN_WORDS = (
    "predator",
    "enemy",
    "danger",
    "dangerous",
    "threat",
    "safe",
    "unsafe",
    "safety",
    "trusted",
    "saves",
    "efficient",
    "efficiently",
    "consider",
    "harmless",
    "armed",
    "unarmed",
    "hostile",
    "passive",
)
ADVICE_WORDS = ("should", "best", "recommend", "good")
KEEPER_FORBIDDEN_PATTERNS = (
    r"\bmusu\s*/\s*(day|hour|hr|h|min)\b",
    r"\bper[-\s](day|hour|kami[-\s]day)\b",
    r"\bbudget(s|ed)?\b",
    r"\bdeadline(s)?\b",
    r"\brun[-\s]window\b",
)


def scrub_hits(text: str) -> list[str]:
    words = KEEPER_FORBIDDEN_WORDS + ADVICE_WORDS
    word_re = re.compile(r"\b(" + "|".join(re.escape(w) for w in words) + r")\b", re.I)
    hits = [m.group(0) for m in word_re.finditer(text)]
    for pattern in KEEPER_FORBIDDEN_PATTERNS:
        hits.extend(m.group(0) for m in re.finditer(pattern, text, re.I))
    return hits


# --- the ladder (D3) ----------------------------------------------------------------


def test_the_ladder_gains_two_capability_rungs_after_planning():
    assert scaffold.PROFILES[-3:] == ("planning", "shell", "shell_keeper")
    assert scaffold.PROFILE_SHELL == "shell"
    assert scaffold.PROFILE_SHELL_KEEPER == "shell_keeper"
    assert scaffold.profile_at_least("shell_keeper", "shell")
    assert scaffold.profile_at_least("shell", "planning")
    assert not scaffold.profile_at_least("planning", "shell")


def test_a_shell_rung_without_an_environment_fails_before_anything_starts(tmp_path, monkeypatch):
    monkeypatch.delenv("KAMI_ENV_CONTAINER", raising=False)
    manifest = {
        "run_id": "r",
        "provider": "anthropic",
        "model": "m",
        "price_table": {"input_usd_per_mtok": 1.0, "output_usd_per_mtok": 5.0},
        "caps": {"session_token_cap": 1000},
        "scaffold_profile": "shell",
    }
    with pytest.raises(SystemExit, match="needs environment.container"):
        cli.build_run_config(manifest, tmp_path)
    manifest["environment"] = {"container": "kami-env"}
    assert cli.build_run_config(manifest, tmp_path).scaffold_profile == "shell"


def test_the_shell_timeout_is_a_manifest_cap(tmp_path):
    manifest = {
        "run_id": "r",
        "provider": "anthropic",
        "model": "m",
        "price_table": {"input_usd_per_mtok": 1.0, "output_usd_per_mtok": 5.0},
        "caps": {"session_token_cap": 1000, "shell_timeout_s": 45},
    }
    assert cli.build_run_config(manifest, tmp_path).caps.shell_timeout_s == 45
    del manifest["caps"]["shell_timeout_s"]
    assert cli.build_run_config(manifest, tmp_path).caps.shell_timeout_s == 120


# --- the surface (P10, X24) ---------------------------------------------------------


def test_the_shell_tool_is_exactly_as_frozen():
    tool = scaffold.SHELL_TOOL_DEF
    assert tool.name == "shell"
    assert tool.description == SHELL_DESCRIPTION
    assert tool.input_schema == SHELL_SCHEMA


def test_shell_rungs_carry_the_tool_and_lower_rungs_do_not():
    for profile in ("shell", "shell_keeper"):
        assert "shell" in scaffold.scaffold_tool_names(profile)
    for profile in ("control", "orientation", "search", "pushed", "planning"):
        assert "shell" not in scaffold.scaffold_tool_names(profile)


def test_the_two_shell_rungs_hash_alike_and_differ_from_planning_by_one_definition():
    planning = scaffold.scaffold_tool_defs("planning")
    shell = scaffold.scaffold_tool_defs("shell")
    keeper = scaffold.scaffold_tool_defs("shell_keeper")
    assert shell == keeper
    assert shell[:-1] == planning
    assert shell[-1] is scaffold.SHELL_TOOL_DEF
    assert tools_hash(GAME_DEFS + shell) == tools_hash(GAME_DEFS + keeper)
    assert tools_hash(GAME_DEFS + shell) != tools_hash(GAME_DEFS + planning)


def test_a_harness_tool_named_shell_is_refused_on_every_arm():
    assert "shell" in scaffold.ALL_SCAFFOLD_TOOL_NAMES


def test_a_profile_below_shell_cannot_run_it_by_name(tmp_path):
    tools = scaffold.ScaffoldTools(tmp_path, profile="planning")
    with pytest.raises(Exception, match="unknown tool: shell"):
        tools.execute("shell", {"command": "true"})


# --- the appendices (P13) -----------------------------------------------------------


def test_the_two_appendices_are_exactly_as_frozen():
    assert (PROMPTS / "shell.txt").read_text(encoding="utf-8") == SHELL
    assert (PROMPTS / "shell_keeper.txt").read_text(encoding="utf-8") == SHELL_KEEPER


def test_the_packaged_appendices_match_the_repo_copies():
    from importlib import resources

    packaged = resources.files("kami_agent") / "prompts"
    for name in ("shell.txt", "shell_keeper.txt"):
        assert (packaged / name).read_text(encoding="utf-8") == (PROMPTS / name).read_text(
            encoding="utf-8"
        )


def test_init_materializes_both_appendices():
    assert {"shell.txt", "shell_keeper.txt"} <= set(cli.PROMPT_NAMES)


@pytest.mark.parametrize(
    "text", [SHELL, SHELL_KEEPER, SHELL_DESCRIPTION], ids=["shell", "shell_keeper", "tool"]
)
def test_the_frozen_texts_carry_no_policy_language(text):
    """E.4: zero hits, by the keeper's own scrub plus the advice words."""
    assert scrub_hits(text) == []


def test_the_scrub_would_catch_what_it_is_for():
    assert scrub_hits("It is safe to consider the best keeper budget per-day.") == [
        "safe",
        "consider",
        "best",
        "per-day",
        "budget",
    ]


def materialize(run_dir, *, skip=()):
    prompts = run_dir / "prompts"
    prompts.mkdir(parents=True, exist_ok=True)
    source = cli._prompts_source()
    for name in cli.PROMPT_NAMES:
        if name in skip:
            continue
        (prompts / name).write_text((source / name).read_text(encoding="utf-8"), encoding="utf-8")


def test_the_system_prompt_appends_the_shell_appendices_in_ladder_order(tmp_path):
    materialize(tmp_path)
    shell = _load_prompts(tmp_path, "shell")["system_parts"]
    keeper = _load_prompts(tmp_path, "shell_keeper")["system_parts"]
    assert len(shell) == 4 and len(keeper) == 5
    assert shell[2].startswith("The file workspace/plan.md")
    assert shell[3] == SHELL.rstrip("\n")
    assert keeper[:4] == shell
    assert keeper[4] == SHELL_KEEPER.rstrip("\n")
    planning = "\n\n".join(_load_prompts(tmp_path, "planning")["system_parts"])
    assert "shell" not in planning


def test_a_missing_shell_appendix_starts_no_session(tmp_path):
    materialize(tmp_path, skip=("shell_keeper.txt",))
    with pytest.raises(FileNotFoundError, match="'shell_keeper' needs prompts/shell_keeper.txt"):
        _load_prompts(tmp_path, "shell_keeper")
    assert _load_prompts(tmp_path, "shell")["system_parts"]


def test_the_plain_shell_rung_never_names_a_keeper(tmp_path):
    """The control arm of the keeper must not learn that one exists (design C8)."""
    materialize(tmp_path)
    joined = "\n\n".join(_load_prompts(tmp_path, "shell")["system_parts"])
    tool = json.dumps(
        [
            {"name": t.name, "description": t.description, "schema": t.input_schema}
            for t in scaffold.scaffold_tool_defs("shell")
        ]
    )
    assert "keeper" not in joined.lower()
    assert "keeper" not in tool.lower()
