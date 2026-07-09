"""Anthropic adapter: canonical types to/from the Messages API (SPEC §5.1–5.2, §5.5).

Provider quirks handled here and nowhere else:
- system prompt is a top-level param, not a message;
- all tool results for one assistant turn go back in a single user
  message (splitting them degrades the provider's parallel calling);
- ``output_tokens`` already includes thinking tokens (D16) — no fold
  needed; no separate reasoning-token count is reported;
- the client is built with ``max_retries=0``: retries are the loop's job
  (SPEC §5.5 — every retry is logged), never the SDK's;
- caching-neutral (D16): no cache_control anywhere.
"""

from __future__ import annotations

from typing import Any

import anthropic

from kami_agent.adapters.base import (
    AdapterError,
    AdapterResponse,
    AssistantMessage,
    Message,
    ProviderState,
    SamplingParams,
    StopReason,
    ToolCall,
    ToolDef,
    ToolResultMessage,
    Usage,
    UserMessage,
)

# Provider stop reasons → canonical enum (SPEC §5.1). "stop_sequence" cannot
# occur (the scaffold sets no stop sequences) but maps safely to end_turn.
# Anything absent — e.g. "pause_turn" (server tools, never requested) — is
# unmappable and raises: a silently misclassified stop reason would corrupt
# the loop's control flow.
PROVIDER = "anthropic"

_STOP_REASONS: dict[str | None, StopReason] = {
    "end_turn": StopReason.END_TURN,
    "tool_use": StopReason.TOOL_USE,
    "max_tokens": StopReason.MAX_TOKENS,
    "refusal": StopReason.REFUSAL,
    "stop_sequence": StopReason.END_TURN,
}


class AnthropicAdapter:
    """ModelAdapter for the Anthropic Messages API."""

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        client: anthropic.Anthropic | None = None,
    ) -> None:
        self.model = model
        self._client = client or anthropic.Anthropic(api_key=api_key, max_retries=0)

    def complete(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolDef],
        params: SamplingParams,
    ) -> AdapterResponse:
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": params.max_tokens,
            "messages": _to_wire_messages(messages),
        }
        if system:
            request["system"] = system
        if tools:
            request["tools"] = [_to_wire_tool(tool) for tool in tools]
        if params.temperature is not None:
            request["temperature"] = params.temperature
        if params.reasoning_effort is not None:
            request["output_config"] = {"effort": params.reasoning_effort}
        try:
            response = self._client.messages.create(**request)
        except anthropic.APIError as exc:
            raise _classify_error(exc) from exc
        return _normalize(response)


def _to_wire_messages(messages: list[Message]) -> list[dict[str, Any]]:
    wire: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, UserMessage):
            wire.append({"role": "user", "content": [{"type": "text", "text": message.text}]})
        elif isinstance(message, AssistantMessage):
            state = message.provider_state
            if state is not None and state.provider == PROVIDER:
                # D22 replay: the original response content blocks (signed
                # thinking blocks included), passed back unchanged.
                wire.append({"role": "assistant", "content": list(state.payload)})
                continue
            content: list[dict[str, Any]] = []
            if message.text:
                content.append({"type": "text", "text": message.text})
            for call in message.tool_calls:
                content.append(
                    {"type": "tool_use", "id": call.id, "name": call.name, "input": call.args}
                )
            wire.append({"role": "assistant", "content": content})
        elif isinstance(message, ToolResultMessage):
            block: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": message.tool_call_id,
                "content": message.content,
            }
            if message.is_error:
                block["is_error"] = True
            # Tool-result pairing per provider convention: every result for
            # one assistant turn goes in a single following user message.
            if wire and _is_tool_result_message(wire[-1]):
                wire[-1]["content"].append(block)
            else:
                wire.append({"role": "user", "content": [block]})
        else:  # pragma: no cover - unreachable with the canonical union
            raise AdapterError(f"unknown message type: {message!r}", retryable=False)
    return wire


def _is_tool_result_message(wire_message: dict[str, Any]) -> bool:
    return (
        wire_message["role"] == "user"
        and bool(wire_message["content"])
        and wire_message["content"][0].get("type") == "tool_result"
    )


def _to_wire_tool(tool: ToolDef) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.input_schema,
    }


def _normalize(response: anthropic.types.Message) -> AdapterResponse:
    text_blocks = tuple(block.text for block in response.content if block.type == "text")
    tool_calls = tuple(
        ToolCall(id=block.id, name=block.name, args=block.input)
        for block in response.content
        if block.type == "tool_use"
    )
    # D22: when the turn carries (signed/redacted) thinking blocks, keep the
    # complete original content for verbatim same-session replay.
    provider_state = None
    if any(block.type in ("thinking", "redacted_thinking") for block in response.content):
        provider_state = ProviderState(provider=PROVIDER, payload=tuple(response.content))
    usage = Usage(
        input_tokens=response.usage.input_tokens,
        # Anthropic's count already includes reasoning/thinking tokens (D16);
        # no separate reasoning_tokens figure is reported.
        output_tokens=response.usage.output_tokens,
    )
    return AdapterResponse(
        text_blocks=text_blocks,
        tool_calls=tool_calls,
        stop_reason=_normalize_stop_reason(response.stop_reason),
        usage=usage,
        provider_state=provider_state,
        provider_meta=response.model_dump(mode="json"),
    )


def _normalize_stop_reason(value: str | None) -> StopReason:
    try:
        return _STOP_REASONS[value]
    except KeyError:
        raise AdapterError(
            f"unmappable anthropic stop_reason: {value!r}", retryable=False
        ) from None


def _classify_error(exc: anthropic.APIError) -> AdapterError:
    if isinstance(exc, anthropic.APIConnectionError):  # includes APITimeoutError
        return AdapterError(f"anthropic connection error: {exc}", retryable=True)
    if isinstance(exc, anthropic.APIStatusError):
        status = exc.status_code
        retryable = status in (408, 429) or status >= 500
        return AdapterError(
            f"anthropic API error {status}: {exc.message}",
            retryable=retryable,
            status_code=status,
        )
    return AdapterError(f"anthropic error: {exc}", retryable=False)
