"""Canonical types: Message, ToolDef, AdapterResponse, ModelAdapter protocol (SPEC P8, P7.1).

The loop speaks only these types. Adapters map them to each provider's wire
format; provider quirks never leave the adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable


class StopReason(StrEnum):
    """Normalized stop reason (SPEC P8)."""

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
class ProviderState:
    """Opaque provider reasoning state on an assistant message (SPEC P8, I17).

    Set by the emitting adapter (e.g. Anthropic signed thinking blocks,
    Gemini thought signatures) and replayed by that same adapter within
    the same session. The loop never inspects ``payload``; it never
    crosses sessions and never reaches telemetry. Adapters ignore state
    they did not produce (matched on ``provider``).
    """

    provider: str
    payload: Any


@dataclass(frozen=True, slots=True)
class AssistantMessage:
    """One assistant turn.

    ``initiator`` names who produced the turn: None for the model's own
    turns, ``"scaffold"`` for the session-start injections the loop
    synthesizes (SPEC P1.12). It is **transcript provenance only** — no
    adapter reads it, so the bytes sent to the provider are identical
    with or without it. Without it a synthesized turn is
    indistinguishable from a model turn in a transcript, and anything
    counting assistant rows as model turns over-counts by the number of
    injections (P12).
    """

    text: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    provider_state: ProviderState | None = None
    initiator: str | None = None
    role: Literal["assistant"] = "assistant"


@dataclass(frozen=True, slots=True)
class ToolResultMessage:
    """One tool result.

    ``initiator`` is the same transcript-only provenance the assistant
    turn carries, on the result half of an injected pair (P12).
    """

    tool_call_id: str
    content: str
    is_error: bool = False
    initiator: str | None = None
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

    ``output_tokens`` MUST include reasoning/thinking tokens (P7.1); adapters
    fold them in when the provider reports them outside the output count.
    ``reasoning_tokens`` is an informational subset, set when the provider
    reports it.

    ``input_tokens`` is the TOTAL prompt token count for the call (SPEC
    P7.1 invariant — this is what reconciles against provider dashboards).
    ``cache_read_tokens`` and ``cache_write_tokens`` are component subsets
    of ``input_tokens``; the uncached remainder is ``input_tokens −
    cache_read_tokens − cache_write_tokens``. Providers whose wire usage
    EXCLUDES cached tokens from the prompt count (Anthropic) fold them back
    in inside the adapter; providers whose count already includes them
    (OpenAI, Gemini) pass the total through unchanged.

    ``cache_write_5m_tokens`` / ``cache_write_1h_tokens`` decompose
    ``cache_write_tokens`` by cache lifetime where the provider reports
    the split (Anthropic). None means the provider serves no such split —
    which is not the same as a split of zero, and is why these are
    None-defaulted rather than 0-defaulted.
    """

    input_tokens: int
    output_tokens: int
    reasoning_tokens: int | None = None
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cache_write_5m_tokens: int | None = None
    cache_write_1h_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class SamplingParams:
    """Sampling parameters, pinned per run in the manifest (SPEC D3).

    Adapters send only the subset their provider accepts; the manifest
    records exactly what was sent.
    """

    max_tokens: int
    temperature: float | None = None
    reasoning_effort: str | None = None


@dataclass(frozen=True, slots=True)
class AdapterResponse:
    """Normalized model response (SPEC P8).

    ``provider_meta`` is logged raw and never parsed by the loop.
    ``provider_state`` (I17) is copied verbatim onto the assistant
    message for same-session replay by the emitting adapter.

    ``request_id`` is the provider's own identifier for this call, where
    it serves one — the join key for a support conversation or an
    invoice line that telemetry alone cannot supply. None where the
    provider exposes none; the adapter never mints one.
    """

    text_blocks: tuple[str, ...]
    tool_calls: tuple[ToolCall, ...]
    stop_reason: StopReason
    usage: Usage
    provider_state: ProviderState | None = None
    provider_meta: dict[str, Any] = field(default_factory=dict)
    request_id: str | None = None


# What of a provider error message survives into the telemetry row (SPEC
# P9). The on-disk error log keeps a longer cut (P14); this bound exists
# because llm_call rows are read in bulk and a provider that echoes a
# request back would otherwise bloat every row of an outage.
ERROR_TEXT_TELEMETRY_CHARS = 200

# What survives into the on-disk error artifact (SPEC P14). Long enough to
# hold a real provider message with its detail intact, bounded so a
# hostile or runaway payload cannot fill a disk one line at a time.
ERROR_TEXT_LOG_CHARS = 4096


class AdapterError(Exception):
    """A provider call failed, normalized for the loop's retry policy.

    ``retryable`` is True for the SPEC P8 backoff cases — rate limits
    (429), server errors (5xx), and timeouts/connection failures — and
    False for everything else (auth, bad request, unmappable response).

    ``request_id`` carries the provider's identifier for the failed call
    where the SDK exposes one on the error, so a failed-but-billed
    attempt is as traceable as a successful one.

    ``error_type`` and ``error_text`` carry **the provider's own words**
    about what went wrong: the error-type token the API returned
    (``insufficient_quota``, ``invalid_request_error``,
    ``RESOURCE_EXHAUSTED``) and its human-readable message. Both are None
    when the failure happened before any provider answer existed — a
    connection reset has no type and no message the provider authored.
    They exist because a run-wide provider outage used to produce error
    rows carrying a request id and nothing else, which cannot answer the
    first question anyone asks after an incident: what did the provider
    say?

    ``error_text`` is the message ONLY. Adapters never fold the request
    payload into it: the SDKs' own ``str(exc)`` for a status error embeds
    the whole response body, and quoting a body back into a row that is
    read in bulk is how a diagnostic field becomes an unreadable one.
    """

    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        status_code: int | None = None,
        request_id: str | None = None,
        error_type: str | None = None,
        error_text: str | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code
        self.request_id = request_id
        self.error_type = error_type
        self.error_text = error_text

    def text_for_telemetry(self) -> str | None:
        """``error_text`` cut to what an llm_call row carries (P9)."""
        return _cut(self.error_text, ERROR_TEXT_TELEMETRY_CHARS)

    def text_for_log(self) -> str | None:
        """``error_text`` cut to what the on-disk error log carries (P14)."""
        return _cut(self.error_text, ERROR_TEXT_LOG_CHARS)


def _cut(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    return text[:limit]


def provider_message(body: object, fallback: str, *, nested: bool = False) -> str:
    """The provider's own message out of an SDK error body, if it has one.

    ``nested`` selects the shape whose body wraps the error one level
    down (``{"error": {"message": ...}}``, Anthropic) rather than being
    the error object itself (OpenAI, whose client unwraps it before
    constructing the exception). Falls back to the exception's own
    message when the body is not a mapping at all — a 502 from a proxy
    serves HTML, not JSON, and that HTML is still the best answer to
    "what did the provider say".
    """
    if isinstance(body, dict):
        inner = body.get("error") if nested else body
        if isinstance(inner, dict):
            message = inner.get("message")
            if isinstance(message, str) and message:
                return message
    return fallback


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
