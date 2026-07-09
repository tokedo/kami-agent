"""D22 (SPEC §5.1 v1.2): opaque provider reasoning state, same-session round-trip."""

import json
from pathlib import Path

from anthropic.types import Message as AnthropicMessage
from google.genai import types as genai_types

from kami_agent.adapters.anthropic import AnthropicAdapter
from kami_agent.adapters.base import (
    AdapterResponse,
    AssistantMessage,
    ProviderState,
    SamplingParams,
    StopReason,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
)
from kami_agent.adapters.google import GoogleAdapter
from kami_agent.adapters.openai import OpenAIAdapter
from kami_agent.runner import _message_dict

FIXTURES = Path(__file__).parent / "fixtures"
PARAMS = SamplingParams(max_tokens=4096)


class Recorder:
    """Fake client core: records kwargs, returns a canned result."""

    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def anthropic_adapter(result):
    recorder = Recorder(result)
    client = type("C", (), {"messages": type("M", (), {"create": staticmethod(recorder)})()})()
    return AnthropicAdapter("claude-test", client=client), recorder


def google_adapter(result):
    recorder = Recorder(result)
    client = type(
        "C", (), {"models": type("M", (), {"generate_content": staticmethod(recorder)})()}
    )()
    return GoogleAdapter("gemini-test", client=client), recorder


def load(provider, name):
    data = json.loads((FIXTURES / provider / f"{name}.json").read_text(encoding="utf-8"))
    if provider == "anthropic":
        return AnthropicMessage.model_validate(data)
    return genai_types.GenerateContentResponse.model_validate(data)


# --- capture -------------------------------------------------------------------


def test_anthropic_captures_signed_thinking_blocks():
    adapter, _ = anthropic_adapter(load("anthropic", "thinking_tool_use"))
    response = adapter.complete("s", [UserMessage(text="hi")], [], PARAMS)
    state = response.provider_state
    assert state is not None
    assert state.provider == "anthropic"
    assert [b.type for b in state.payload] == ["thinking", "text", "tool_use"]
    assert state.payload[0].signature == "EvcRsignedThinkingSignatureBase64=="
    # Canonical fields are unaffected by the opaque state.
    assert response.text_blocks == ("Listing the workspace.",)
    assert response.tool_calls[0].name == "workspace_list"


def test_anthropic_no_thinking_leaves_state_unset():
    adapter, _ = anthropic_adapter(load("anthropic", "parallel_tool_use"))
    assert adapter.complete("s", [UserMessage(text="hi")], [], PARAMS).provider_state is None


def test_google_captures_thought_signatures():
    adapter, _ = google_adapter(load("google", "thought_signature"))
    response = adapter.complete("s", [UserMessage(text="hi")], [], PARAMS)
    state = response.provider_state
    assert state is not None
    assert state.provider == "google"
    assert state.payload[1].thought_signature  # bytes, decoded from base64
    # The D16 fold still applies alongside the state capture.
    assert response.usage.output_tokens == 270  # 90 candidates + 180 thoughts
    assert response.usage.reasoning_tokens == 180


def test_google_no_signatures_leaves_state_unset():
    adapter, _ = google_adapter(load("google", "parallel_function_calls"))
    assert adapter.complete("s", [UserMessage(text="hi")], [], PARAMS).provider_state is None


def test_openai_never_sets_state():
    from openai.types.chat import ChatCompletion

    data = json.loads((FIXTURES / "openai" / "reasoning_usage.json").read_text())
    recorder = Recorder(ChatCompletion.model_validate(data))
    client = type(
        "C",
        (),
        {
            "chat": type(
                "Ch", (), {"completions": type("Co", (), {"create": staticmethod(recorder)})()}
            )()
        },
    )()
    adapter = OpenAIAdapter("gpt-test", client=client)
    assert adapter.complete("s", [UserMessage(text="hi")], [], PARAMS).provider_state is None


# --- replay --------------------------------------------------------------------


def anthropic_conversation(state):
    return [
        UserMessage(text="Session start."),
        AssistantMessage(
            text="Listing the workspace.",
            tool_calls=(ToolCall(id="toolu_01CCC333", name="workspace_list", args={}),),
            provider_state=state,
        ),
        ToolResultMessage(tool_call_id="toolu_01CCC333", content="workspace/ (empty)"),
    ]


def test_anthropic_replays_its_own_state_verbatim():
    first = load("anthropic", "thinking_tool_use")
    adapter, recorder = anthropic_adapter(first)
    state = adapter.complete("s", [UserMessage(text="hi")], [], PARAMS).provider_state

    adapter2, recorder2 = anthropic_adapter(load("anthropic", "text_end_turn"))
    adapter2.complete("s", anthropic_conversation(state), [], PARAMS)
    assistant_turn = recorder2.calls[0]["messages"][1]
    assert assistant_turn["content"] == list(first.content)  # blocks, verbatim
    assert assistant_turn["content"][0].signature == "EvcRsignedThinkingSignatureBase64=="


def test_anthropic_ignores_foreign_state():
    foreign = ProviderState(provider="google", payload=("something",))
    adapter, recorder = anthropic_adapter(load("anthropic", "text_end_turn"))
    adapter.complete("s", anthropic_conversation(foreign), [], PARAMS)
    assistant_turn = recorder.calls[0]["messages"][1]
    assert assistant_turn["content"] == [
        {"type": "text", "text": "Listing the workspace."},
        {"type": "tool_use", "id": "toolu_01CCC333", "name": "workspace_list", "input": {}},
    ]


def test_google_replays_signatures_and_keeps_response_matching():
    first = load("google", "thought_signature")
    adapter, _ = google_adapter(first)
    state = adapter.complete("s", [UserMessage(text="hi")], [], PARAMS).provider_state

    adapter2, recorder2 = google_adapter(load("google", "text_stop"))
    conversation = [
        UserMessage(text="Session start."),
        AssistantMessage(
            text="Listing the workspace.",
            tool_calls=(ToolCall(id="call_1", name="workspace_list", args={}),),
            provider_state=state,
        ),
        ToolResultMessage(tool_call_id="call_1", content="workspace/ (empty)"),
    ]
    adapter2.complete("s", conversation, [], PARAMS)
    contents = recorder2.calls[0]["contents"]
    model_turn = contents[1]
    assert model_turn.role == "model"
    assert model_turn.parts == list(first.candidates[0].content.parts)
    assert model_turn.parts[1].thought_signature
    # id→name resolution for the grouped function response still works.
    assert contents[2].parts[0].function_response.name == "workspace_list"


def test_google_ignores_foreign_state():
    foreign = ProviderState(provider="anthropic", payload=("blob",))
    adapter, recorder = google_adapter(load("google", "text_stop"))
    adapter.complete(
        "s",
        [
            UserMessage(text="hi"),
            AssistantMessage(text="ok", provider_state=foreign),
        ],
        [],
        PARAMS,
    )
    model_turn = recorder.calls[0]["contents"][1]
    assert model_turn.parts[0].text == "ok"
    assert len(model_turn.parts) == 1


def test_openai_ignores_any_state():
    from kami_agent.adapters.openai import _to_wire_messages

    state = ProviderState(provider="anthropic", payload=("blob",))
    wire = _to_wire_messages("s", [AssistantMessage(text="ok", provider_state=state)])
    assert wire[1] == {"role": "assistant", "content": "ok"}


# --- loop + transcript ------------------------------------------------------------


def test_loop_copies_state_verbatim_without_inspecting(tmp_path):
    from kami_agent.governor import PriceTable
    from kami_agent.loop import AgentLoop, LoopCaps
    from kami_agent.telemetry import TelemetryWriter, read_events

    state = ProviderState(provider="anthropic", payload=(object(),))  # truly opaque

    class Scripted:
        def __init__(self):
            self.requests = []

        def complete(self, system, messages, tools, params):
            self.requests.append(list(messages))
            if len(self.requests) == 1:
                return AdapterResponse(
                    text_blocks=(),
                    tool_calls=(ToolCall(id="s1", name="get_status", args={}),),
                    stop_reason=StopReason.TOOL_USE,
                    usage=Usage(input_tokens=10, output_tokens=5),
                    provider_state=state,
                )
            return AdapterResponse(
                text_blocks=(),
                tool_calls=(ToolCall(id="e1", name="end_session", args={"reason": "x"}),),
                stop_reason=StopReason.TOOL_USE,
                usage=Usage(input_tokens=10, output_tokens=5),
            )

    from kami_agent.tools.scaffold import ScaffoldTools

    adapter = Scripted()
    loop = AgentLoop(
        adapter=adapter,
        model="m",
        system="s",
        kickoff_text="go",
        continuation_text="continue",
        scaffold=ScaffoldTools(tmp_path),
        game=None,
        telemetry=TelemetryWriter(tmp_path / "t.jsonl", run_id="r"),
        session=1,
        params=PARAMS,
        prices=PriceTable(1.0, 5.0),
        caps=LoopCaps(session_token_cap=100_000),
        sleep=lambda s: None,
    )
    loop.run()
    replayed = adapter.requests[1][1]
    assert isinstance(replayed, AssistantMessage)
    assert replayed.provider_state is state  # the same object, uninspected
    # Telemetry never carries provider state (D22).
    for event in read_events(tmp_path / "t.jsonl"):
        assert "provider_state" not in json.dumps(event)


def test_transcript_records_state_as_sent():
    payload = (genai_types.Part(text="thought", thought_signature=b"sig"),)
    message = AssistantMessage(
        text="ok",
        provider_state=ProviderState(provider="google", payload=payload),
    )
    entry = _message_dict(message)
    assert entry["provider_state"]["provider"] == "google"
    dumped = json.dumps(entry)  # transcript lines must be JSON-serializable
    assert "thought" in dumped


def test_provider_state_defaults_to_none():
    assert AssistantMessage(text="x").provider_state is None
    assert (
        AdapterResponse(
            text_blocks=(),
            tool_calls=(),
            stop_reason=StopReason.END_TURN,
            usage=Usage(input_tokens=1, output_tokens=1),
        ).provider_state
        is None
    )
