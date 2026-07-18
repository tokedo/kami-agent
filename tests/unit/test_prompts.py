"""The three frozen prompt strings (SPEC §6): exact content + leak discipline."""

from pathlib import Path

import pytest

PROMPTS = Path(__file__).parents[2] / "prompts"

# The frozen wording, reviewed and approved. Any change is deliberate:
# update this test in the same commit that re-freezes the string.
SYSTEM = """\
You are an autonomous agent in Kamigotchi, a persistent on-chain world shared with other players. You act in periodic sessions; the world advances between them. You act only through tool calls. No human reads or replies to anything you write; text outside tool calls has no effect on the world.

Your objective is to complete as many quests as possible.

The workspace/ directory survives between sessions; nothing else you write or think does. Its use and structure are entirely up to you.

The reference/ directory holds the game's design document. It is read-only.

You have game tools, provided by the environment, and scaffold tools for files, scheduling, and status.

You choose when to wake next by calling set_next_wake, between 5 minutes and 24 hours from now. You cannot wait or pause within a session. To wait for something, choose your next wake with set_next_wake and end the session with end_session.

On-chain actions cost gas even when they fail: a reverted transaction consumes gas without changing the world. Diagnose why an action failed before submitting it again.
"""

KICKOFF = "Session start.\n"

CONTINUE = "Continue. To end this session, call end_session.\n"


def test_frozen_strings_are_exactly_as_reviewed():
    assert (PROMPTS / "system.txt").read_text(encoding="utf-8") == SYSTEM
    assert (PROMPTS / "kickoff.txt").read_text(encoding="utf-8") == KICKOFF
    assert (PROMPTS / "continue.txt").read_text(encoding="utf-8") == CONTINUE


@pytest.mark.parametrize("name", ["system.txt", "kickoff.txt", "continue.txt"])
def test_no_apparatus_or_policy_leaks(name):
    # D12/D13: no budget, cost, tokens, compute limits, run duration,
    # session caps, forced truncation, or study existence. Hard rule 2:
    # no strategy hints, no vendor idioms, no XML-tag formatting.
    # Gas is a world mechanic, not apparatus (SPEC §9: in-game resources
    # are outside budget_usd): the transaction-cost item's "cost gas" is
    # the one allowed use of "cost"; any other occurrence still fails.
    text = (PROMPTS / name).read_text(encoding="utf-8").lower().replace("cost gas", "")
    forbidden = [
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
    for word in forbidden:
        assert word not in text, f"{name} contains {word!r}"


def test_packaged_prompts_match_repo_prompts():
    # The wheel ships the frozen strings as package data (kami_agent/prompts)
    # so `init` works under any install; the repo-root prompts/ tree is the
    # brief's canonical layout. The two copies must stay byte-identical.
    from importlib import resources

    packaged = resources.files("kami_agent") / "prompts"
    for name in ("system.txt", "kickoff.txt", "continue.txt"):
        assert (packaged / name).read_text(encoding="utf-8") == (PROMPTS / name).read_text(
            encoding="utf-8"
        ), f"packaged {name} diverges from prompts/{name}"


def test_wake_bounds_in_frozen_prompt_match_code_defaults():
    # The frozen prompt hardcodes the §9 default wake bounds. A manifest or
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
    # Frozen constants: no numbers, no timestamps (SPEC §3 step 6).
    for name in ("kickoff.txt", "continue.txt"):
        text = (PROMPTS / name).read_text(encoding="utf-8")
        assert not any(ch.isdigit() for ch in text), f"{name} contains digits"
