"""The frozen prompt assets (SPEC P13): exact content + leak discipline.

Three strings ship for every run and two more are the profile appendices
(P10, P13): each is a separate pinned file, frozen byte-exact here, so
what a rung showed the agent is an artifact rather than a string built at
runtime. Any reword is deliberate and re-freezes its literal in the same
commit.
"""

import re
from pathlib import Path

import pytest

PROMPTS = Path(__file__).parents[2] / "prompts"

ASSET_NAMES = ("system.txt", "kickoff.txt", "continue.txt", "orientation.txt", "planning.txt")

# The frozen wording, reviewed and approved. Any change is deliberate:
# update this test in the same commit that re-freezes the string.
SYSTEM = """\
You are an autonomous agent in Kamigotchi, a persistent on-chain world shared with other players. You act in periodic sessions; the world advances between them. You act only through tool calls. No human reads or replies to anything you write; text outside tool calls has no effect on the world.

Your objective is to complete as many quests as possible.

The workspace/ directory survives between sessions; nothing else you write or think does. Its use and structure are entirely up to you.

The reference/ directory holds the game's design document. It is read-only.

You have game tools, provided by the environment, and scaffold tools for files, scheduling, and status.

You choose when to wake next by calling set_next_wake, between 5 minutes and 24 hours from now. You cannot wait or pause within a session. To wait for something, choose your next wake with set_next_wake and end the session with end_session.

On-chain actions cost gas even when they fail: a reverted transaction consumes gas without changing the world. Diagnose why an action failed before submitting it again. Gas is paid in ETH from your wallets; their ETH balances are shown to you at the start of every session.
"""

KICKOFF = "Session start.\n"

CONTINUE = "Continue. To end this session, call end_session.\n"

# Appendix for profiles at or above `orientation` (P13). Every sentence is a
# rule of the world; none is a recommendation. Verified against the pinned
# design document at build time, and pinned by the family's design — a
# reword here is a change to what those arms were measured on.
ORIENTATION = """\
You own kamis (creatures). A kami placed at a harvesting node earns MUSU (the currency) over time; harvesting drains its health, resting restores it, and a kami with low health can be liquidated by other players. MUSU buys items; food restores health. Harvesting earns experience; experience lets a kami level up, which grants a skill point spent on skills that change its stats. Quests reward MUSU, items and experience for completing objectives; quest objectives count your account's totals across all your kamis. Every on-chain action costs gas (ETH).
"""

# Appendix for the `planning` profile (P13). Mechanism about a file, not
# advice about what to plan.
PLANNING = """\
The file workspace/plan.md is where your goals and plan live. Its contents are shown to you at the start of every session. Keeping it current is up to you.
"""

# I1: no budget, cost, tokens, compute limits, run duration, session caps,
# forced truncation, or study existence. I3: no strategy hints, no vendor
# idioms, no XML-tag formatting. Applied to every agent-visible string the
# scaffold composes, not only the three prompts.
FORBIDDEN = [
    "budget",
    "cost",
    "token",
    "spend",
    "usd",
    "horizon",
    "limit",
    "cap",
    "truncat",
    "study",
    "experiment",
    "benchmark",
    "measure",
    "step by step",
    "think carefully",
    "<",
    ">",
]


def test_frozen_strings_are_exactly_as_reviewed():
    assert (PROMPTS / "system.txt").read_text(encoding="utf-8") == SYSTEM
    assert (PROMPTS / "kickoff.txt").read_text(encoding="utf-8") == KICKOFF
    assert (PROMPTS / "continue.txt").read_text(encoding="utf-8") == CONTINUE


def test_profile_appendices_are_exactly_as_reviewed():
    """Each rung's appendix is frozen on the same terms as the base prompt."""
    assert (PROMPTS / "orientation.txt").read_text(encoding="utf-8") == ORIENTATION
    assert (PROMPTS / "planning.txt").read_text(encoding="utf-8") == PLANNING


def test_the_gas_sentence_states_the_resource_and_where_it_is_shown():
    """Gas visibility is one sentence of the FIXED prompt, on every profile.

    It names the resource (ETH), whose it is (the agent's wallets), and
    that the balances arrive at session start — the fact that makes the
    session-start injection legible instead of surprising. No numbers: the
    balances themselves are world state, injected as a tool result, never
    prompt text.
    """
    sentence = (
        "Gas is paid in ETH from your wallets; their ETH balances are shown "
        "to you at the start of every session."
    )
    assert sentence in SYSTEM
    assert not any(ch.isdigit() for ch in sentence)


@pytest.mark.parametrize("name", ASSET_NAMES)
def test_no_apparatus_or_policy_leaks(name):
    # I1: no budget, cost, tokens, compute limits, run duration, session
    # caps, forced truncation, or study existence. I3: no strategy hints,
    # no vendor idioms, no XML-tag formatting.
    # Gas is a world mechanic, not apparatus (SPEC P7.4: in-world resources
    # are outside budget_usd): "cost gas" / "costs gas" is the one allowed
    # use of "cost"; any other occurrence still fails. Both forms are
    # carved out because both are in pinned text — the base prompt uses
    # one and the orientation appendix the other.
    text = (PROMPTS / name).read_text(encoding="utf-8").lower()
    text = re.sub(r"\bcosts? gas\b", "", text)
    for word in FORBIDDEN:
        assert word not in text, f"{name} contains {word!r}"


def test_packaged_prompts_match_repo_prompts():
    # The wheel ships the frozen strings as package data (kami_agent/prompts)
    # so `init` works under any install; the repo-root prompts/ tree is the
    # brief's canonical layout. The two copies must stay byte-identical.
    from importlib import resources

    packaged = resources.files("kami_agent") / "prompts"
    for name in ASSET_NAMES:
        assert (packaged / name).read_text(encoding="utf-8") == (PROMPTS / name).read_text(
            encoding="utf-8"
        ), f"packaged {name} diverges from prompts/{name}"


def test_init_materializes_every_asset():
    """`init` writes all five, so a profile can never lack its appendix."""
    from kami_agent.cli import PROMPT_NAMES

    assert set(PROMPT_NAMES) == set(ASSET_NAMES)


def test_wake_bounds_in_frozen_prompt_match_code_defaults():
    # The frozen prompt hardcodes the P6 default wake bounds. A manifest or
    # code change to wake_min/wake_max must consciously re-freeze the prompt
    # (and this test) in the same commit — they can never silently diverge.
    from kami_agent.runner import RunConfig
    from kami_agent.tools.scaffold import DEFAULT_WAKE_MAX_MINUTES, DEFAULT_WAKE_MIN_MINUTES

    phrase = (
        f"between {DEFAULT_WAKE_MIN_MINUTES:g} minutes and {DEFAULT_WAKE_MAX_MINUTES / 60:g} hours"
    )
    assert phrase in SYSTEM
    config_defaults = RunConfig.__dataclass_fields__
    assert config_defaults["wake_min_minutes"].default == DEFAULT_WAKE_MIN_MINUTES
    assert config_defaults["wake_max_minutes"].default == DEFAULT_WAKE_MAX_MINUTES


def test_kickoff_and_continue_carry_no_dynamic_content():
    # Frozen constants: no numbers, no timestamps (SPEC P1.11).
    for name in ("kickoff.txt", "continue.txt"):
        text = (PROMPTS / name).read_text(encoding="utf-8")
        assert not any(ch.isdigit() for ch in text), f"{name} contains digits"


# --- the one other agent-visible string the scaffold authors ------------------
#
# A daemon that cannot be reached has no words of its own to quote, so the
# session-start brief injects this record instead (SPEC X21, D7). It is the
# only agent-visible text the scaffold composes outside the three prompts
# above, so it is frozen on the same terms: reviewed wording, changed only
# deliberately, in the commit that re-freezes it.


def test_the_unavailable_lens_record_is_exactly_as_reviewed():
    import json

    from kami_agent.lens import LensUnavailableError

    record = LensUnavailableError("cannot connect to /run/lens.sock: [Errno 2]").as_record()
    assert record == (
        '{"error": {"code": "LENS_UNAVAILABLE", '
        '"message": "cannot connect to /run/lens.sock: [Errno 2]"}}'
    )
    # Machine-shaped, not prose: no advice, no judgement, no instruction to
    # the agent about what to do next.
    parsed = json.loads(record)
    assert set(parsed) == {"error"}
    assert set(parsed["error"]) == {"code", "message"}


def test_the_scaffold_authors_only_the_code_of_that_record():
    """The message half is the operating system's, verbatim."""
    from kami_agent.lens import CODE_UNAVAILABLE, LensUnavailableError

    os_text = "[Errno 111] Connection refused"
    assert LensUnavailableError(os_text).message == os_text
    assert CODE_UNAVAILABLE == "LENS_UNAVAILABLE"


def test_the_unavailable_record_leaks_no_apparatus_vocabulary():
    """I1 applies to it exactly as it does to the three prompts."""
    from kami_agent.lens import LensUnavailableError

    text = LensUnavailableError("socket error: broken pipe").as_record().lower()
    for word in FORBIDDEN:
        assert word not in text


# --- the two strings 0.5.0 adds to that set ----------------------------------
#
# The gas-balance injection authors NO new string on its happy path (the
# harness's payload) or its failure path (the harness's own words). The one
# case it composes is a surface without the balance tool, and it composes it
# by reusing the loop's existing unknown-tool wording rather than inventing
# a record — so the agent sees exactly what any missing name yields.


def test_a_missing_balance_tool_reuses_the_unknown_tool_wording():
    from kami_agent.loop import BALANCE_TOOL

    assert f"unknown tool: {BALANCE_TOOL}" == "unknown tool: get_gas_balance"
    for word in FORBIDDEN:
        assert word not in f"unknown tool: {BALANCE_TOOL}".lower()


def test_the_empty_reference_index_answer_is_exactly_as_reviewed(tmp_path):
    """search_reference over an empty tree says so, rather than returning
    an empty list a reader could take for 'nothing matched'."""
    import json

    from kami_agent.tools.scaffold import NO_REFERENCE_FILES, ScaffoldTools

    assert NO_REFERENCE_FILES == "no reference files"
    for word in FORBIDDEN:
        assert word not in NO_REFERENCE_FILES.lower()
    # No reference/ tree in this run directory at all.
    empty = ScaffoldTools(tmp_path, profile="search")
    payload = json.loads(empty.execute("search_reference", {"query": "musu"}))
    assert payload == {"hits": [], "message": NO_REFERENCE_FILES}
