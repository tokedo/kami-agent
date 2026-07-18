"""Anthropic adapter: recorded-fixture normalization + retry classification (brief §3.3)."""

import json
from pathlib import Path

import anthropic
import httpx
import pytest
from anthropic.types import Message as ProviderMessage

from kami_agent.adapters.anthropic import (
    AnthropicAdapter,
    _classify_error,
    _normalize_stop_reason,
)
from kami_agent.adapters.base import (
    AdapterError,
    AssistantMessage,
    ModelAdapter,
    ProviderState,
    SamplingParams,
    StopReason,
    ToolCall,
    ToolDef,
    ToolResultMessage,
    UserMessage,
)

FIXTURES = Path(__file__).parent / "fixtures" / "anthropic"


def load_fixture(name: str) -> ProviderMessage:
    return ProviderMessage.model_validate(
        json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    )


class FakeMessages:
    def __init__(self, result):
        self._result = result
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class FakeClient:
    def __init__(self, result):
        self.messages = FakeMessages(result)


def make_adapter(result, model="claude-haiku-4-5"):
    client = FakeClient(result)
    return AnthropicAdapter(model, client=client), client


PARAMS = SamplingParams(max_tokens=4096)

TOOLS = [
    ToolDef(
        name="workspace_read",
        description="Read a file.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer"},
                "length": {"type": "integer"},
            },
            "required": ["path"],
        },
    )
]


def test_satisfies_model_adapter_protocol():
    adapter, _ = make_adapter(load_fixture("text_end_turn"))
    assert isinstance(adapter, ModelAdapter)


# --- message mapping -------------------------------------------------------


def test_request_shape_system_tools_params():
    adapter, client = make_adapter(load_fixture("text_end_turn"))
    adapter.complete("You are an agent.", [UserMessage(text="Session start.")], TOOLS, PARAMS)
    (request,) = client.messages.calls
    assert request["model"] == "claude-haiku-4-5"
    assert request["max_tokens"] == 4096
    # Breakpoint 1 (SPEC §5.2): the last system block carries cache_control;
    # the system text itself is byte-identical to the plain-string form.
    assert request["system"] == [
        {
            "type": "text",
            "text": "You are an agent.",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    # Tools are untouched (the system breakpoint already covers them —
    # render order is tools → system → messages).
    assert request["tools"] == [
        {
            "name": "workspace_read",
            "description": "Read a file.",
            "input_schema": TOOLS[0].input_schema,
        }
    ]
    # Rolling breakpoint: last content block of the final message.
    assert request["messages"] == [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "Session start.",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        }
    ]
    # Not requested → not sent: temperature and effort.
    assert "temperature" not in request
    assert "output_config" not in request


def test_optional_params_sent_when_set():
    adapter, client = make_adapter(load_fixture("text_end_turn"))
    params = SamplingParams(max_tokens=1024, temperature=0.5, reasoning_effort="low")
    adapter.complete("s", [UserMessage(text="hi")], [], params)
    (request,) = client.messages.calls
    assert request["temperature"] == 0.5
    assert request["output_config"] == {"effort": "low"}
    assert "tools" not in request


def test_conversation_mapping_groups_tool_results_into_one_user_message():
    adapter, client = make_adapter(load_fixture("text_end_turn"))
    conversation = [
        UserMessage(text="Session start."),
        AssistantMessage(
            text="Checking two things.",
            tool_calls=(
                ToolCall(id="toolu_01AAA111", name="workspace_list", args={}),
                ToolCall(id="toolu_01BBB222", name="workspace_read", args={"path": "a.md"}),
            ),
        ),
        ToolResultMessage(tool_call_id="toolu_01AAA111", content="workspace/ (empty)"),
        ToolResultMessage(tool_call_id="toolu_01BBB222", content="boom", is_error=True),
        AssistantMessage(text="Done."),
    ]
    adapter.complete("s", conversation, [], PARAMS)
    (request,) = client.messages.calls
    assert request["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "Session start."}]},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Checking two things."},
                {"type": "tool_use", "id": "toolu_01AAA111", "name": "workspace_list", "input": {}},
                {
                    "type": "tool_use",
                    "id": "toolu_01BBB222",
                    "name": "workspace_read",
                    "input": {"path": "a.md"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_01AAA111",
                    "content": "workspace/ (empty)",
                },
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_01BBB222",
                    "content": "boom",
                    "is_error": True,
                },
            ],
        },
        {
            "role": "assistant",
            "content": [
                # Rolling breakpoint: last block of the final message.
                {"type": "text", "text": "Done.", "cache_control": {"type": "ephemeral"}}
            ],
        },
    ]


def test_tool_results_after_user_text_start_a_new_message():
    adapter, client = make_adapter(load_fixture("text_end_turn"))
    conversation = [
        UserMessage(text="Continue."),
        ToolResultMessage(tool_call_id="toolu_01CCC333", content="ok"),
    ]
    adapter.complete("s", conversation, [], PARAMS)
    (request,) = client.messages.calls
    assert [m["role"] for m in request["messages"]] == ["user", "user"]
    assert request["messages"][1]["content"][0]["type"] == "tool_result"


def test_assistant_message_without_text_maps_to_tool_use_only():
    adapter, client = make_adapter(load_fixture("text_end_turn"))
    conversation = [
        UserMessage(text="go"),
        AssistantMessage(tool_calls=(ToolCall(id="t1", name="get_status", args={}),)),
        ToolResultMessage(tool_call_id="t1", content="{}"),
    ]
    adapter.complete("s", conversation, [], PARAMS)
    (request,) = client.messages.calls
    assert request["messages"][1]["content"] == [
        {"type": "tool_use", "id": "t1", "name": "get_status", "input": {}}
    ]


# --- response normalization ------------------------------------------------


def test_text_end_turn_normalization():
    adapter, _ = make_adapter(load_fixture("text_end_turn"))
    response = adapter.complete("s", [UserMessage(text="hi")], [], PARAMS)
    assert response.text_blocks == ("The workspace is empty.",)
    assert response.tool_calls == ()
    assert response.stop_reason is StopReason.END_TURN
    assert response.provider_meta["id"] == "msg_01Xk2fQqLxGgeExampleText"


def test_parallel_intent_extraction_preserves_order():
    adapter, _ = make_adapter(load_fixture("parallel_tool_use"))
    response = adapter.complete("s", [UserMessage(text="hi")], TOOLS, PARAMS)
    assert response.stop_reason is StopReason.TOOL_USE
    assert response.text_blocks == ("Reading the file index and the status.",)
    assert [call.name for call in response.tool_calls] == ["workspace_list", "workspace_read"]
    assert response.tool_calls[0] == ToolCall(id="toolu_01AAA111", name="workspace_list", args={})
    assert response.tool_calls[1].args == {
        "path": "reference/README.md",
        "offset": 0,
        "length": 1024,
    }


def test_max_tokens_and_refusal_normalization():
    adapter, _ = make_adapter(load_fixture("max_tokens"))
    assert (
        adapter.complete("s", [UserMessage(text="hi")], [], PARAMS).stop_reason
        is StopReason.MAX_TOKENS
    )
    adapter, _ = make_adapter(load_fixture("refusal"))
    response = adapter.complete("s", [UserMessage(text="hi")], [], PARAMS)
    assert response.stop_reason is StopReason.REFUSAL
    assert response.text_blocks == ()


def test_stop_reason_mapping_table():
    assert _normalize_stop_reason("end_turn") is StopReason.END_TURN
    assert _normalize_stop_reason("tool_use") is StopReason.TOOL_USE
    assert _normalize_stop_reason("max_tokens") is StopReason.MAX_TOKENS
    assert _normalize_stop_reason("refusal") is StopReason.REFUSAL
    # No stop sequences are ever set; if the provider reports one anyway it
    # means "generation ended" — end_turn.
    assert _normalize_stop_reason("stop_sequence") is StopReason.END_TURN


@pytest.mark.parametrize("value", ["pause_turn", "model_context_window_exceeded", None, "novel"])
def test_unmappable_stop_reason_raises(value):
    with pytest.raises(AdapterError) as excinfo:
        _normalize_stop_reason(value)
    assert not excinfo.value.retryable


# --- prompt caching breakpoints (SPEC §5.2) ---------------------------------


def _cache_markers(request):
    """Every cache_control marker in a request, as (location, block_index)."""
    markers = []
    system = request.get("system")
    if isinstance(system, list):
        for index, block in enumerate(system):
            if isinstance(block, dict) and "cache_control" in block:
                markers.append(("system", index))
    for index, tool in enumerate(request.get("tools") or []):
        if "cache_control" in tool:
            markers.append(("tools", index))
    for position, message in enumerate(request["messages"]):
        for index, block in enumerate(message["content"]):
            if isinstance(block, dict) and "cache_control" in block:
                markers.append((f"messages[{position}]", index))
    return markers


def _conversation_after_turn(intents: int):
    """kickoff → assistant turn with N intents → N grouped tool results."""
    calls = tuple(
        ToolCall(id=f"toolu_{i:03d}", name="workspace_read", args={"path": f"f{i}.md"})
        for i in range(intents)
    )
    return [
        UserMessage(text="Session start."),
        AssistantMessage(text="Reading.", tool_calls=calls),
        *(ToolResultMessage(tool_call_id=call.id, content="ok") for call in calls),
    ]


def test_no_system_anchors_fixed_floor_breakpoint_on_last_tool():
    adapter, client = make_adapter(load_fixture("text_end_turn"))
    adapter.complete("", [UserMessage(text="hi")], TOOLS, PARAMS)
    (request,) = client.messages.calls
    assert "system" not in request
    assert request["tools"][-1]["cache_control"] == {"type": "ephemeral"}


def test_rolling_breakpoint_moves_forward_across_a_session():
    """Simulated multi-turn session: the marker is only ever on the newest
    message; earlier positions carry no marker on later calls (earlier
    cache entries remain valid read points server-side)."""
    adapter, client = make_adapter(load_fixture("text_end_turn"))
    conversation = _conversation_after_turn(2)

    adapter.complete("s", conversation[:1], TOOLS, PARAMS)  # call 1: kickoff only
    adapter.complete("s", conversation, TOOLS, PARAMS)  # call 2: turn appended
    first, second = client.messages.calls

    assert _cache_markers(first) == [("system", 0), ("messages[0]", 0)]
    # Call 2: the kickoff block no longer carries the marker; it moved to
    # the last block of the grouped tool-result message.
    assert _cache_markers(second) == [("system", 0), ("messages[2]", 1)]
    kickoff_block = second["messages"][0]["content"][0]
    assert kickoff_block == {"type": "text", "text": "Session start."}


def test_small_turn_gets_no_intermediate_breakpoint():
    adapter, client = make_adapter(load_fixture("text_end_turn"))
    adapter.complete("s", _conversation_after_turn(8), TOOLS, PARAMS)
    (request,) = client.messages.calls
    # 9 assistant blocks + 8 result blocks = 17 ≤ 20: rolling marker only.
    assert _cache_markers(request) == [("system", 0), ("messages[2]", 7)]


def test_oversized_turn_gets_one_intermediate_breakpoint():
    adapter, client = make_adapter(load_fixture("text_end_turn"))
    adapter.complete("s", _conversation_after_turn(12), TOOLS, PARAMS)
    (request,) = client.messages.calls
    # 13 assistant blocks + 12 result blocks = 25 > the 20-block lookback:
    # the assistant message gets an intermediate marker on its last block
    # so the rolling marker can still find the previous cache entry.
    assert _cache_markers(request) == [
        ("system", 0),
        ("messages[1]", 12),
        ("messages[2]", 11),
    ]


def test_breakpoints_never_exceed_provider_maximum():
    adapter, client = make_adapter(load_fixture("text_end_turn"))
    adapter.complete("s", _conversation_after_turn(30), TOOLS, PARAMS)
    (request,) = client.messages.calls
    assert len(_cache_markers(request)) <= 4


def test_replay_payload_annotation_is_non_destructive():
    """D22 replay blocks are shared across calls: the annotated block is a
    serialized copy; the original SDK objects never gain a marker."""
    replayed = load_fixture("thinking_tool_use").content  # thinking, text, tool_use
    state = ProviderState(provider="anthropic", payload=tuple(replayed))
    adapter, client = make_adapter(load_fixture("text_end_turn"))
    conversation = [
        UserMessage(text="go"),
        AssistantMessage(text=None, tool_calls=(), provider_state=state),
    ]
    adapter.complete("s", conversation, [], PARAMS)
    (request,) = client.messages.calls
    content = request["messages"][1]["content"]
    # First two blocks pass through as the original SDK objects.
    assert content[0] is replayed[0]
    assert content[1] is replayed[1]
    # The annotated last block is a content-identical wire dict.
    assert content[2]["type"] == "tool_use"
    assert content[2]["id"] == replayed[2].id
    assert content[2]["input"] == replayed[2].input
    assert content[2]["cache_control"] == {"type": "ephemeral"}
    # The shared payload objects are untouched for the next call's replay.
    assert not hasattr(replayed[2], "cache_control")
    assert state.payload[2] is replayed[2]


def test_thinking_blocks_are_never_annotated():
    """cache_control is invalid on thinking blocks: the breakpoint walks
    back past them, or is skipped when no cacheable block exists."""
    thinking = load_fixture("thinking_tool_use").content[0]
    state = ProviderState(provider="anthropic", payload=(thinking,))
    adapter, client = make_adapter(load_fixture("text_end_turn"))
    conversation = [
        UserMessage(text="go"),
        AssistantMessage(text=None, tool_calls=(), provider_state=state),
    ]
    adapter.complete("s", conversation, [], PARAMS)
    (request,) = client.messages.calls
    # The replay message has no cacheable block → no marker inside it, and
    # the shared payload object is untouched.
    assert _cache_markers(request) == [("system", 0)]
    assert request["messages"][1]["content"][0] is thinking


# --- token accounting invariant (SPEC §5.2) --------------------------------


def test_usage_passthrough_and_reasoning_tokens_absent():
    adapter, _ = make_adapter(load_fixture("parallel_tool_use"))
    response = adapter.complete("s", [UserMessage(text="hi")], TOOLS, PARAMS)
    # Anthropic reports output_tokens inclusive of thinking tokens (D16):
    # the adapter passes counts through unchanged and reports no
    # informational reasoning subset.
    assert response.usage.input_tokens == 2521
    assert response.usage.output_tokens == 138
    assert response.usage.reasoning_tokens is None
    # Absent wire cache fields normalize to zero components.
    assert response.usage.cache_read_tokens == 0
    assert response.usage.cache_write_tokens == 0


def test_usage_folds_cache_components_into_total_input():
    adapter, _ = make_adapter(load_fixture("cached_usage"))
    response = adapter.complete("s", [UserMessage(text="hi")], TOOLS, PARAMS)
    # Anthropic's wire input_tokens EXCLUDES cached tokens; the canonical
    # figure is the TOTAL prompt count (§5.2 invariant).
    assert response.usage.input_tokens == 314 + 22000 + 928
    assert response.usage.cache_read_tokens == 22000
    assert response.usage.cache_write_tokens == 928
    # Uncached remainder is derivable and matches the wire figure.
    remainder = (
        response.usage.input_tokens
        - response.usage.cache_read_tokens
        - response.usage.cache_write_tokens
    )
    assert remainder == 314


def test_raw_usage_preserved_in_provider_meta():
    adapter, _ = make_adapter(load_fixture("text_end_turn"))
    response = adapter.complete("s", [UserMessage(text="hi")], [], PARAMS)
    assert response.provider_meta["usage"]["input_tokens"] == 2314


# --- retry classification (SPEC §5.5) ---------------------------------------


def _status_error(status: int) -> anthropic.APIStatusError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status, request=request)
    return anthropic.APIStatusError(f"status {status}", response=response, body=None)


@pytest.mark.parametrize("status", [408, 429, 500, 529])
def test_retryable_statuses(status):
    error = _classify_error(_status_error(status))
    assert error.retryable
    assert error.status_code == status


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 422])
def test_fatal_statuses(status):
    error = _classify_error(_status_error(status))
    assert not error.retryable
    assert error.status_code == status


def test_connection_and_timeout_errors_are_retryable():
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    assert _classify_error(anthropic.APIConnectionError(request=request)).retryable
    assert _classify_error(anthropic.APITimeoutError(request=request)).retryable


def test_complete_raises_classified_error_with_cause():
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(429, request=request)
    sdk_error = anthropic.RateLimitError("rate limited", response=response, body=None)
    adapter, _ = make_adapter(sdk_error)
    with pytest.raises(AdapterError) as excinfo:
        adapter.complete("s", [UserMessage(text="hi")], [], PARAMS)
    assert excinfo.value.retryable
    assert excinfo.value.status_code == 429
    assert excinfo.value.__cause__ is sdk_error


def test_non_provider_exceptions_propagate_unwrapped():
    adapter, _ = make_adapter(TypeError("scaffold bug"))
    with pytest.raises(TypeError):
        adapter.complete("s", [UserMessage(text="hi")], [], PARAMS)


# --- client construction -----------------------------------------------------


def test_default_client_disables_sdk_retries():
    # Retries are the loop's job (SPEC §5.5, every retry logged); the SDK's
    # invisible internal retries would corrupt accounting.
    adapter = AnthropicAdapter("claude-haiku-4-5", api_key="test-key")
    assert adapter._client.max_retries == 0
