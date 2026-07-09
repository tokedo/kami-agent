"""Google adapter: canonical types to/from the Gemini API; reasoning-token fold (SPEC §5.2).

Provider quirks handled here and nowhere else:
- system prompt is ``system_instruction`` in the request config;
- Gemini reports thinking tokens OUTSIDE ``candidates_token_count`` —
  the adapter folds them in (D16): ``output_tokens = candidates +
  thoughts``, with ``reasoning_tokens`` as the informational subset;
- function calls carry no wire IDs; the adapter mints deterministic
  per-response IDs and resolves ``tool_call_id`` back to the function
  name when sending results (Gemini matches responses by name);
- parallel function responses are grouped into one user-role content;
- ``finish_reason`` STOP with function calls present is a tool_use turn;
  safety-class terminations map to ``refusal``;
- ``reasoning_effort`` has no native equivalent and is not sent
  (adapters tolerate provider-specific param subsets, §5.5);
- caching-neutral (D16): implicit provider-side caching may occur but is
  never requested; retries are the loop's job (SPEC §5.5).
"""

from __future__ import annotations

from typing import Any

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

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

PROVIDER = "google"

_REFUSAL_FINISHES = {
    "SAFETY",
    "RECITATION",
    "BLOCKLIST",
    "PROHIBITED_CONTENT",
    "SPII",
    "IMAGE_SAFETY",
}


class GoogleAdapter:
    """ModelAdapter for the Gemini API (google-genai SDK)."""

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        client: genai.Client | None = None,
    ) -> None:
        self.model = model
        self._client = client or genai.Client(api_key=api_key)

    def complete(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolDef],
        params: SamplingParams,
    ) -> AdapterResponse:
        config = genai_types.GenerateContentConfig(
            system_instruction=system or None,
            max_output_tokens=params.max_tokens,
            temperature=params.temperature,
            tools=(
                [genai_types.Tool(function_declarations=[_to_wire_tool(tool) for tool in tools])]
                if tools
                else None
            ),
        )
        try:
            response = self._client.models.generate_content(
                model=self.model, contents=_to_wire_contents(messages), config=config
            )
        except genai_errors.APIError as exc:
            raise _classify_error(exc) from exc
        except httpx.HTTPError as exc:
            raise AdapterError(f"google connection error: {exc}", retryable=True) from exc
        return _normalize(response)


def _to_wire_tool(tool: ToolDef) -> genai_types.FunctionDeclaration:
    return genai_types.FunctionDeclaration(
        name=tool.name,
        description=tool.description,
        parameters_json_schema=tool.input_schema,
    )


def _to_wire_contents(messages: list[Message]) -> list[genai_types.Content]:
    contents: list[genai_types.Content] = []
    call_names: dict[str, str] = {}
    for message in messages:
        if isinstance(message, UserMessage):
            contents.append(
                genai_types.Content(role="user", parts=[genai_types.Part(text=message.text)])
            )
        elif isinstance(message, AssistantMessage):
            # The id→name map is needed for function responses regardless of
            # which replay path builds the model turn.
            for call in message.tool_calls:
                call_names[call.id] = call.name
            state = message.provider_state
            if state is not None and state.provider == PROVIDER:
                # D22 replay: the original response parts, thought
                # signatures included, passed back unchanged.
                contents.append(genai_types.Content(role="model", parts=list(state.payload)))
                continue
            parts: list[genai_types.Part] = []
            if message.text:
                parts.append(genai_types.Part(text=message.text))
            for call in message.tool_calls:
                parts.append(
                    genai_types.Part(
                        function_call=genai_types.FunctionCall(name=call.name, args=call.args)
                    )
                )
            contents.append(genai_types.Content(role="model", parts=parts))
        elif isinstance(message, ToolResultMessage):
            # Gemini matches function responses by name, not id (provider
            # convention); parallel results are grouped into one content.
            name = call_names.get(message.tool_call_id, message.tool_call_id)
            payload = (
                {"error": message.content} if message.is_error else {"result": message.content}
            )
            part = genai_types.Part(
                function_response=genai_types.FunctionResponse(name=name, response=payload)
            )
            if contents and _is_function_response_content(contents[-1]):
                assert contents[-1].parts is not None
                contents[-1].parts.append(part)
            else:
                contents.append(genai_types.Content(role="user", parts=[part]))
        else:  # pragma: no cover - unreachable with the canonical union
            raise AdapterError(f"unknown message type: {message!r}", retryable=False)
    return contents


def _is_function_response_content(content: genai_types.Content) -> bool:
    return bool(
        content.role == "user" and content.parts and content.parts[0].function_response is not None
    )


def _normalize(response: Any) -> AdapterResponse:
    if not response.candidates:
        raise AdapterError("google response has no candidates", retryable=False)
    candidate = response.candidates[0]
    parts = (candidate.content.parts if candidate.content else None) or []

    text_blocks = tuple(part.text for part in parts if part.text)
    tool_calls: list[ToolCall] = []
    for part in parts:
        call = part.function_call
        if call is None:
            continue
        # Gemini function calls carry no wire id; mint a deterministic one.
        minted = call.id or f"call_{len(tool_calls) + 1}"
        tool_calls.append(ToolCall(id=minted, name=call.name or "", args=dict(call.args or {})))

    # D22: thought signatures ride on parts; keep the original parts for
    # verbatim same-session replay when any are present.
    provider_state = None
    if any(getattr(part, "thought_signature", None) for part in parts):
        provider_state = ProviderState(provider=PROVIDER, payload=tuple(parts))

    usage = response.usage_metadata
    thoughts = getattr(usage, "thoughts_token_count", None)
    candidates_tokens = (usage.candidates_token_count or 0) if usage else 0
    return AdapterResponse(
        text_blocks=text_blocks,
        tool_calls=tuple(tool_calls),
        stop_reason=_normalize_stop_reason(candidate.finish_reason, bool(tool_calls)),
        provider_state=provider_state,
        usage=Usage(
            input_tokens=(usage.prompt_token_count or 0) if usage else 0,
            # The D16 fold: Gemini reports thoughts outside the candidate
            # count; output_tokens must include them.
            output_tokens=candidates_tokens + (thoughts or 0),
            reasoning_tokens=thoughts,
        ),
        provider_meta=response.model_dump(mode="json"),
    )


def _normalize_stop_reason(finish_reason: Any, has_tool_calls: bool) -> StopReason:
    name = getattr(finish_reason, "name", None) or (
        str(finish_reason) if finish_reason is not None else None
    )
    if name == "STOP":
        return StopReason.TOOL_USE if has_tool_calls else StopReason.END_TURN
    if name == "MAX_TOKENS":
        return StopReason.MAX_TOKENS
    if name in _REFUSAL_FINISHES:
        return StopReason.REFUSAL
    if name == "MALFORMED_FUNCTION_CALL":
        # A transient Gemini generation artifact: the turn carries no usable
        # tool call. Normalized to end_turn so the loop's §5.4 path applies —
        # frozen continuation string, counts as one error — instead of
        # killing the session over a provider quirk.
        return StopReason.END_TURN
    raise AdapterError(f"unmappable google finish_reason: {name!r}", retryable=False)


def _classify_error(exc: genai_errors.APIError) -> AdapterError:
    status = exc.code
    retryable = status in (408, 429) or (status or 0) >= 500
    return AdapterError(
        f"google API error {status}: {exc.message}", retryable=retryable, status_code=status
    )
