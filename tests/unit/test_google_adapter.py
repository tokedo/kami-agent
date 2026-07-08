"""Google adapter: fixtures incl. the reasoning-token fold (SPEC §5.2, brief §3.8)."""

import json
from pathlib import Path

import httpx
import pytest
from google.genai import errors as genai_errors
from google.genai import types as genai_types

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
from kami_agent.adapters.google import GoogleAdapter, _classify_error

FIXTURES = Path(__file__).parent / "fixtures" / "google"
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
    return genai_types.GenerateContentResponse.model_validate(
        json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    )


class FakeModels:
    def __init__(self, result):
        self._result = result
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class FakeClient:
    def __init__(self, result):
        self.models = FakeModels(result)


def make_adapter(result):
    client = FakeClient(result)
    return GoogleAdapter("gemini-test", client=client), client


def test_satisfies_model_adapter_protocol():
    adapter, _ = make_adapter(load_fixture("text_stop"))
    assert isinstance(adapter, ModelAdapter)


# --- request mapping -------------------------------------------------------------


def test_request_shape():
    adapter, client = make_adapter(load_fixture("text_stop"))
    adapter.complete("You are an agent.", [UserMessage(text="Session start.")], TOOLS, PARAMS)
    (request,) = client.models.calls
    assert request["model"] == "gemini-test"
    config = request["config"]
    assert config.system_instruction == "You are an agent."
    assert config.max_output_tokens == 4096
    assert config.temperature is None
    declaration = config.tools[0].function_declarations[0]
    assert declaration.name == "workspace_read"
    assert declaration.parameters_json_schema == TOOLS[0].input_schema
    contents = request["contents"]
    assert contents[0].role == "user"
    assert contents[0].parts[0].text == "Session start."


def test_reasoning_effort_is_not_sent():
    # No native equivalent on this provider; tolerated per §5.5.
    adapter, client = make_adapter(load_fixture("text_stop"))
    params = SamplingParams(max_tokens=1024, temperature=0.5, reasoning_effort="high")
    adapter.complete("s", [UserMessage(text="hi")], [], params)
    (request,) = client.models.calls
    assert request["config"].temperature == 0.5
    dumped = request["config"].model_dump_json()
    assert "reasoning_effort" not in dumped
    assert "thinking" not in dumped or '"thinking_config":null' in dumped


def test_conversation_mapping_function_calls_and_grouped_responses():
    adapter, client = make_adapter(load_fixture("text_stop"))
    conversation = [
        UserMessage(text="Session start."),
        AssistantMessage(
            text="Checking.",
            tool_calls=(
                ToolCall(id="call_1", name="workspace_list", args={}),
                ToolCall(id="call_2", name="workspace_read", args={"path": "a.md"}),
            ),
        ),
        ToolResultMessage(tool_call_id="call_1", content="workspace/ (empty)"),
        ToolResultMessage(tool_call_id="call_2", content="boom", is_error=True),
        UserMessage(text="Continue."),
    ]
    adapter.complete("s", conversation, [], PARAMS)
    (request,) = client.models.calls
    contents = request["contents"]

    model_turn = contents[1]
    assert model_turn.role == "model"
    assert model_turn.parts[0].text == "Checking."
    assert model_turn.parts[1].function_call.name == "workspace_list"
    assert model_turn.parts[2].function_call.args == {"path": "a.md"}

    # Both results grouped in ONE user content; ids resolved back to names.
    results_turn = contents[2]
    assert results_turn.role == "user"
    assert len(results_turn.parts) == 2
    first, second = results_turn.parts
    assert first.function_response.name == "workspace_list"
    assert first.function_response.response == {"result": "workspace/ (empty)"}
    assert second.function_response.name == "workspace_read"
    assert second.function_response.response == {"error": "boom"}

    # The trailing user text starts a fresh content.
    assert contents[3].parts[0].text == "Continue."


# --- response normalization --------------------------------------------------------


def test_text_stop_normalization():
    adapter, _ = make_adapter(load_fixture("text_stop"))
    response = adapter.complete("s", [UserMessage(text="hi")], [], PARAMS)
    assert response.text_blocks == ("The workspace is empty.",)
    assert response.tool_calls == ()
    assert response.stop_reason is StopReason.END_TURN
    assert response.usage.input_tokens == 2314
    assert response.usage.output_tokens == 41
    assert response.usage.reasoning_tokens is None


def test_parallel_function_calls_extracted_with_minted_ids():
    adapter, _ = make_adapter(load_fixture("parallel_function_calls"))
    response = adapter.complete("s", [UserMessage(text="hi")], TOOLS, PARAMS)
    # STOP finish + function calls present → tool_use turn.
    assert response.stop_reason is StopReason.TOOL_USE
    assert response.text_blocks == ("Reading the file index and the status.",)
    assert response.tool_calls == (
        ToolCall(id="call_1", name="workspace_list", args={}),
        ToolCall(
            id="call_2",
            name="workspace_read",
            args={"path": "reference/README.md", "offset": 0, "length": 1024},
        ),
    )


def test_the_reasoning_token_fold():
    # THE D16 invariant this adapter exists to enforce: Gemini reports
    # thoughts outside candidatesTokenCount; output_tokens must fold them in.
    adapter, _ = make_adapter(load_fixture("thinking_fold"))
    response = adapter.complete("s", [UserMessage(text="hi")], [], PARAMS)
    assert response.usage.output_tokens == 350  # 100 candidates + 250 thoughts
    assert response.usage.reasoning_tokens == 250
    assert response.usage.input_tokens == 2000


def test_max_tokens_and_safety_normalization():
    adapter, _ = make_adapter(load_fixture("max_tokens"))
    assert (
        adapter.complete("s", [UserMessage(text="hi")], [], PARAMS).stop_reason
        is StopReason.MAX_TOKENS
    )
    adapter, _ = make_adapter(load_fixture("safety"))
    response = adapter.complete("s", [UserMessage(text="hi")], [], PARAMS)
    assert response.stop_reason is StopReason.REFUSAL
    assert response.text_blocks == ()


def test_unmappable_finish_reason_raises():
    fixture = json.loads((FIXTURES / "text_stop.json").read_text())
    fixture["candidates"][0]["finishReason"] = "MALFORMED_FUNCTION_CALL"
    adapter, _ = make_adapter(genai_types.GenerateContentResponse.model_validate(fixture))
    with pytest.raises(AdapterError) as excinfo:
        adapter.complete("s", [UserMessage(text="hi")], [], PARAMS)
    assert not excinfo.value.retryable


def test_no_candidates_raises():
    empty = genai_types.GenerateContentResponse.model_validate({"candidates": []})
    adapter, _ = make_adapter(empty)
    with pytest.raises(AdapterError):
        adapter.complete("s", [UserMessage(text="hi")], [], PARAMS)


# --- retry classification (SPEC §5.5) -----------------------------------------------


def _api_error(status):
    return genai_errors.APIError(status, {"error": {"message": f"status {status}", "status": "X"}})


@pytest.mark.parametrize("status", [408, 429, 500, 503])
def test_retryable_statuses(status):
    error = _classify_error(_api_error(status))
    assert error.retryable
    assert error.status_code == status


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_fatal_statuses(status):
    assert not _classify_error(_api_error(status)).retryable


def test_complete_wraps_api_errors_and_connection_errors():
    sdk_error = _api_error(429)
    adapter, _ = make_adapter(sdk_error)
    with pytest.raises(AdapterError) as excinfo:
        adapter.complete("s", [UserMessage(text="hi")], [], PARAMS)
    assert excinfo.value.retryable
    assert excinfo.value.__cause__ is sdk_error

    adapter, _ = make_adapter(httpx.ConnectError("no route to host"))
    with pytest.raises(AdapterError) as excinfo:
        adapter.complete("s", [UserMessage(text="hi")], [], PARAMS)
    assert excinfo.value.retryable
