"""Tri-provider live smoke (SPEC §11.2): one canned session per adapter.

Per provider, against its cheapest tier with tiny caps:
read status → list files → read a reference/ slice → call one read-only
harness tool → write a workspace file → set next wake → end session.

Asserts: all tool calls parsed natively, usage accounting non-zero,
telemetry validates against the §8 schema, and the D12 leak check — no
apparatus vocabulary in any agent-visible string.

Also reports the observed per-call fixed context floor (system prompt +
file index + full tool schemas), which SPEC §9's fixed-floor arithmetic
needs for the manifests.
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
from kami_agent.loop import GameToolResult, LoopCaps
from kami_agent.runner import SESSION_RAN, RunConfig, run_session
from kami_agent.telemetry import read_events, validate_event
from kami_agent.tools.errors import ToolError
from kami_agent.tools.scaffold import SCAFFOLD_TOOL_DEFS

REPO_ROOT = Path(__file__).parents[2]
FIXTURE = Path(__file__).parent / "fixtures" / "harness_tools.json"

# The read-only game tool the canned session calls. get_nodes exercises the
# live Kamibots API (needs a registered account); list_accounts is keyless
# (used by the CI live-harness workflow, where no account keys exist).
HARNESS_TOOL = os.environ.get("KAMI_SMOKE_HARNESS_TOOL", "get_nodes")

# Apparatus vocabulary that must never reach the agent (D12). Deliberately
# apparatus-specific: in-game economics legitimately mention costs, prices,
# tokens, and spending (MUSU/skill points), so generic money words stay out.
D12_FORBIDDEN = [
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
        "prices": PriceTable(input_usd_per_mtok=1.0, output_usd_per_mtok=5.0),
        "adapter": lambda model: AnthropicAdapter(model),
    },
    "openai": {
        "key_env": "OPENAI_API_KEY",
        "model_env": "SMOKE_OPENAI_MODEL",
        "default_model": "gpt-4o-mini",
        "prices": PriceTable(input_usd_per_mtok=0.15, output_usd_per_mtok=0.60),
        "adapter": lambda model: OpenAIAdapter(model),
    },
    "google": {
        "key_env": "GEMINI_API_KEY",
        "model_env": "SMOKE_GEMINI_MODEL",
        "default_model": "gemini-2.5-flash-lite",
        "prices": PriceTable(input_usd_per_mtok=0.10, output_usd_per_mtok=0.40),
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

KICKOFF = f"""\
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

    def execute(self, name, args):
        if not name.startswith(("get_", "list_")):
            raise ToolError(f"{name} is not available")
        return GameToolResult(content=json.dumps({"ok": True, "simulated": True, "tool": name}))

    def close(self):
        pass


class ReadOnlyHarness:
    """Real harness client with a read-only execution allowlist.

    The model sees the full tool surface (so the measured context floor is
    real), but only get_*/list_* tools execute — a stray write intent gets
    an error result instead of a transaction.
    """

    def __init__(self, client):
        self._client = client
        self.tool_defs = client.tool_defs

    def execute(self, name, args):
        if not name.startswith(("get_", "list_")):
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
        return ReadOnlyHarness(
            HarnessClient(python, ["executor/server.py"], cwd=harness_dir, handshake_timeout_s=90)
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
            caps=LoopCaps(session_token_cap=150_000, session_tool_cap=12),
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

    # §11.2: telemetry events validate against the §8 schema.
    for event in events:
        validate_event(event)

    # §11.2: all tool calls parsed natively → each canned step executed ok.
    tool_events = [e for e in events if e["event"] == "tool_call"]
    executed = [e["tool"] for e in tool_events if e["ok"]]
    for step in EXPECTED_SEQUENCE:
        assert step in executed, f"{provider}: step {step!r} missing from {executed}"
    harness_events = [e for e in tool_events if e["tool"] == HARNESS_TOOL]
    assert harness_events[0]["source"] == "harness"

    # §11.2: usage accounting non-zero. Transient provider errors emit
    # usage_unknown attempts at cost 0 (§5.5) before the retry succeeds —
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

    # D12 leak check over every agent-visible string: system prompt +
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
        for word in D12_FORBIDDEN:
            assert word not in lowered, f"{provider}: D12 leak: {word!r}"

    # Report (SPEC §9 fixed-floor arithmetic wants the observed floor).
    print(
        f"\nSMOKE[{provider}] model={model} "
        f"fixed_floor_input_tokens={ok_llm[0]['input_tokens']} "
        f"llm_calls={session_end['llm_calls']} tool_calls={session_end['tool_calls']} "
        f"session_tokens={session_end['session_tokens']} "
        f"cost_usd={session_end['session_cost_usd']:.6f} "
        f"tools={len(list(harness.tool_defs))} "
        f"executed={executed}"
    )


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
