"""OpenAI adapter: canonical types to/from the Chat Completions API (SPEC §5.1–5.2, §5.5).

Provider quirks handled here and nowhere else:
- system prompt is the leading ``system`` message;
- tool arguments cross the wire as JSON strings (serialized on send,
  parsed on receipt; an unparseable payload is a normalization failure
  and raises, like an unmappable stop reason);
- tool results are one ``tool``-role message per result (no grouping,
  no native error flag — the error text itself is the content);
- ``completion_tokens`` already includes reasoning tokens (D16);
  ``completion_tokens_details.reasoning_tokens`` is the informational
  subset when reported;
- the client is built with ``max_retries=0``: retries are the loop's
  job (SPEC §5.5), never the SDK's;
- provider-side automatic caching is measured, not managed (SPEC §5.2):
  nothing is requested, but ``prompt_tokens_details.cached_tokens`` is
  normalized into ``cache_read_tokens``. ``prompt_tokens`` already
  INCLUDES cached tokens, so canonical ``input_tokens`` passes through
  unchanged; there is no write premium (``cache_write_tokens`` = 0).
"""

from __future__ import annotations

import json
from typing import Any

import openai

from kami_agent.adapters.base import (
    AdapterError,
    AdapterResponse,
    AssistantMessage,
    Message,
    SamplingParams,
    StopReason,
    ToolCall,
    ToolDef,
    ToolResultMessage,
    Usage,
    UserMessage,
)

_STOP_REASONS: dict[str | None, StopReason] = {
    "stop": StopReason.END_TURN,
    "tool_calls": StopReason.TOOL_USE,
    "function_call": StopReason.TOOL_USE,
    "length": StopReason.MAX_TOKENS,
    "content_filter": StopReason.REFUSAL,
}


class OpenAIAdapter:
    """ModelAdapter for the OpenAI Chat Completions API."""

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        client: openai.OpenAI | None = None,
    ) -> None:
        self.model = model
        self._client = client or openai.OpenAI(api_key=api_key, max_retries=0)

    def complete(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolDef],
        params: SamplingParams,
    ) -> AdapterResponse:
        request: dict[str, Any] = {
            "model": self.model,
            "max_completion_tokens": params.max_tokens,
            "messages": _to_wire_messages(system, messages),
        }
        if tools:
            request["tools"] = [_to_wire_tool(tool) for tool in tools]
        if params.temperature is not None:
            request["temperature"] = params.temperature
        if params.reasoning_effort is not None:
            request["reasoning_effort"] = params.reasoning_effort
        try:
            response = self._client.chat.completions.create(**request)
        except openai.OpenAIError as exc:
            raise _classify_error(exc) from exc
        return _normalize(response)


def _to_wire_messages(system: str, messages: list[Message]) -> list[dict[str, Any]]:
    wire: list[dict[str, Any]] = []
    if system:
        wire.append({"role": "system", "content": system})
    for message in messages:
        if isinstance(message, UserMessage):
            wire.append({"role": "user", "content": message.text})
        elif isinstance(message, AssistantMessage):
            entry: dict[str, Any] = {"role": "assistant", "content": message.text}
            if message.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": json.dumps(call.args)},
                    }
                    for call in message.tool_calls
                ]
            wire.append(entry)
        elif isinstance(message, ToolResultMessage):
            wire.append(
                {
                    "role": "tool",
                    "tool_call_id": message.tool_call_id,
                    "content": message.content,
                }
            )
        else:  # pragma: no cover - unreachable with the canonical union
            raise AdapterError(f"unknown message type: {message!r}", retryable=False)
    return wire


def _to_wire_tool(tool: ToolDef) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.input_schema,
        },
    }


def _normalize(response: Any) -> AdapterResponse:
    choice = response.choices[0]
    message = choice.message
    text_blocks = (message.content,) if message.content else ()
    tool_calls = tuple(
        _to_canonical_call(call) for call in (message.tool_calls or []) if _is_function(call)
    )
    usage = response.usage
    details = getattr(usage, "completion_tokens_details", None)
    reasoning = getattr(details, "reasoning_tokens", None) if details is not None else None
    prompt_details = getattr(usage, "prompt_tokens_details", None)
    cached = getattr(prompt_details, "cached_tokens", None) if prompt_details is not None else None
    return AdapterResponse(
        text_blocks=text_blocks,
        tool_calls=tool_calls,
        stop_reason=_normalize_stop_reason(choice.finish_reason),
        usage=Usage(
            # prompt_tokens already INCLUDES cached tokens (§5.2): the total
            # passes through; cached_tokens is the read component (0 when
            # absent) and automatic caching has no write premium.
            input_tokens=usage.prompt_tokens,
            # completion_tokens already includes reasoning tokens (D16).
            output_tokens=usage.completion_tokens,
            reasoning_tokens=reasoning,
            cache_read_tokens=cached or 0,
        ),
        provider_meta=response.model_dump(mode="json"),
    )


def _is_function(call: Any) -> bool:
    return getattr(call, "type", "function") == "function"


def _to_canonical_call(call: Any) -> ToolCall:
    raw = call.function.arguments or "{}"
    try:
        args = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AdapterError(
            f"unparseable tool arguments for {call.function.name}: {raw[:200]!r}",
            retryable=False,
        ) from exc
    if not isinstance(args, dict):
        raise AdapterError(
            f"tool arguments for {call.function.name} are not an object", retryable=False
        )
    return ToolCall(id=call.id, name=call.function.name, args=args)


def _normalize_stop_reason(value: str | None) -> StopReason:
    try:
        return _STOP_REASONS[value]
    except KeyError:
        raise AdapterError(f"unmappable openai finish_reason: {value!r}", retryable=False) from None


def _classify_error(exc: openai.OpenAIError) -> AdapterError:
    if isinstance(exc, openai.APIConnectionError):  # includes APITimeoutError
        return AdapterError(f"openai connection error: {exc}", retryable=True)
    if isinstance(exc, openai.APIStatusError):
        status = exc.status_code
        retryable = status in (408, 429) or status >= 500
        return AdapterError(
            f"openai API error {status}: {exc.message}", retryable=retryable, status_code=status
        )
    return AdapterError(f"openai error: {exc}", retryable=False)
