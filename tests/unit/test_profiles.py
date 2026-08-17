"""scaffold_profile: validation, surface, prompt assembly, recorded value.

SPEC D3 (the manifest key), P10 (the profile-selected surface and its
ordering rule), P13 (base prompt + pinned appendices), P9 (the value on
every session_start).
"""

import json
from pathlib import Path

import pytest
import yaml

from kami_agent import cli
from kami_agent.adapters.base import ToolDef
from kami_agent.harness import tools_hash
from kami_agent.runner import _load_prompts
from kami_agent.tools.scaffold import (
    ALL_SCAFFOLD_TOOL_NAMES,
    PROFILE_CONTROL,
    PROFILE_ORIENTATION,
    PROFILE_PLANNING,
    PROFILE_PUSHED,
    PROFILE_SEARCH,
    PROFILES,
    SCAFFOLD_TOOL_DEFS,
    SEARCH_TOOL_DEF,
    ScaffoldTools,
    profile_at_least,
    scaffold_tool_defs,
    scaffold_tool_names,
)

BASE_NAMES = {
    "workspace_write",
    "workspace_read",
    "workspace_list",
    "workspace_delete",
    "set_next_wake",
    "get_status",
    "end_session",
}

GAME_DEFS = [
    ToolDef(name="lens_node", description="d", input_schema={"type": "object", "properties": {}})
]


def manifest(**overrides):
    base = {
        "run_id": "profile-test",
        "provider": "anthropic",
        "model": "m",
        "price_table": {"input_usd_per_mtok": 1.0, "output_usd_per_mtok": 5.0},
        "caps": {"session_token_cap": 1000},
    }
    base.update(overrides)
    return base


# --- the ladder ---------------------------------------------------------------


def test_the_ladder_is_the_five_rungs_in_order():
    assert PROFILES == ("control", "orientation", "search", "pushed", "planning")


def test_profiles_are_cumulative():
    assert profile_at_least(PROFILE_PLANNING, PROFILE_SEARCH)
    assert profile_at_least(PROFILE_PUSHED, PROFILE_ORIENTATION)
    assert not profile_at_least(PROFILE_ORIENTATION, PROFILE_SEARCH)
    assert profile_at_least(PROFILE_CONTROL, PROFILE_CONTROL)


# --- manifest validation (D3) -------------------------------------------------


def test_absent_key_is_control(tmp_path):
    assert cli.build_run_config(manifest(), tmp_path).scaffold_profile == PROFILE_CONTROL


@pytest.mark.parametrize("profile", PROFILES)
def test_every_rung_is_accepted(tmp_path, profile):
    config = cli.build_run_config(manifest(scaffold_profile=profile), tmp_path)
    assert config.scaffold_profile == profile


def test_an_unknown_rung_fails_before_anything_starts(tmp_path):
    """Like an unknown provider: a manifest error, not a run (D3)."""
    with pytest.raises(SystemExit, match="unknown scaffold_profile 'orienting'"):
        cli.build_run_config(manifest(scaffold_profile="orienting"), tmp_path)


def test_the_example_manifest_pins_a_valid_rung():
    example = yaml.safe_load((Path(__file__).parents[2] / "manifests/example.yaml").read_text())
    assert example["scaffold_profile"] in PROFILES


# --- the surface (P10) --------------------------------------------------------


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        (PROFILE_CONTROL, BASE_NAMES),
        (PROFILE_ORIENTATION, BASE_NAMES),
        (PROFILE_SEARCH, BASE_NAMES | {"search_reference"}),
        (PROFILE_PUSHED, BASE_NAMES | {"search_reference"}),
        (PROFILE_PLANNING, BASE_NAMES | {"search_reference"}),
    ],
)
def test_the_surface_per_profile(profile, expected):
    assert scaffold_tool_names(profile) == expected


def test_profile_added_defs_come_after_the_base_ones():
    """The P10 ordering rule, and the reason a control hash does not move."""
    defs = scaffold_tool_defs(PROFILE_SEARCH)
    assert [t.name for t in defs[:-1]] == [t.name for t in SCAFFOLD_TOOL_DEFS]
    assert defs[-1] is SEARCH_TOOL_DEF


def test_control_and_orientation_hash_exactly_as_the_base_surface_does():
    """A profile that adds no tool must be byte-identical to 0.4.0's surface
    at the same harness pin — otherwise every control arm would carry a new
    tools_hash for no reason a reader could see."""
    baseline = tools_hash(GAME_DEFS + list(SCAFFOLD_TOOL_DEFS))
    assert tools_hash(GAME_DEFS + scaffold_tool_defs(PROFILE_CONTROL)) == baseline
    assert tools_hash(GAME_DEFS + scaffold_tool_defs(PROFILE_ORIENTATION)) == baseline


def test_a_profile_that_adds_a_tool_has_its_own_hash_by_design():
    baseline = tools_hash(GAME_DEFS + scaffold_tool_defs(PROFILE_CONTROL))
    added = tools_hash(GAME_DEFS + scaffold_tool_defs(PROFILE_SEARCH))
    assert added != baseline
    # And every profile above the search rung shares that one value.
    assert added == tools_hash(GAME_DEFS + scaffold_tool_defs(PROFILE_PUSHED))
    assert added == tools_hash(GAME_DEFS + scaffold_tool_defs(PROFILE_PLANNING))


def test_the_collision_check_covers_every_profiles_names():
    """A mis-pinned harness must be refused identically on every arm."""
    assert ALL_SCAFFOLD_TOOL_NAMES == BASE_NAMES | {"search_reference"}
    assert scaffold_tool_names(PROFILE_CONTROL) < ALL_SCAFFOLD_TOOL_NAMES


def test_every_profiles_schemas_stay_in_the_tri_provider_subset():
    for profile in PROFILES:
        for tool in scaffold_tool_defs(profile):
            assert tool.input_schema["type"] == "object"
            assert not {"oneOf", "anyOf", "allOf"} & tool.input_schema.keys()


def test_no_apparatus_leaks_in_any_profiles_tool_strings():
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
    for profile in PROFILES:
        for tool in scaffold_tool_defs(profile):
            visible = (
                tool.name + " " + tool.description + " " + json.dumps(tool.input_schema)
            ).lower()
            for word in forbidden:
                assert word not in visible, f"{tool.name} contains {word!r}"


# --- prompt assembly (P13) ----------------------------------------------------


def materialize(run_dir, *, skip=()):
    prompts = run_dir / "prompts"
    prompts.mkdir(parents=True, exist_ok=True)
    source = cli._prompts_source()
    for name in cli.PROMPT_NAMES:
        if name in skip:
            continue
        (prompts / name).write_text((source / name).read_text(encoding="utf-8"), encoding="utf-8")


@pytest.mark.parametrize(
    ("profile", "parts"),
    [
        (PROFILE_CONTROL, 1),
        (PROFILE_ORIENTATION, 2),
        (PROFILE_SEARCH, 2),
        (PROFILE_PUSHED, 2),
        (PROFILE_PLANNING, 3),
    ],
)
def test_the_system_prompt_gains_one_appendix_per_rung(tmp_path, profile, parts):
    materialize(tmp_path)
    loaded = _load_prompts(tmp_path, profile)["system_parts"]
    assert len(loaded) == parts
    # Base first, then orientation, then planning — the ladder's order.
    assert loaded[0].startswith("You are an autonomous agent in Kamigotchi")
    if parts >= 2:
        assert loaded[1].startswith("You own kamis (creatures).")
    if parts == 3:
        assert loaded[2].startswith("The file workspace/plan.md")


def test_the_gas_sentence_is_in_every_profiles_prompt(tmp_path):
    materialize(tmp_path)
    for profile in PROFILES:
        joined = "\n\n".join(_load_prompts(tmp_path, profile)["system_parts"])
        assert "Gas is paid in ETH from your wallets" in joined


def test_a_missing_appendix_fails_loudly_and_names_the_rung(tmp_path):
    """Fails before the harness spawn and before any telemetry: a
    mis-provisioned arm must not half-start a session."""
    materialize(tmp_path, skip=("orientation.txt",))
    with pytest.raises(FileNotFoundError, match="'orientation' needs prompts/orientation.txt"):
        _load_prompts(tmp_path, PROFILE_ORIENTATION)
    # A profile that does not need it is unaffected.
    assert _load_prompts(tmp_path, PROFILE_CONTROL)["system_parts"]


def test_the_appendices_are_never_in_the_control_prompt(tmp_path):
    materialize(tmp_path)
    joined = "\n\n".join(_load_prompts(tmp_path, PROFILE_CONTROL)["system_parts"])
    assert "You own kamis" not in joined
    assert "plan.md" not in joined


# --- the recorded value -------------------------------------------------------


def test_the_scaffold_carries_its_profile_and_surface(tmp_path):
    tools = ScaffoldTools(tmp_path, profile=PROFILE_SEARCH)
    assert tools.profile == PROFILE_SEARCH
    assert tools.tool_names == scaffold_tool_names(PROFILE_SEARCH)
    assert [t.name for t in tools.tool_defs] == [t.name for t in scaffold_tool_defs(PROFILE_SEARCH)]
