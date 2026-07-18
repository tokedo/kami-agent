"""OpenAI adapter: recorded-fixture normalization + retry classification (brief §3.8)."""

import json
from pathlib import Path

import httpx
import openai
import pytest
from openai.types.chat import ChatCompletion

from kami_agent.adapters.base import (
    AdapterError,
    AssistantMessage,
    ModelAdapter,
    SamplingParams,
    StopReason,
    ToolCall,
    ToolDef,
    ToolResultMessage,
    UserMessage,
)
from kami_agent.adapters.openai import (
    OpenAIAdapter,
    _classify_error,
    _normalize_stop_reason,
)

FIXTURES = Path(__file__).parent / "fixtures" / "openai"
PARAMS = SamplingParams(max_tokens=4096)

TOOLS = [
    ToolDef(
        name="workspace_read",
        description="Read a file.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    )
]


def load_fixture(name):
    return ChatCompletion.model_validate(
        json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    )


class FakeCompletions:
    def __init__(self, result):
        self._result = result
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class FakeChat:
    def __init__(self, result):
        self.completions = FakeCompletions(result)


class FakeClient:
    def __init__(self, result):
        self.chat = FakeChat(result)


def make_adapter(result):
    client = FakeClient(result)
    return OpenAIAdapter("gpt-test", client=client), client


def test_satisfies_model_adapter_protocol():
    adapter, _ = make_adapter(load_fixture("text_stop"))
    assert isinstance(adapter, ModelAdapter)


# --- message mapping ----------------------------------------------------------


def test_request_shape():
    adapter, client = make_adapter(load_fixture("text_stop"))
    adapter.complete("You are an agent.", [UserMessage(text="Session start.")], TOOLS, PARAMS)
    (request,) = client.chat.completions.calls
    assert request["model"] == "gpt-test"
    assert request["max_completion_tokens"] == 4096
    assert request["messages"][0] == {"role": "system", "content": "You are an agent."}
    assert request["messages"][1] == {"role": "user", "content": "Session start."}
    assert request["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "workspace_read",
                "description": "Read a file.",
                "parameters": TOOLS[0].input_schema,
            },
        }
    ]
    assert "temperature" not in request
    assert "reasoning_effort" not in request


def test_optional_params_sent_when_set():
    adapter, client = make_adapter(load_fixture("text_stop"))
    params = SamplingParams(max_tokens=1024, temperature=0.5, reasoning_effort="low")
    adapter.complete("s", [UserMessage(text="hi")], [], params)
    (request,) = client.chat.completions.calls
    assert request["temperature"] == 0.5
    assert request["reasoning_effort"] == "low"
    assert "tools" not in request


def test_conversation_mapping_serializes_args_and_splits_tool_results():
    adapter, client = make_adapter(load_fixture("text_stop"))
    conversation = [
        UserMessage(text="Session start."),
        AssistantMessage(
            text="Checking.",
            tool_calls=(
                ToolCall(id="call_AAA111", name="workspace_list", args={}),
                ToolCall(id="call_BBB222", name="workspace_read", args={"path": "a.md"}),
            ),
        ),
        ToolResultMessage(tool_call_id="call_AAA111", content="workspace/ (empty)"),
        ToolResultMessage(tool_call_id="call_BBB222", content="boom", is_error=True),
    ]
    adapter.complete("s", conversation, [], PARAMS)
    (request,) = client.chat.completions.calls
    assistant = request["messages"][2]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == "Checking."
    assert assistant["tool_calls"][0]["function"] == {
        "name": "workspace_list",
        "arguments": "{}",
    }
    assert json.loads(assistant["tool_calls"][1]["function"]["arguments"]) == {"path": "a.md"}
    # One tool-role message per result — no grouping on this provider.
    assert request["messages"][3] == {
        "role": "tool",
        "tool_call_id": "call_AAA111",
        "content": "workspace/ (empty)",
    }
    assert request["messages"][4] == {
        "role": "tool",
        "tool_call_id": "call_BBB222",
        "content": "boom",
    }


# --- response normalization -----------------------------------------------------


def test_text_stop_normalization():
    adapter, _ = make_adapter(load_fixture("text_stop"))
    response = adapter.complete("s", [UserMessage(text="hi")], [], PARAMS)
    assert response.text_blocks == ("The workspace is empty.",)
    assert response.tool_calls == ()
    assert response.stop_reason is StopReason.END_TURN
    assert response.usage.input_tokens == 2314
    assert response.usage.output_tokens == 41
    assert response.usage.reasoning_tokens is None
    assert response.provider_meta["id"] == "chatcmpl-ExampleText01"


def test_parallel_intent_extraction_parses_json_arguments():
    adapter, _ = make_adapter(load_fixture("parallel_tool_calls"))
    response = adapter.complete("s", [UserMessage(text="hi")], TOOLS, PARAMS)
    assert response.stop_reason is StopReason.TOOL_USE
    assert response.tool_calls == (
        ToolCall(id="call_AAA111", name="workspace_list", args={}),
        ToolCall(
            id="call_BBB222",
            name="workspace_read",
            args={"path": "reference/README.md", "offset": 0, "length": 1024},
        ),
    )


def test_reasoning_tokens_subset_extracted():
    # completion_tokens already includes reasoning tokens (D16): pass
    # through unchanged, expose the informational subset.
    adapter, _ = make_adapter(load_fixture("reasoning_usage"))
    response = adapter.complete("s", [UserMessage(text="hi")], [], PARAMS)
    assert response.usage.output_tokens == 340
    assert response.usage.reasoning_tokens == 120


def test_absent_cache_fields_normalize_to_zero():
    adapter, _ = make_adapter(load_fixture("text_stop"))
    response = adapter.complete("s", [UserMessage(text="hi")], [], PARAMS)
    assert response.usage.cache_read_tokens == 0
    assert response.usage.cache_write_tokens == 0


def test_cached_prompt_tokens_are_a_component_not_an_addition():
    # prompt_tokens already INCLUDES cached tokens (SPEC §5.2): the total
    # passes through unchanged; cached_tokens is the read component and
    # automatic caching has no write premium.
    adapter, _ = make_adapter(load_fixture("cached_usage"))
    response = adapter.complete("s", [UserMessage(text="hi")], [], PARAMS)
    assert response.usage.input_tokens == 13741
    assert response.usage.cache_read_tokens == 12800
    assert response.usage.cache_write_tokens == 0
    uncached = response.usage.input_tokens - response.usage.cache_read_tokens
    assert uncached == 941


def test_length_and_content_filter_normalization():
    adapter, _ = make_adapter(load_fixture("length"))
    assert (
        adapter.complete("s", [UserMessage(text="hi")], [], PARAMS).stop_reason
        is StopReason.MAX_TOKENS
    )
    adapter, _ = make_adapter(load_fixture("content_filter"))
    response = adapter.complete("s", [UserMessage(text="hi")], [], PARAMS)
    assert response.stop_reason is StopReason.REFUSAL
    assert response.text_blocks == ()


def test_stop_reason_table_and_unmappable():
    assert _normalize_stop_reason("stop") is StopReason.END_TURN
    assert _normalize_stop_reason("tool_calls") is StopReason.TOOL_USE
    assert _normalize_stop_reason("function_call") is StopReason.TOOL_USE
    assert _normalize_stop_reason("length") is StopReason.MAX_TOKENS
    assert _normalize_stop_reason("content_filter") is StopReason.REFUSAL
    with pytest.raises(AdapterError):
        _normalize_stop_reason("novel_reason")
    with pytest.raises(AdapterError):
        _normalize_stop_reason(None)


def test_malformed_tool_arguments_raise():
    fixture = json.loads((FIXTURES / "parallel_tool_calls.json").read_text())
    fixture["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "{not json"
    adapter, _ = make_adapter(ChatCompletion.model_validate(fixture))
    with pytest.raises(AdapterError) as excinfo:
        adapter.complete("s", [UserMessage(text="hi")], [], PARAMS)
    assert not excinfo.value.retryable


# --- retry classification (SPEC §5.5) ---------------------------------------------


def _status_error(status):
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx.Response(status, request=request)
    return openai.APIStatusError(f"status {status}", response=response, body=None)


@pytest.mark.parametrize("status", [408, 429, 500, 503])
def test_retryable_statuses(status):
    error = _classify_error(_status_error(status))
    assert error.retryable
    assert error.status_code == status


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_fatal_statuses(status):
    assert not _classify_error(_status_error(status)).retryable


def test_connection_and_timeout_errors_are_retryable():
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    assert _classify_error(openai.APIConnectionError(request=request)).retryable
    assert _classify_error(openai.APITimeoutError(request=request)).retryable


def test_complete_wraps_sdk_errors_with_cause():
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    sdk_error = openai.RateLimitError(
        "rate limited", response=httpx.Response(429, request=request), body=None
    )
    adapter, _ = make_adapter(sdk_error)
    with pytest.raises(AdapterError) as excinfo:
        adapter.complete("s", [UserMessage(text="hi")], [], PARAMS)
    assert excinfo.value.retryable
    assert excinfo.value.__cause__ is sdk_error


def test_default_client_disables_sdk_retries():
    adapter = OpenAIAdapter("gpt-test", api_key="test-key")
    assert adapter._client.max_retries == 0
