"""Canonical types: Message, ToolDef, AdapterResponse, ModelAdapter protocol (SPEC §5.1–5.2).

The loop speaks only these types. Adapters map them to each provider's wire
format; provider quirks never leave the adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable


class StopReason(StrEnum):
    """Normalized stop reason (SPEC §5.1)."""

    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"
    REFUSAL = "refusal"


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One tool-call intent from an assistant turn."""

    id: str
    name: str
    args: dict[str, Any]


@dataclass(frozen=True, slots=True)
class UserMessage:
    text: str
    role: Literal["user"] = "user"


@dataclass(frozen=True, slots=True)
class AssistantMessage:
    text: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    role: Literal["assistant"] = "assistant"


@dataclass(frozen=True, slots=True)
class ToolResultMessage:
    tool_call_id: str
    content: str
    is_error: bool = False
    role: Literal["tool_result"] = "tool_result"


Message = UserMessage | AssistantMessage | ToolResultMessage


@dataclass(frozen=True, slots=True)
class ToolDef:
    """Tool definition authored once in JSON Schema, translated per provider.

    Schemas restrict themselves to the feature subset all three providers
    accept: objects, scalars, arrays, enums, required — no oneOf/anyOf/allOf.
    """

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Usage:
    """Token usage for one model call.

    ``output_tokens`` MUST include reasoning/thinking tokens (D16); adapters
    fold them in when the provider reports them outside the output count.
    ``reasoning_tokens`` is an informational subset, set when the provider
    reports it.
    """

    input_tokens: int
    output_tokens: int
    reasoning_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class SamplingParams:
    """Sampling parameters, pinned per run in the manifest (SPEC §5.5).

    Adapters send only the subset their provider accepts; the manifest
    records exactly what was sent.
    """

    max_tokens: int
    temperature: float | None = None
    reasoning_effort: str | None = None


@dataclass(frozen=True, slots=True)
class AdapterResponse:
    """Normalized model response (SPEC §5.1).

    ``provider_meta`` is logged raw and never parsed by the loop.
    """

    text_blocks: tuple[str, ...]
    tool_calls: tuple[ToolCall, ...]
    stop_reason: StopReason
    usage: Usage
    provider_meta: dict[str, Any] = field(default_factory=dict)


class AdapterError(Exception):
    """A provider call failed, normalized for the loop's retry policy.

    ``retryable`` is True for the SPEC §5.5 backoff cases — rate limits
    (429), server errors (5xx), and timeouts/connection failures — and
    False for everything else (auth, bad request, unmappable response).
    """

    def __init__(self, message: str, *, retryable: bool, status_code: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


@runtime_checkable
class ModelAdapter(Protocol):
    """One provider adapter; native tool calling, normalized in/out."""

    def complete(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolDef],
        params: SamplingParams,
    ) -> AdapterResponse: ...
