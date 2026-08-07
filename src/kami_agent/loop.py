"""Agent loop: intents, strict serialization, error semantics (SPEC P2, P8, X7, I12).

One session's model-call / tool-execution alternation. The loop is
provider-blind: it speaks only the canonical adapter types. Frozen
strings (kickoff, continuation) are injected by the runner from
``prompts/`` — no prompt text lives in code.

Forced endings (context guard, tool cap, errors) are silent (I4): no
warning message, no final model call.

Before the first model call the loop issues the session-start status
brief (SPEC P1.12): one compact roster query to the kami-lens daemon,
made by the scaffold over the daemon's own socket, injected as a tool
result. It **is** a special path (X22): the query is not on the tool
surface, the agent cannot make it, and it runs on its own execution
path rather than through ``_execute_intent``.
"""

from __future__ import annotations

import json
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import jsonschema

from kami_agent.adapters.base import (
    AdapterError,
    AdapterResponse,
    AssistantMessage,
    Message,
    ModelAdapter,
    SamplingParams,
    ToolCall,
    ToolDef,
    ToolResultMessage,
    UserMessage,
)
from kami_agent.governor import PriceTable, cost_usd
from kami_agent.lens import LensError
from kami_agent.repetition import (
    DEFAULT_IDENTICAL_CAP,
    DEFAULT_MIN_DISTINCT,
    DEFAULT_SAME_TOOL_ERROR_CAP,
    DEFAULT_WINDOW,
    RepetitionTracker,
    RepetitionTrip,
    is_error_or_revert,
)
from kami_agent.telemetry import TelemetryWriter
from kami_agent.tools.errors import ToolError
from kami_agent.tools.receipts import classify_error, error_shaped_payload, tx_hash_from_error
from kami_agent.tools.scaffold import SCAFFOLD_TOOL_DEFS, SCAFFOLD_TOOL_NAMES, ScaffoldTools
from kami_agent.tools.truncation import cap_tool_result

# session_end reasons produced by the loop (SPEC P5; "crash" is written by
# recovery, never by a live loop).
REASON_AGENT = "agent"
REASON_TOKEN_CAP = "token_cap"
REASON_TOOL_CAP = "tool_cap"
REASON_ERRORS = "errors"
REASON_REPETITION = "repetition"

# Carried-wake outcomes (SessionResult.carried_wake): _carry_wake runs only
# on the token_cap / tool_cap / repetition paths — never "errors", never
# intents skipped by end_session (I12).
CARRIED_APPLIED = "applied"
CARRIED_INVALID = "invalid"

_FILE_TOOLS = frozenset({"workspace_write", "workspace_read", "workspace_list", "workspace_delete"})

# Who issued a tool call (tool_call.initiator, SPEC P9). "model" is every
# intent the agent returned; "scaffold" is a call the scaffold made on its
# own — currently only the session-start status brief.
INITIATOR_MODEL = "model"
INITIATOR_SCAFFOLD = "scaffold"

# Which layer owns the thing that was called (tool_call.source, SPEC P9).
SOURCE_HARNESS = "harness"
SOURCE_SCAFFOLD = "scaffold"
# The world-state daemon, reached directly by the scaffold. Neither of the
# other two: the harness does not own this call and the scaffold does not
# own the answer.
SOURCE_LENS = "lens"

# Session-start status brief (SPEC P1.12). A compact roster query straight
# to the kami-lens daemon: one line per kami (index, state, [hp, hpTotal])
# plus the room the account is standing in. Full per-kami detail stays on
# the harness's own party report, which the agent can still call itself.
#
# BRIEF_QUERY is the daemon's registry name. BRIEF_TOOL is the name the
# injected call and its telemetry row carry — it is NOT on the tool surface
# and the agent cannot call it (X22). It must not collide with a harness
# tool name, and that is ENFORCED at loop construction rather than assumed
# from the spelling: nothing stops a future harness from registering a
# function of this name, and a pin that did would mean the two layers
# disagree about who serves the roster.
#
# The name deliberately stays inside the [A-Za-z0-9_-] set every provider
# accepts for a function name. A dotted namespace would be collision-proof
# by construction, but the injected assistant turn carries this name to
# three provider APIs, and at least one of them documents that character
# set as a constraint.
#
# No arguments: the daemon prefills the account index of an
# operator-argument query from its own configured default operator when the
# argument list is empty (D7).
BRIEF_TOOL = "lens_roster"
BRIEF_QUERY = "roster"
BRIEF_ARGS: dict[str, Any] = {}
BRIEF_CALL_ID = "brief_1"

_BACKOFF_BASE_S = 1.0
_BACKOFF_MAX_S = 60.0


@dataclass(frozen=True, slots=True)
class GameToolResult:
    """Result of one harness tool execution.

    ``terminal_state`` is the transaction outcome the harness reported,
    when the result is one (SPEC D1); None for reads and for anything
    that is not a single submitted transaction.

    ``txs`` is the per-transaction receipt evidence a multi-transaction
    result carries in band — one entry per hop or per item, each with
    whatever of ``tx_hash`` / ``status`` / ``block`` / ``gas_used`` the
    harness reported. Copied out verbatim for telemetry so a tx-keyed
    reconciliation does not have to parse transcripts; empty whenever the
    payload carried none.
    """

    content: str
    tx_hash: str | None = None
    terminal_state: str | None = None
    txs: tuple[dict[str, Any], ...] = ()


@runtime_checkable
class GameTools(Protocol):
    """The harness-tool surface the loop needs (implemented in harness.py)."""

    @property
    def tool_defs(self) -> list[ToolDef]: ...

    def execute(self, name: str, args: dict[str, Any]) -> GameToolResult: ...


@runtime_checkable
class LensQuery(Protocol):
    """The world-state daemon surface the brief needs (implemented in lens.py)."""

    def query(self, name: str, args: list[Any] | None = None) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class LoopCaps:
    """Per-session caps, pinned per manifest (SPEC D3).

    ``session_token_cap`` has no spec default — it is set per manifest from
    the model list — so it is required here.
    """

    session_token_cap: int
    session_tool_cap: int = 50
    max_consecutive_errors: int = 5
    retry_max_attempts: int = 5
    tool_timeout_s: float = 120.0
    tool_result_max_bytes: int = 65536
    repetition_identical_cap: int = DEFAULT_IDENTICAL_CAP
    repetition_window: int = DEFAULT_WINDOW
    repetition_min_distinct: int = DEFAULT_MIN_DISTINCT
    repetition_same_tool_error_cap: int = DEFAULT_SAME_TOOL_ERROR_CAP


@dataclass
class SessionResult:
    """What the runner needs to emit session_end and update state.

    ``repetition`` names the tripped breaker rule and its telemetry
    fields when ``reason`` is ``repetition``. ``carried_wake`` is
    ``"applied"`` / ``"invalid"`` when a cap-skipped final-turn
    ``set_next_wake`` intent was carried (or discarded as invalid) at
    teardown, else None.
    """

    reason: str
    llm_calls: int
    tool_calls: int
    session_cost_usd: float
    session_tokens: int
    cumulative_usd: float
    cumulative_tokens: int
    messages: list[Message] = field(default_factory=list)
    repetition: RepetitionTrip | None = None
    carried_wake: str | None = None


class AgentLoop:
    """Runs SPEC P1 step 12: alternate model calls and tool executions (P2)."""

    def __init__(
        self,
        *,
        adapter: ModelAdapter,
        model: str,
        system: str,
        kickoff_text: str,
        continuation_text: str,
        scaffold: ScaffoldTools,
        game: GameTools | None,
        telemetry: TelemetryWriter,
        session: int,
        params: SamplingParams,
        prices: PriceTable,
        caps: LoopCaps,
        lens: LensQuery | None = None,
        cumulative_usd: float = 0.0,
        cumulative_tokens: int = 0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._adapter = adapter
        self._model = model
        self._system = system
        self._kickoff_text = kickoff_text
        self._continuation_text = continuation_text
        self._scaffold = scaffold
        self._game = game
        self._telemetry = telemetry
        self._session = session
        self._params = params
        self._prices = prices
        self._caps = caps
        self._lens = lens
        self._cumulative_usd = cumulative_usd
        self._cumulative_tokens = cumulative_tokens
        self._sleep = sleep

        game_defs = list(game.tool_defs) if game is not None else []
        collisions = {t.name for t in game_defs} & SCAFFOLD_TOOL_NAMES
        if collisions:
            raise ValueError(f"harness tools shadow scaffold tools: {sorted(collisions)}")
        # The brief's name is not on the surface, so a harness tool of the
        # same name would make one name mean two things — an injected call
        # the agent cannot make, and a real tool it can. That is a mis-pin,
        # not a runtime condition to absorb: refuse before any model call,
        # exactly as a scaffold collision does.
        if BRIEF_TOOL in {t.name for t in game_defs}:
            raise ValueError(f"harness tool shadows the session-start brief name: {BRIEF_TOOL!r}")
        # Game tools first, scaffold tools second (SPEC P10 order); the order
        # is deterministic so tools_hash is stable.
        self._tool_defs: list[ToolDef] = game_defs + list(SCAFFOLD_TOOL_DEFS)
        self._validators = {
            t.name: jsonschema.Draft202012Validator(t.input_schema) for t in self._tool_defs
        }

        self._llm_calls = 0
        self._tool_events = 0
        self._executed_intents = 0
        self._consecutive_errors = 0
        self._session_cost_usd = 0.0
        self._session_tokens = 0
        self._repetition = RepetitionTracker(
            identical_cap=caps.repetition_identical_cap,
            window=caps.repetition_window,
            min_distinct=caps.repetition_min_distinct,
            same_tool_error_cap=caps.repetition_same_tool_error_cap,
        )
        self._repetition_trip: RepetitionTrip | None = None
        self._carried_wake: str | None = None
        # Scaffold-internal per-call identity (SPEC P9). Telemetry carried no
        # call identity at all before 0.4.0, so two rows of the same tool in
        # one turn could not be told apart, and a provider that reuses a call
        # id could not be distinguished from a genuine repeat without reading
        # the transcript. One monotonic number per EMITTED tool_call row,
        # skipped intents included, makes the row↔intent correspondence 1:1
        # on the face of the stream.
        self._call_seq = 0
        # One monotonic number per model REQUEST, written ahead of the call
        # so a request that is billed but never completed leaves a record.
        self._request_seq = 0

    # --- public --------------------------------------------------------------

    def run(self) -> SessionResult:
        messages: list[Message] = [UserMessage(text=self._kickoff_text)]
        self._inject_brief(messages)
        continuation = False
        while True:
            response = self._call_model(messages, continuation)
            if response is None:
                return self._result(REASON_ERRORS, messages)
            continuation = False
            messages.append(
                AssistantMessage(
                    text="\n\n".join(response.text_blocks) if response.text_blocks else None,
                    tool_calls=response.tool_calls,
                    # Provider reasoning state (I17): copied verbatim for
                    # same-session replay by the emitting adapter; the
                    # loop never inspects it.
                    provider_state=response.provider_state,
                )
            )
            # Context guard (X7): post-call, silent; the response's intents
            # are never executed (a final-turn set_next_wake is carried).
            usage = response.usage
            if usage.input_tokens + usage.output_tokens >= self._caps.session_token_cap:
                self._carry_wake(response.tool_calls)
                return self._result(REASON_TOKEN_CAP, messages)
            if not response.tool_calls:
                # P2: the loop cannot advance on its own — send the frozen
                # continuation string; counts as one error.
                self._consecutive_errors += 1
                if self._consecutive_errors >= self._caps.max_consecutive_errors:
                    return self._result(REASON_ERRORS, messages)
                messages.append(UserMessage(text=self._continuation_text))
                continuation = True
                continue
            reason = self._execute_batch(response.tool_calls, messages)
            if reason is not None:
                return self._result(reason, messages)

    # --- session-start status brief (SPEC P1.12) -----------------------------

    def _inject_brief(self, messages: list[Message]) -> None:
        """Query the daemon's compact roster once and inject it verbatim.

        Runs before the first model call, so call 1 already carries the
        account's own kami state instead of spending turns rediscovering
        it. The answer enters context as a tool result — an assistant
        turn holding the call, then its result — which is the shape a
        model reads as "a read already happened". Nothing is summarized,
        reordered, filtered, or annotated: the daemon envelope is
        serialized compactly and passed through under the same byte cap
        every tool result gets (P2), and the same is true of a failure.

        **This is a special path** (X22), and the previous version's
        claim that it was not is retired. The roster is not a harness
        tool: the scaffold speaks to the daemon itself, on its own
        execution path, and the agent cannot issue this call. What the
        agent keeps is the full per-kami detail on the harness's own
        party report, unchanged.

        Degrade visibly, never block (SPEC X21): exactly one attempt, no
        retry and no fallback content. A failure is injected as the
        minimal machine-shaped error record it is, telemetered, and the
        session proceeds — the failure is data, not a reason to abort.
        A query error carries the daemon's own code and message; a
        transport failure has no daemon text to quote, so its code is the
        scaffold's and its message is the operating system's.

        The brief consumes no ``session_tool_cap``, never advances the
        consecutive-error counter, and never feeds the repetition breaker
        (X20): those caps bound what the agent does. It is skipped
        entirely when no daemon is configured, which leaves no telemetry.
        """
        if self._lens is None:
            return
        intent = ToolCall(id=BRIEF_CALL_ID, name=BRIEF_TOOL, args=dict(BRIEF_ARGS))
        start = time.perf_counter()
        stale: bool | None = None
        block: int | None = None
        try:
            envelope = self._lens.query(BRIEF_QUERY)
        except LensError as exc:
            # The record IS the failure text: no rewording, no advice.
            raw = exc.as_record()
            ok = False
            error: str | None = raw
        else:
            raw = json.dumps(envelope, ensure_ascii=False)
            ok = True
            error = None
            meta = envelope.get("meta")
            if isinstance(meta, dict):
                # Operator-side only (I1): recorded so analysis can see the
                # brief was served from degraded state without reparsing the
                # transcript. Never a separate agent-visible channel — the
                # same values are already inside the injected envelope.
                if isinstance(meta.get("stale"), bool):
                    stale = meta["stale"]
                if isinstance(meta.get("blockNumber"), int):
                    block = meta["blockNumber"]
        duration_ms = (time.perf_counter() - start) * 1000
        capped = cap_tool_result(raw, self._caps.tool_result_max_bytes)
        messages.append(AssistantMessage(text=None, tool_calls=(intent,)))
        messages.append(
            ToolResultMessage(
                tool_call_id=intent.id,
                content=capped.content,
                is_error=not ok,
            )
        )
        self._emit_tool_call(
            intent,
            source=SOURCE_LENS,
            duration_ms=duration_ms,
            ok=ok,
            error=error,
            truncated=capped.truncated,
            original_bytes=capped.original_bytes if capped.truncated else None,
            initiator=INITIATOR_SCAFFOLD,
            lens_stale=stale,
            lens_block=block,
        )

    # --- model calls (SPEC P8) ----------------------------------------------

    def _call_model(self, messages: list[Message], continuation: bool) -> AdapterResponse | None:
        attempt = 0
        while True:
            # Write-ahead (SPEC P9, I6). The provider is billed the instant
            # the request leaves; the llm_call row is written only once it
            # comes back. Everything in between — a process kill, an OOM,
            # the host going away — used to leave a billed call with no
            # record of ANY kind, which is unrecoverable after the fact
            # because nothing local knows the request happened. This row
            # says it happened, before it happens. Recovery pairs it with
            # its llm_call by request_seq and synthesizes a phantom row for
            # any that never got one (P3).
            self._request_seq += 1
            request_seq = self._request_seq
            self._telemetry.emit("llm_request", session=self._session, request_seq=request_seq)
            start = time.perf_counter()
            try:
                response = self._adapter.complete(
                    self._system, list(messages), self._tool_defs, self._params
                )
            except AdapterError as exc:
                latency_ms = (time.perf_counter() - start) * 1000
                # Failed attempt: usage unknowable, logged at cost 0 (P7.4).
                self._llm_calls += 1
                self._emit_llm_call(
                    input_tokens=0,
                    output_tokens=0,
                    reasoning_tokens=None,
                    cache_read_tokens=0,
                    cache_write_tokens=0,
                    cost=0.0,
                    latency_ms=latency_ms,
                    stop_reason="error",
                    retry_count=attempt,
                    usage_unknown=True,
                    continuation=continuation,
                    request_seq=request_seq,
                    provider_request_id=exc.request_id,
                )
                if not exc.retryable or attempt >= self._caps.retry_max_attempts:
                    return None
                self._sleep(min(_BACKOFF_MAX_S, _BACKOFF_BASE_S * 2**attempt))
                attempt += 1
                continue
            except Exception:
                # Anything the adapter did not normalize into an AdapterError
                # — an SDK shape the adapter did not expect, a fault inside
                # response parsing — after the provider has already been
                # billed. Previously this escaped the loop entirely and the
                # session died with NO llm_call row: a billed call invisible
                # to accounting. It is emitted here on the same terms as any
                # other failed attempt (cost 0, usage unknowable) and ends
                # the session as `errors`, which is what a non-retryable
                # model error already means (P5). Deliberately broad: the
                # point is that no exception type can reintroduce the hole.
                latency_ms = (time.perf_counter() - start) * 1000
                self._llm_calls += 1
                self._emit_llm_call(
                    input_tokens=0,
                    output_tokens=0,
                    reasoning_tokens=None,
                    cache_read_tokens=0,
                    cache_write_tokens=0,
                    cost=0.0,
                    latency_ms=latency_ms,
                    stop_reason="error",
                    retry_count=attempt,
                    usage_unknown=True,
                    continuation=continuation,
                    request_seq=request_seq,
                )
                return None
            latency_ms = (time.perf_counter() - start) * 1000
            usage = response.usage
            if (
                not response.text_blocks
                and not response.tool_calls
                and usage.input_tokens == 0
                and usage.output_tokens == 0
            ):
                # Empty response with zero usage: a provider fault, not an
                # assistant turn — retried under the P8 backoff instead of
                # leaking into the continuation/error path. Empty-but-billed
                # responses (nonzero usage) keep the P2 handling.
                self._llm_calls += 1
                self._emit_llm_call(
                    input_tokens=0,
                    output_tokens=0,
                    reasoning_tokens=None,
                    cache_read_tokens=0,
                    cache_write_tokens=0,
                    cost=0.0,
                    latency_ms=latency_ms,
                    stop_reason=response.stop_reason.value,
                    retry_count=attempt,
                    usage_unknown=False,
                    continuation=continuation,
                    empty_response=True,
                    request_seq=request_seq,
                    provider_request_id=response.request_id,
                )
                if attempt >= self._caps.retry_max_attempts:
                    return None
                self._sleep(min(_BACKOFF_MAX_S, _BACKOFF_BASE_S * 2**attempt))
                attempt += 1
                continue
            cost = cost_usd(usage, self._prices)
            tokens = usage.input_tokens + usage.output_tokens
            self._llm_calls += 1
            self._session_cost_usd += cost
            self._session_tokens += tokens
            self._cumulative_usd += cost
            self._cumulative_tokens += tokens
            self._emit_llm_call(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                reasoning_tokens=usage.reasoning_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                cost=cost,
                latency_ms=latency_ms,
                stop_reason=response.stop_reason.value,
                retry_count=attempt,
                usage_unknown=False,
                continuation=continuation,
                request_seq=request_seq,
                provider_request_id=response.request_id,
                cache_write_5m_tokens=usage.cache_write_5m_tokens,
                cache_write_1h_tokens=usage.cache_write_1h_tokens,
            )
            return response

    def _emit_llm_call(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        reasoning_tokens: int | None,
        cache_read_tokens: int,
        cache_write_tokens: int,
        cost: float,
        latency_ms: float,
        stop_reason: str,
        retry_count: int,
        usage_unknown: bool,
        continuation: bool,
        request_seq: int,
        empty_response: bool = False,
        provider_request_id: str | None = None,
        cache_write_5m_tokens: int | None = None,
        cache_write_1h_tokens: int | None = None,
    ) -> None:
        fields: dict[str, Any] = {
            "model": self._model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            # Cache decomposition (SPEC P7.1): input_tokens is the total;
            # these are its components, preserved so per-component provider
            # CSV columns reconcile exactly.
            "cache_read_tokens": cache_read_tokens,
            "cache_write_tokens": cache_write_tokens,
            "cost_usd": cost,
            "cumulative_usd": self._cumulative_usd,
            "cumulative_tokens": self._cumulative_tokens,
            "latency_ms": latency_ms,
            "stop_reason": stop_reason,
            "retry_count": retry_count,
            # Pairs this row with its write-ahead llm_request (P3, P9).
            "request_seq": request_seq,
        }
        if reasoning_tokens is not None:
            fields["reasoning_tokens"] = reasoning_tokens
        if usage_unknown:
            fields["usage_unknown"] = True
        if continuation:
            fields["continuation"] = True
        if empty_response:
            fields["empty_response"] = True
        if provider_request_id is not None:
            fields["provider_request_id"] = provider_request_id
        # Cache-TTL decomposition of cache_write_tokens, where the provider
        # reports one (D2). Absent means the provider serves no split — not
        # that the split is zero.
        if cache_write_5m_tokens is not None:
            fields["cache_write_5m_tokens"] = cache_write_5m_tokens
        if cache_write_1h_tokens is not None:
            fields["cache_write_1h_tokens"] = cache_write_1h_tokens
        self._telemetry.emit("llm_call", session=self._session, **fields)

    # --- tool execution (SPEC P2, I12, I16) -------------------------------

    def _execute_batch(self, calls: tuple[ToolCall, ...], messages: list[Message]) -> str | None:
        """Execute intents strictly sequentially, in the order returned.

        Returns the session_end reason if the session must end, else None.
        """
        # Provider call ids are copied verbatim and are NOT trusted to be
        # unique: the loop routes results positionally and cannot alias
        # them, but anything downstream that joins a result to its call by
        # id can, and a provider that reuses an id inside one turn makes
        # that join silently wrong. Recorded, never enforced — a provider
        # quirk must not end a session (X23).
        counts = Counter(c.id for c in calls)
        duplicates = {call_id for call_id, n in counts.items() if n > 1}
        for index, intent in enumerate(calls):
            duplicate_id = intent.id in duplicates
            if self._scaffold.session_ended:
                # end_session took effect earlier in this batch (I12).
                self._emit_tool_call(
                    intent,
                    source=self._source_of(intent.name),
                    duration_ms=0.0,
                    ok=False,
                    initiator=INITIATOR_MODEL,
                    skipped=True,
                    provider_call_id=intent.id,
                    provider_call_id_duplicate=duplicate_id,
                )
                continue
            outcome = self._execute_intent(intent)
            messages.append(
                ToolResultMessage(
                    tool_call_id=intent.id,
                    content=outcome["content"],
                    is_error=not outcome["ok"],
                )
            )
            self._executed_intents += 1
            self._emit_tool_call(
                intent,
                source=outcome["source"],
                duration_ms=outcome["duration_ms"],
                ok=outcome["ok"],
                initiator=INITIATOR_MODEL,
                error=outcome.get("error"),
                truncated=outcome.get("truncated", False),
                original_bytes=outcome.get("original_bytes"),
                tx_hash=outcome.get("tx_hash"),
                terminal_state=outcome.get("terminal_state"),
                txs=outcome.get("txs") or (),
                error_shaped=outcome.get("error_shaped", False),
                provider_call_id=intent.id,
                provider_call_id_duplicate=duplicate_id,
            )
            if outcome["ok"]:
                self._consecutive_errors = 0
            else:
                self._consecutive_errors += 1
                if self._consecutive_errors >= self._caps.max_consecutive_errors:
                    return REASON_ERRORS
            if not self._scaffold.session_ended:
                # Repetition breaker: evaluated after every executed call,
                # ends the session exactly as tool_cap does (silent, I4).
                trip = self._repetition.record(
                    intent.name,
                    intent.args,
                    error_or_revert=outcome["error_or_revert"],
                )
                if trip is not None:
                    self._repetition_trip = trip
                    self._carry_wake(calls[index + 1 :])
                    return REASON_REPETITION
                if self._executed_intents >= self._caps.session_tool_cap:
                    self._carry_wake(calls[index + 1 :])
                    return REASON_TOOL_CAP
        return REASON_AGENT if self._scaffold.session_ended else None

    def _execute_intent(self, intent: ToolCall) -> dict[str, Any]:
        source = self._source_of(intent.name)
        start = time.perf_counter()

        def failure(message: str, *, from_harness: bool = False) -> dict[str, Any]:
            # The message reaches the model exactly as produced (P2): the
            # only transformation is the byte cap every result gets. The
            # harness's transaction-outcome classification is recorded
            # alongside it for telemetry, never folded into the content.
            capped = cap_tool_result(message, self._caps.tool_result_max_bytes)
            return {
                "content": capped.content,
                "ok": False,
                "error": message,
                "source": source,
                "duration_ms": (time.perf_counter() - start) * 1000,
                "truncated": capped.truncated,
                "original_bytes": capped.original_bytes if capped.truncated else None,
                "terminal_state": classify_error(message) if from_harness else None,
                # A raised terminal state names its transaction in prose and
                # nowhere else, so the hash used to survive only inside the
                # error text — the one place P9 tells readers not to parse.
                # Lifted onto the field the reverted and unconfirmed rows
                # were always missing.
                "tx_hash": tx_hash_from_error(message) if from_harness else None,
                "error_or_revert": True,
            }

        validator = self._validators.get(intent.name)
        if validator is None:
            # Malformed tool call (P2): unknown tool.
            return failure(f"unknown tool: {intent.name}")
        schema_errors = sorted(validator.iter_errors(intent.args), key=str)
        if schema_errors:
            # Malformed tool call (P2): args failing schema validation.
            return failure(f"invalid arguments for {intent.name}: {schema_errors[0].message}")

        try:
            raw = self._run_with_timeout(intent)
        except _ToolTimeout:
            return failure(f"tool call timed out after {self._caps.tool_timeout_s:g} seconds")
        except ToolError as exc:
            # A harness ToolError may be a raised transaction outcome
            # (SPEC D1); a scaffold one never is.
            return failure(str(exc), from_harness=source == "harness")
        except Exception as exc:  # harness/executor failure (P2)
            return failure(f"tool execution failed: {exc}")

        content, tx_hash, terminal_state, txs = raw
        # Slice-hint only where re-readable via workspace_read (I16).
        reread_path = intent.args.get("path") if intent.name == "workspace_read" else None
        capped = cap_tool_result(
            content,
            self._caps.tool_result_max_bytes,
            path=reread_path if isinstance(reread_path, str) else None,
        )
        return {
            "content": capped.content,
            "ok": True,
            "source": source,
            "duration_ms": (time.perf_counter() - start) * 1000,
            "truncated": capped.truncated,
            "original_bytes": capped.original_bytes if capped.truncated else None,
            "tx_hash": tx_hash,
            "terminal_state": terminal_state,
            "txs": txs,
            # Classified on the raw (pre-truncation) content: success-shaped
            # harness results can still carry an on-chain revert.
            "error_or_revert": is_error_or_revert(True, content),
            # A tool that RETURNS a payload whose body is an error rather
            # than raising it. ok stays exception-keyed and true — that is
            # the contract, and moving it would silently rewrite four
            # versions of accounting — but the shape is now visible on its
            # own field instead of only to a reader who parses the payload.
            "error_shaped": error_shaped_payload(content),
        }

    def _run_with_timeout(
        self, intent: ToolCall
    ) -> tuple[str, str | None, str | None, tuple[dict[str, Any], ...]]:
        """Run one intent in a watchdog thread (tool_timeout_s, P2)."""

        def dispatch() -> tuple[str, str | None, str | None, tuple[dict[str, Any], ...]]:
            if intent.name in SCAFFOLD_TOOL_NAMES:
                return self._scaffold.execute(intent.name, intent.args), None, None, ()
            assert self._game is not None  # _source_of guarantees this
            result = self._game.execute(intent.name, intent.args)
            return result.content, result.tx_hash, result.terminal_state, result.txs

        box: list[tuple[str, Any]] = []

        def target() -> None:
            try:
                box.append(("ok", dispatch()))
            except BaseException as exc:  # noqa: BLE001 - re-raised in the caller
                box.append(("err", exc))

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        thread.join(self._caps.tool_timeout_s)
        if not box:
            raise _ToolTimeout
        kind, value = box[0]
        if kind == "err":
            raise value
        return value

    def _source_of(self, name: str) -> str:
        if name in SCAFFOLD_TOOL_NAMES:
            return SOURCE_SCAFFOLD
        if self._game is not None and name in {t.name for t in self._game.tool_defs}:
            return SOURCE_HARNESS
        # Unknown tools are rejected by the scaffold layer (X15). This
        # includes the brief's own name if the agent tries to call it: the
        # roster is not on the surface, so the attempt fails like any other
        # unknown tool and the failure is data (X22).
        return SOURCE_SCAFFOLD

    def _emit_tool_call(
        self,
        intent: ToolCall,
        *,
        source: str,
        duration_ms: float,
        ok: bool,
        initiator: str,
        error: str | None = None,
        skipped: bool = False,
        truncated: bool = False,
        original_bytes: int | None = None,
        tx_hash: str | None = None,
        terminal_state: str | None = None,
        txs: tuple[dict[str, Any], ...] = (),
        error_shaped: bool = False,
        provider_call_id: str | None = None,
        provider_call_id_duplicate: bool = False,
        lens_stale: bool | None = None,
        lens_block: int | None = None,
    ) -> None:
        self._call_seq += 1
        fields: dict[str, Any] = {
            "tool": intent.name,
            "source": source,
            # Provenance, not policy (SPEC P9): "source" names which layer
            # owns the tool, "initiator" names who asked for the call.
            "initiator": initiator,
            # Scaffold-minted, session-monotonic, one per emitted row. The
            # stream's own identity for this call, owned by no provider.
            "call_seq": self._call_seq,
            "duration_ms": duration_ms,
            "ok": ok,
        }
        path = intent.args.get("path") if intent.name in _FILE_TOOLS else None
        if isinstance(path, str):
            fields["path"] = path
        if error is not None:
            fields["error"] = error
        if skipped:
            fields["skipped"] = True
        if truncated:
            fields["truncated"] = True
        if original_bytes is not None:
            fields["original_bytes"] = original_bytes
        if tx_hash is not None:
            fields["tx_hash"] = tx_hash
        if terminal_state is not None:
            fields["tx_terminal_state"] = terminal_state
        if txs:
            fields["txs"] = list(txs)
        if error_shaped:
            fields["result_error_shaped"] = True
        if provider_call_id is not None:
            fields["provider_call_id"] = provider_call_id
        if provider_call_id_duplicate:
            fields["provider_call_id_duplicate"] = True
        if lens_stale is not None:
            fields["lens_stale"] = lens_stale
        if lens_block is not None:
            fields["lens_block"] = lens_block
        self._tool_events += 1
        self._telemetry.emit("tool_call", session=self._session, **fields)

    # --- carried set_next_wake ---------------------------------------------------

    def _carry_wake(self, unexecuted: tuple[ToolCall, ...]) -> None:
        """Execute the ONE cap-skipped final-turn set_next_wake intent.

        Called only on the token_cap / tool_cap / repetition paths, with
        the intents the cap prevented from executing. The last set_next_wake among
        them wins (normal last-call-wins semantics), validated and
        clamped exactly as a normal call; invalid args are discarded with
        the discard recorded (``carried_wake = "invalid"``), leaving any
        previously executed wake state untouched. No other skipped intent
        is ever executed. No tool_call event, no tool result message: the
        agent never observes the carried execution.
        """
        candidates = [c for c in unexecuted if c.name == "set_next_wake"]
        if not candidates:
            return
        intent = candidates[-1]
        validator = self._validators["set_next_wake"]
        if any(validator.iter_errors(intent.args)):
            self._carried_wake = CARRIED_INVALID
            return
        try:
            self._scaffold.execute("set_next_wake", intent.args)
        except ToolError:
            self._carried_wake = CARRIED_INVALID
            return
        self._carried_wake = CARRIED_APPLIED

    # --- result ----------------------------------------------------------------

    def _result(self, reason: str, messages: list[Message]) -> SessionResult:
        return SessionResult(
            reason=reason,
            llm_calls=self._llm_calls,
            tool_calls=self._tool_events,
            session_cost_usd=self._session_cost_usd,
            session_tokens=self._session_tokens,
            cumulative_usd=self._cumulative_usd,
            cumulative_tokens=self._cumulative_tokens,
            messages=messages,
            repetition=self._repetition_trip,
            carried_wake=self._carried_wake,
        )


class _ToolTimeout(Exception):
    """Internal: a tool exceeded tool_timeout_s."""
