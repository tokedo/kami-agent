"""Tri-provider live smoke (README, Verification tier 3): one canned session per adapter.

Per provider, against its cheapest tier with tiny caps:
read status → list files → read a reference/ slice → call one read-only
harness tool → write a workspace file → set next wake → end session.

Asserts: all tool calls parsed natively, usage accounting non-zero,
telemetry validates against the P9 schema, and the apparatus leak check (I1) — no
apparatus vocabulary in any agent-visible string.

Also reports the observed per-call fixed context floor (system prompt +
file index + full tool schemas + the session-start brief), which SPEC
D1's cap-arithmetic assumption needs for the manifests.

The brief (SPEC P1.12) is part of call-1 context, so it is part of the
floor. Against the recorded surface its result comes from a committed
fixture — synthetic roster, real envelope shape — because the floor has
to be reproducible run to run, and a live party report is neither. The
fixture names its own roster size and per-kami byte cost, and the report
below quotes both: the brief's contribution scales with roster size and
nothing else, so a floor quoted without them is not interpretable.
"""

import json
import os
from pathlib import Path

import pytest

from kami_agent.adapters.anthropic import AnthropicAdapter
from kami_agent.adapters.base import ToolDef
from kami_agent.adapters.google import GoogleAdapter
from kami_agent.adapters.openai import OpenAIAdapter
from kami_agent.governor import PriceTable
from kami_agent.harness import HarnessClient, tools_hash
from kami_agent.loop import BRIEF_TOOL, GameToolResult, LoopCaps
from kami_agent.runner import SESSION_RAN, RunConfig, run_session
from kami_agent.telemetry import read_events, validate_event
from kami_agent.tools.errors import ToolError
from kami_agent.tools.scaffold import SCAFFOLD_TOOL_DEFS

REPO_ROOT = Path(__file__).parents[2]
FIXTURE = Path(__file__).parent / "fixtures" / "harness_tools.json"
BRIEF_FIXTURE = Path(__file__).parent / "fixtures" / "party_brief.json"

# The read-only game tool the canned session calls, and with it the
# measurement convention the reported context floor is quoted on.
#
# It was get_nodes through the v1.x pins. The v2 surface removes get_nodes
# (its reads moved behind the lens wrappers), so the convention re-bases on
# list_accounts — keyless, local-roster-served, and already what the
# live-harness workflow uses, so both tiers now quote one convention.
# Floors either side of this change are NOT directly comparable; the
# convention delta is measured, not assumed (README, Verification).
HARNESS_TOOL = os.environ.get("KAMI_SMOKE_HARNESS_TOOL", "list_accounts")

# Apparatus vocabulary that must never reach the agent (I1). Deliberately
# apparatus-specific: in-game economics legitimately mention costs, prices,
# tokens, and spending (MUSU/skill points), so generic money words stay out.
APPARATUS_FORBIDDEN = [
    "budget",
    "_usd",  # cost_usd / cumulative_usd / …; bare "usd" false-positives on base62 ids
    "horizon",
    "t_max",
    "session_token_cap",
    "session_tool_cap",
    "max_consecutive_errors",
    "wake_default",
    "study",
    "experiment",
]

PROVIDERS = {
    "anthropic": {
        "key_env": "ANTHROPIC_API_KEY",
        "model_env": "SMOKE_ANTHROPIC_MODEL",
        "default_model": "claude-haiku-4-5",
        # Cache rates: published list prices at pin time (5m write = 1.25 x
        # input, read = 0.1 x input).
        "prices": PriceTable(
            input_usd_per_mtok=1.0,
            output_usd_per_mtok=5.0,
            cache_read_usd_per_mtok=0.10,
            cache_write_usd_per_mtok=1.25,
        ),
        "adapter": lambda model: AnthropicAdapter(model),
    },
    "openai": {
        "key_env": "OPENAI_API_KEY",
        "model_env": "SMOKE_OPENAI_MODEL",
        "default_model": "gpt-4o-mini",
        # Published cached-input rate; automatic caching has no write
        # premium (write rate = input rate; cache_write_tokens is 0).
        "prices": PriceTable(
            input_usd_per_mtok=0.15,
            output_usd_per_mtok=0.60,
            cache_read_usd_per_mtok=0.075,
            cache_write_usd_per_mtok=0.15,
        ),
        "adapter": lambda model: OpenAIAdapter(model),
    },
    "google": {
        "key_env": "GEMINI_API_KEY",
        "model_env": "SMOKE_GEMINI_MODEL",
        "default_model": "gemini-2.5-flash-lite",
        # Published implicit-caching cached-input rate; no write premium.
        "prices": PriceTable(
            input_usd_per_mtok=0.10,
            output_usd_per_mtok=0.40,
            cache_read_usd_per_mtok=0.01,
            cache_write_usd_per_mtok=0.10,
        ),
        "adapter": lambda model: GoogleAdapter(model, api_key=os.environ.get("GEMINI_API_KEY")),
    },
}

# The harness account label the canned session queries. "main" suits the
# recorded fake; a real-harness run sets KAMI_SMOKE_ACCOUNT to a label from
# the local roster.
SMOKE_ACCOUNT = os.environ.get("KAMI_SMOKE_ACCOUNT", "main")

_STEP_4 = (
    f"Call {HARNESS_TOOL}."
    if HARNESS_TOOL == "list_accounts"
    else f'Call {HARNESS_TOOL} with account "{SMOKE_ACCOUNT}".'
)

# Test-only kickoff (the frozen production one is "Session start."). The
# first line is load-bearing: the session-start brief puts a completed
# tool call and its result in context ahead of the model's first turn, and
# without being told otherwise the cheapest tiers read that as the script
# having already begun — observed as skipped steps on gpt-4o-mini and as a
# jump straight to end_session on gemini-2.5-flash. It states a fact about
# the transcript, not a strategy.
KICKOFF = f"""\
A tool call and its result may already appear above. It is not one of the
steps below; none of the steps below have been done yet.

Complete the following steps in order, one tool call each, then stop.
1. Call get_status.
2. Call workspace_list.
3. Call workspace_read with path "reference/guide.md", offset 0, length 120.
4. {_STEP_4}
5. Call workspace_write with path "workspace/smoke.md" and content "smoke ok".
6. Call set_next_wake with minutes_from_now 30.
7. Call end_session with a short reason.
Do not call any other tools.
"""

# Roster size for the session-start brief against the recorded surface.
# Unset: the fixture's own roster. Set: the same roster resized, which is
# how the floor's sensitivity to roster size — the only thing the brief's
# context cost depends on — is re-measured without editing the fixture.
BRIEF_KAMIS = int(os.environ.get("KAMI_SMOKE_BRIEF_KAMIS", "0"))


def _brief_fixture(kami_count=0):
    """The brief fixture, optionally resized to ``kami_count`` kamis.

    Resizing cycles the recorded roster and renumbers the copies, so the
    envelope stays schema-shaped and per-kami byte cost stays realistic at
    any size.
    """
    brief = json.loads(BRIEF_FIXTURE.read_text(encoding="utf-8"))
    roster = brief["envelope"]["data"]["kamis"]
    if kami_count and kami_count != len(roster):
        resized = []
        for i in range(kami_count):
            kami = dict(roster[i % len(roster)])
            kami["index"] = 1041 + i * 13
            kami["name"] = f"kami-{kami['index']}"
            resized.append(kami)
        brief["envelope"]["data"]["kamis"] = resized
        brief["kami_count"] = kami_count
        brief["envelope_chars"] = len(json.dumps(brief["envelope"]))
    return brief


EXPECTED_SEQUENCE = [
    "get_status",
    "workspace_list",
    "workspace_read",
    HARNESS_TOOL,
    "workspace_write",
    "set_next_wake",
    "end_session",
]


class RecordedFakeHarness:
    """Serves the recorded real tool surface; execution is simulated."""

    def __init__(self):
        surface = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.tool_defs = [
            ToolDef(
                name=t["name"],
                description=t["description"],
                input_schema=t["input_schema"],
            )
            for t in surface["tools"]
        ]
        self.recorded_hash = surface["tools_hash"]
        self.brief = _brief_fixture(BRIEF_KAMIS)

    def execute(self, name, args):
        # The party report is what the session-start brief calls; it has to
        # answer with a real-shaped envelope or the floor it contributes to
        # is a measurement of an error string.
        if name == BRIEF_TOOL:
            return GameToolResult(content=json.dumps(self.brief["envelope"]))
        if not name.startswith(("get_", "list_")):
            raise ToolError(f"{name} is not available")
        return GameToolResult(content=json.dumps({"ok": True, "simulated": True, "tool": name}))

    def close(self):
        pass


class ReadOnlyHarness:
    """Real harness client with a read-only execution allowlist.

    The model sees the full tool surface (so the measured context floor is
    real), but only reads execute — a stray write intent gets an error
    result instead of a transaction. The brief's party report is a read,
    so it goes through to the real harness: what it answers with (a live
    envelope where a lens daemon is running, its own unavailability error
    where none is) is exactly what this tier exists to observe, and the
    floor it reports moves with it.
    """

    def __init__(self, client):
        self._client = client
        self.tool_defs = client.tool_defs

    def execute(self, name, args):
        if name != BRIEF_TOOL and not name.startswith(("get_", "list_")):
            raise ToolError(f"{name} is not available")
        return self._client.execute(name, args)

    def close(self):
        self._client.close()


def make_harness():
    mode = os.environ.get("KAMI_SMOKE_HARNESS", "fake")
    if mode == "real":
        harness_dir = os.environ.get("KAMI_HARNESS_DIR", str(Path.home() / "kami-harness"))
        python = os.environ.get(
            "KAMI_HARNESS_PYTHON", str(Path(harness_dir) / ".venv-smoke" / "bin" / "python")
        )
        if not Path(harness_dir).exists():
            pytest.skip(f"KAMI_SMOKE_HARNESS=real but {harness_dir} does not exist")
        # Pass the environment through (the MCP SDK's default child env
        # drops it): the harness refuses to start without MAINNET_RPC_URL.
        # The canned session is read-only and never dials mainnet, so the
        # offline placeholder suffices when no real endpoint is configured.
        env = {**os.environ}
        env.setdefault("MAINNET_RPC_URL", "http://127.0.0.1:9/offline-test")
        return ReadOnlyHarness(
            HarnessClient(
                python, ["executor/server.py"], cwd=harness_dir, env=env, handshake_timeout_s=90
            )
        )
    if not FIXTURE.exists():
        pytest.skip("recorded harness tool surface fixture missing")
    return RecordedFakeHarness()


@pytest.fixture
def run_dir(tmp_path):
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    for name in ("system.txt", "continue.txt"):
        (prompts / name).write_text(
            (REPO_ROOT / "prompts" / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    (prompts / "kickoff.txt").write_text(KICKOFF, encoding="utf-8")
    reference = tmp_path / "reference"
    reference.mkdir()
    (reference / "guide.md").write_text(
        "# Field guide\n\n" + "The world persists between sessions. " * 20, encoding="utf-8"
    )
    return tmp_path


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
def test_canned_session(provider, run_dir):
    spec = PROVIDERS[provider]
    if not os.environ.get(spec["key_env"]):
        pytest.skip(f"{spec['key_env']} not set")
    model = os.environ.get(spec["model_env"], spec["default_model"])
    adapter = spec["adapter"](model)
    harness = make_harness()
    try:
        config = RunConfig(
            run_dir=run_dir,
            run_id=f"smoke-{provider}",
            model=model,
            prices=spec["prices"],
            # retry_max_attempts=8: backoff must outlast a 60 s free-tier quota
            # window (observed: gemini 429s for >31 s, the default-5 span).
            caps=LoopCaps(session_token_cap=150_000, session_tool_cap=12, retry_max_attempts=8),
            budget_usd=5.0,
        )
        outcome = run_session(config, adapter, harness_factory=lambda: harness)
    finally:
        harness.close()

    events = list(read_events(run_dir / "telemetry.jsonl"))
    try:
        _assert_canned_session(provider, model, run_dir, harness, outcome, events)
    except AssertionError:
        _dump_diagnostics(provider, run_dir, events)
        raise


def _dump_diagnostics(provider, run_dir, events):
    """On failure, put what the model actually did into the CI log."""
    print(f"\n--- SMOKE DIAGNOSTICS [{provider}] ---")
    for event in events:
        kind = event["event"]
        if kind == "llm_call":
            print(
                f"llm_call stop={event['stop_reason']} in={event['input_tokens']} "
                f"cache_read={event.get('cache_read_tokens', 0)} "
                f"cache_write={event.get('cache_write_tokens', 0)} "
                f"out={event['output_tokens']} retry={event['retry_count']} "
                f"usage_unknown={event.get('usage_unknown', False)}"
            )
        elif kind == "tool_call":
            print(
                f"tool_call {event['tool']} ok={event['ok']} "
                f"skipped={event.get('skipped', False)} err={event.get('error', '')[:120]}"
            )
        elif kind == "session_end":
            print(f"session_end reason={event['reason']}")
    transcript = run_dir / "transcripts" / "session-0001.jsonl"
    if transcript.exists():
        for line in transcript.read_text(encoding="utf-8").splitlines():
            message = json.loads(line)
            if message["role"] == "assistant":
                calls = [c["name"] for c in message["tool_calls"]]
                print(f"assistant calls={calls} text={(message['text'] or '')[:100]!r}")


def _assert_canned_session(provider, model, run_dir, harness, outcome, events):
    assert outcome == SESSION_RAN

    # Tier gate: telemetry events validate against the P9 schema.
    for event in events:
        validate_event(event)

    tool_events = [e for e in events if e["event"] == "tool_call"]
    model_events = [e for e in tool_events if e["initiator"] == "model"]

    # Tier gate: the session-start brief (SPEC P1.12) ran exactly once, as a
    # harness call, before the first model call — and every provider carried
    # the synthesized call/result pair natively, which is the part that can
    # only be proven against real APIs.
    #
    # Asserted BEFORE the canned sequence deliberately: the sequence depends
    # on the model following seven ordered instructions, which the cheapest
    # tiers occasionally do not, and a flake there must not decide whether
    # the brief gate ran.
    briefs = [e for e in tool_events if e["initiator"] == "scaffold"]
    assert len(briefs) == 1, f"{provider}: expected one scaffold-initiated call, got {len(briefs)}"
    assert briefs[0]["tool"] == BRIEF_TOOL
    assert briefs[0]["source"] == "harness"
    assert tool_events[0] is briefs[0], f"{provider}: the brief was not the first tool call"
    kinds = [e["event"] for e in events]
    assert kinds.index("tool_call") < kinds.index("llm_call"), (
        f"{provider}: the brief must precede the first model call, got {kinds[:4]}"
    )
    # And it reached the model verbatim, envelope untouched.
    transcript = [
        json.loads(line)
        for line in (run_dir / "transcripts" / "session-0001.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert transcript[1]["role"] == "assistant"
    assert [c["name"] for c in transcript[1]["tool_calls"]] == [BRIEF_TOOL]
    assert transcript[2]["role"] == "tool_result"
    brief_content = transcript[2]["content"]
    if isinstance(harness, RecordedFakeHarness):
        assert brief_content == json.dumps(harness.brief["envelope"])

    # Tier gate: all tool calls parsed natively → each canned step executed ok.
    executed = [e["tool"] for e in model_events if e["ok"]]
    for step in EXPECTED_SEQUENCE:
        assert step in executed, f"{provider}: step {step!r} missing from {executed}"
    harness_events = [e for e in model_events if e["tool"] == HARNESS_TOOL]
    assert harness_events[0]["source"] == "harness"

    # Tier gate: usage accounting non-zero. Transient provider errors emit
    # usage_unknown attempts at cost 0 (P7.4) before the retry succeeds —
    # measure the first *successful* call.
    llm_events = [e for e in events if e["event"] == "llm_call"]
    ok_llm = [e for e in llm_events if not e.get("usage_unknown")]
    assert ok_llm, f"{provider}: no successful llm_call events"
    assert ok_llm[0]["input_tokens"] > 0
    assert ok_llm[0]["output_tokens"] > 0
    session_end = next(e for e in events if e["event"] == "session_end")
    assert session_end["reason"] == "agent"
    assert session_end["session_cost_usd"] > 0

    # The agent's scheduling call took effect.
    schedule = next(e for e in events if e["event"] == "schedule_next")
    assert schedule["source"] == "agent"
    assert schedule["clamped_min"] == 30.0

    # The workspace write landed.
    assert (run_dir / "workspace" / "smoke.md").read_text(encoding="utf-8") == "smoke ok"

    # Apparatus leak check (I1) over every agent-visible string: system prompt +
    # file index, kickoff/continuation, tool names/descriptions/schemas,
    # and the full transcript (assistant + tool results as sent).
    visible = [
        (run_dir / "prompts" / "system.txt").read_text(encoding="utf-8"),
        KICKOFF,
        (run_dir / "prompts" / "continue.txt").read_text(encoding="utf-8"),
        (run_dir / "transcripts" / "session-0001.jsonl").read_text(encoding="utf-8"),
        json.dumps(
            [
                {"name": t.name, "description": t.description, "schema": t.input_schema}
                for t in list(harness.tool_defs) + list(SCAFFOLD_TOOL_DEFS)
            ]
        ),
    ]
    for text in visible:
        lowered = text.lower()
        for word in APPARATUS_FORBIDDEN:
            assert word not in lowered, f"{provider}: apparatus leak: {word!r}"

    # Report (SPEC D1 cap arithmetic wants the observed floor;
    # the cache columns show how much of it was served from/written to
    # provider cache, per P7.1).
    session_cache_read = sum(e.get("cache_read_tokens", 0) for e in ok_llm)
    session_cache_write = sum(e.get("cache_write_tokens", 0) for e in ok_llm)
    # The floor is only interpretable alongside what produced it: the exact
    # surface the model was shown and the exact size of the fixed prefix.
    # Quoting a floor without them is how two tiers end up disagreeing with
    # no way to tell whether the surface or the prompt moved.
    system_chars = len(
        (run_dir / "prompts" / "system.txt").read_text(encoding="utf-8").rstrip("\n")
    ) + len(_file_index(run_dir))
    # The brief is part of call 1, so it is part of the floor. Its size is a
    # function of roster size and nothing else — quote both, so a floor
    # measured at one roster converts to any other.
    kami_count = harness.brief["kami_count"] if isinstance(harness, RecordedFakeHarness) else "live"
    print(
        f"\nSMOKE[{provider}] model={model} "
        f"fixed_floor_input_tokens={ok_llm[0]['input_tokens']} "
        f"surface_hash={tools_hash(list(harness.tool_defs))} "
        f"brief_tool={BRIEF_TOOL} brief_ok={briefs[0]['ok']} "
        f"brief_chars={len(brief_content)} brief_kamis={kami_count} "
        f"system_chars={system_chars} kickoff_chars={len(KICKOFF)} "
        f"llm_calls={session_end['llm_calls']} tool_calls={session_end['tool_calls']} "
        f"session_tokens={session_end['session_tokens']} "
        f"session_cache_read={session_cache_read} "
        f"session_cache_write={session_cache_write} "
        f"cost_usd={session_end['session_cost_usd']:.6f} "
        f"tools={len(list(harness.tool_defs))} "
        f"executed={executed}"
    )


def _file_index(run_dir):
    """The file-index half of the system prompt, as the runner builds it."""
    from kami_agent.tools.scaffold import ScaffoldTools

    return ScaffoldTools(run_dir, session_number=1).workspace_list()


def test_recorded_surface_matches_hash():
    """The committed fixture is internally consistent (guards pin bumps)."""
    if not FIXTURE.exists():
        pytest.skip("recorded harness tool surface fixture missing")
    surface = json.loads(FIXTURE.read_text(encoding="utf-8"))
    defs = [
        ToolDef(name=t["name"], description=t["description"], input_schema=t["input_schema"])
        for t in surface["tools"]
    ]
    assert tools_hash(defs) == surface["tools_hash"]


def test_brief_fixture_is_internally_consistent():
    """The sizes the fixture quotes are the sizes it has.

    Those two numbers are how a floor measured at this roster converts to
    any other, so a fixture that misquotes itself would silently mis-size
    every manifest derived from the reported floor.
    """
    if not BRIEF_FIXTURE.exists():
        pytest.skip("brief fixture missing")
    brief = _brief_fixture()
    kamis = brief["envelope"]["data"]["kamis"]
    envelope = brief["envelope"]
    empty = {**envelope, "data": {**envelope["data"], "kamis": []}}
    assert brief["kami_count"] == len(kamis)
    assert brief["envelope_chars"] == len(json.dumps(envelope))
    assert brief["kami_chars_each"] == round(
        (len(json.dumps(envelope)) - len(json.dumps(empty))) / len(kamis)
    )
    # Resizing keeps the envelope schema-shaped and the per-kami cost linear.
    doubled = _brief_fixture(2 * len(kamis))
    assert len(doubled["envelope"]["data"]["kamis"]) == 2 * len(kamis)
    assert doubled["envelope_chars"] > brief["envelope_chars"]
    # It must be the shape the brief actually asks for, and carry the
    # coverage the brief exists to deliver.
    assert brief["tool"] == BRIEF_TOOL
    assert set(brief["envelope"]) == {"data", "untrusted", "meta"}
    for kami in kamis:
        assert {"state", "hp", "hpRatePerHr", "cooldownSec"} <= set(kami)


def test_recorded_surface_matches_the_live_harness():
    """The fixture must be what a live harness at the pinned SHA actually serves.

    Internal consistency (above) only proves the fixture was not edited by
    hand. This proves it is not stale: the recorded-surface tier reports
    the context floor every manifest is sized against, and a fixture that
    has drifted from the live surface makes that floor quietly wrong.
    Only meaningful against a real child, so it skips elsewhere.
    """
    if os.environ.get("KAMI_SMOKE_HARNESS") != "real":
        pytest.skip("live-harness tier only (KAMI_SMOKE_HARNESS=real)")
    if not FIXTURE.exists():
        pytest.skip("recorded harness tool surface fixture missing")
    recorded = json.loads(FIXTURE.read_text(encoding="utf-8"))
    # The surface is sensitive to the harness's Python minor version:
    # descriptions come from docstrings, and CPython 3.13 strips their
    # leading indentation at compile time where 3.12 keeps it. Comparing
    # across versions would report drift that is really a mismatched
    # interpreter, so the fixture records what it was captured under.
    assert recorded["harness"]["recorded_under_python"] == "3.13", (
        "fixture recorded under an unexpected Python; the packaged image is "
        "python:3.13-slim and the surface must be captured to match it"
    )
    harness = make_harness()
    try:
        live = list(harness.tool_defs)
    finally:
        harness.close()
    live_names = [t.name for t in live]
    recorded_names = [t["name"] for t in recorded["tools"]]
    assert live_names == recorded_names, (
        f"added={sorted(set(live_names) - set(recorded_names))} "
        f"removed={sorted(set(recorded_names) - set(live_names))}"
    )
    by_name = {t["name"]: t for t in recorded["tools"]}
    drifted = [
        t.name
        for t in live
        if t.description != by_name[t.name]["description"]
        or t.input_schema != by_name[t.name]["input_schema"]
    ]
    assert not drifted, f"description/schema drift in: {drifted}"
    assert tools_hash(live) == recorded["tools_hash"]
