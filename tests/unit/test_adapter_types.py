"""Canonical adapter types (SPEC §5.1–5.2)."""

import dataclasses

import pytest

from kami_agent.adapters.base import (
    AdapterResponse,
    AssistantMessage,
    ModelAdapter,
    SamplingParams,
    StopReason,
    ToolCall,
    ToolDef,
    ToolResultMessage,
    Usage,
    UserMessage,
)


def test_stop_reason_is_exactly_the_spec_enum():
    assert {r.value for r in StopReason} == {"end_turn", "tool_use", "max_tokens", "refusal"}


def test_message_roles():
    user = UserMessage(text="hello")
    assistant = AssistantMessage(text="hi", tool_calls=(ToolCall(id="t1", name="f", args={}),))
    result = ToolResultMessage(tool_call_id="t1", content="ok")
    assert user.role == "user"
    assert assistant.role == "assistant"
    assert result.role == "tool_result"
    assert result.is_error is False


def test_assistant_message_optional_fields():
    msg = AssistantMessage()
    assert msg.text is None
    assert msg.tool_calls == ()


def test_tool_result_error_flag():
    err = ToolResultMessage(tool_call_id="t1", content="boom", is_error=True)
    assert err.is_error is True


def test_usage_reasoning_tokens_optional():
    usage = Usage(input_tokens=10, output_tokens=20)
    assert usage.reasoning_tokens is None
    folded = Usage(input_tokens=10, output_tokens=25, reasoning_tokens=5)
    assert folded.reasoning_tokens == 5


def test_usage_cache_components_default_to_zero():
    # SPEC §5.2: input_tokens is the total; the cache components are
    # subsets and default to 0 for providers/calls without caching.
    usage = Usage(input_tokens=10, output_tokens=20)
    assert usage.cache_read_tokens == 0
    assert usage.cache_write_tokens == 0
    cached = Usage(input_tokens=100, output_tokens=20, cache_read_tokens=70, cache_write_tokens=20)
    assert cached.input_tokens - cached.cache_read_tokens - cached.cache_write_tokens == 10


def test_sampling_params_defaults():
    params = SamplingParams(max_tokens=1024)
    assert params.temperature is None
    assert params.reasoning_effort is None


def test_adapter_response_shape():
    response = AdapterResponse(
        text_blocks=("thinking about it",),
        tool_calls=(ToolCall(id="t1", name="get_status", args={}),),
        stop_reason=StopReason.TOOL_USE,
        usage=Usage(input_tokens=100, output_tokens=50),
        provider_meta={"raw": "never parsed by the loop"},
    )
    assert response.stop_reason is StopReason.TOOL_USE
    assert response.tool_calls[0].name == "get_status"


def test_tool_def_fields():
    tool = ToolDef(
        name="workspace_read",
        description="d",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
    )
    assert tool.input_schema["type"] == "object"


def test_types_are_frozen():
    user = UserMessage(text="x")
    with pytest.raises(dataclasses.FrozenInstanceError):
        user.text = "y"  # type: ignore[misc]


def test_model_adapter_protocol_structural_check():
    class Fake:
        def complete(self, system, messages, tools, params):
            return AdapterResponse(
                text_blocks=(),
                tool_calls=(),
                stop_reason=StopReason.END_TURN,
                usage=Usage(input_tokens=0, output_tokens=0),
            )

    assert isinstance(Fake(), ModelAdapter)
    assert not isinstance(object(), ModelAdapter)
