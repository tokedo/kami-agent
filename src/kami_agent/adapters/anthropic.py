"""Anthropic adapter: canonical types to/from the Messages API (SPEC §5.1–5.2, §5.5).

Provider quirks handled here and nowhere else:
- system prompt is a top-level param, not a message;
- all tool results for one assistant turn go back in a single user
  message (splitting them degrades the provider's parallel calling);
- ``output_tokens`` already includes thinking tokens (D16) — no fold
  needed; no separate reasoning-token count is reported;
- the client is built with ``max_retries=0``: retries are the loop's job
  (SPEC §5.5 — every retry is logged), never the SDK's;
- explicit prompt caching (SPEC §5.2): the adapter places
  ``cache_control`` breakpoints (5-minute ephemeral) — one on the last
  system block (render order is tools → system → messages, so it caches
  the whole fixed floor) and a rolling one on the last content block of
  the final message, moved forward each call. Earlier cache entries
  remain valid read points, so hits accrue as the session grows.
  ``cache_control`` is request metadata: the prompt bytes sent to the
  model are identical with or without it, and nothing about caching is
  agent-visible (D12);
- wire ``usage.input_tokens`` EXCLUDES cached tokens; the adapter folds
  ``cache_read_input_tokens`` and ``cache_creation_input_tokens`` back in
  so canonical ``input_tokens`` is the total prompt count (§5.2).
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

# 5-minute ephemeral cache entries (SPEC §5.2). Request metadata only —
# never part of the prompt bytes the model sees.
_CACHE_CONTROL = {"type": "ephemeral"}

# A breakpoint looks back at most this many content blocks for the previous
# cache entry (provider constraint). A turn that appends more blocks than
# this gets one intermediate breakpoint so the chain of entries stays
# connected. Max 4 breakpoints per request; this adapter places at most 3.
_CACHE_LOOKBACK_BLOCKS = 20

# cache_control is not accepted on thinking blocks; a breakpoint that would
# land on one walks back to the nearest cacheable block instead.
_UNCACHEABLE_BLOCK_TYPES = frozenset({"thinking", "redacted_thinking"})

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
        if tools:
            request["tools"] = [_to_wire_tool(tool) for tool in tools]
        if system:
            # Breakpoint 1: the last system block. Render order is tools →
            # system → messages, so this one entry caches the entire fixed
            # floor (all tool schemas + the system prompt). The system text
            # itself is byte-identical to the plain-string form.
            request["system"] = [{"type": "text", "text": system, "cache_control": _CACHE_CONTROL}]
        elif tools:
            # No system prompt (connectivity checks, unit fixtures): anchor
            # the fixed-floor entry on the last tool definition instead.
            request["tools"][-1]["cache_control"] = _CACHE_CONTROL
        _place_message_breakpoints(request["messages"])
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


def _place_message_breakpoints(wire: list[dict[str, Any]]) -> None:
    """Rolling cache breakpoints over the message list (SPEC §5.2).

    The rolling breakpoint sits on the last content block of the most
    recently appended message and moves forward each call (the wire list
    is rebuilt per call, so earlier positions carry no marker). Earlier
    cache entries remain valid read points, so hits accrue incrementally.

    One turn appends at most two messages since the previous call (the
    assistant turn + its grouped tool results, or the continuation pair).
    When those two messages together exceed the provider's 20-block
    lookback window, the rolling breakpoint could not find the previous
    entry — an intermediate breakpoint on the earlier of the two messages
    splits the gap. Total: ≤ 2 message breakpoints (+1 fixed-floor
    breakpoint), under the 4-per-request maximum.
    """
    if not wire:
        return
    targets = [wire[-1]]
    if len(wire) >= 2:
        turn_blocks = _block_count(wire[-1]) + _block_count(wire[-2])
        if turn_blocks > _CACHE_LOOKBACK_BLOCKS:
            targets.insert(0, wire[-2])
    for message in targets:
        _annotate_last_cacheable_block(message)


def _block_count(message: dict[str, Any]) -> int:
    content = message.get("content")
    return len(content) if isinstance(content, list) else 1


def _annotate_last_cacheable_block(message: dict[str, Any]) -> None:
    """Attach cache_control to the last cacheable block, non-destructively.

    Blocks are replaced by annotated copies (never mutated in place):
    replayed D22 payloads are shared across calls, and a stale marker left
    on a mid-conversation block would count against the 4-breakpoint
    budget on every later call.
    """
    content = message.get("content")
    if not isinstance(content, list):
        return
    for index in range(len(content) - 1, -1, -1):
        annotated = _annotated_block(content[index])
        if annotated is not None:
            content[index] = annotated
            return


def _annotated_block(block: Any) -> dict[str, Any] | None:
    """A copy of ``block`` carrying cache_control, or None if uncacheable."""
    if not isinstance(block, dict):
        # D22 replay payloads carry SDK response objects; serialize the one
        # annotated block to its wire dict (content-identical) and leave
        # every other block untouched.
        dump = getattr(block, "model_dump", None)
        if not callable(dump):
            return None
        block = dump(mode="json", exclude_none=True)
    if block.get("type") in _UNCACHEABLE_BLOCK_TYPES:
        return None
    return {**block, "cache_control": _CACHE_CONTROL}


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
    # Anthropic's wire input_tokens EXCLUDES cached tokens; canonical
    # input_tokens is the TOTAL prompt count (§5.2 invariant), so the cache
    # components are folded back in. Absent/None fields mean no caching.
    cache_read = getattr(response.usage, "cache_read_input_tokens", None) or 0
    cache_write = getattr(response.usage, "cache_creation_input_tokens", None) or 0
    usage = Usage(
        input_tokens=response.usage.input_tokens + cache_read + cache_write,
        # Anthropic's count already includes reasoning/thinking tokens (D16);
        # no separate reasoning_tokens figure is reported.
        output_tokens=response.usage.output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
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
