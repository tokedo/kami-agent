"""Agent loop: intents, strict serialization, error semantics (SPEC P2, P8, X7, I12).

One session's model-call / tool-execution alternation. The loop is
provider-blind: it speaks only the canonical adapter types. Frozen
strings (kickoff, continuation) are injected by the runner from
``prompts/`` — no prompt text lives in code.

Forced endings (context guard, tool cap, errors) are silent (I4): no
warning message, no final model call.

Before the first model call the loop issues the session-start status
brief (SPEC P1.12): one call to the pinned harness's general
any-operator party-report tool, for the account's own operator, injected
as a normal tool result. It is not a special path — the same tool stays
available for the agent to call itself, and both invocations execute
through ``_execute_intent`` and are telemetered identically, separated
only by ``tool_call.initiator``.
"""

from __future__ import annotations

import threading
import time
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
from kami_agent.tools.receipts import classify_error
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

# Session-start status brief (SPEC P1.12). The tool is the pinned harness's
# general any-operator party report — every kami of one account with its
# full vitals (state, HP current/total/percent, HP rate per hour, cooldown
# seconds, accrual) — not a brief-specific entry point: the agent may call
# it itself with any account, and does so through exactly this name.
# ``account_index`` is the sentinel the tool defines for "the operator this
# harness runs as", passed explicitly rather than left to the tool's
# default so the recorded call says what it asked for.
BRIEF_TOOL = "lens_party"
BRIEF_ARGS: dict[str, Any] = {"account_index": -1}
BRIEF_CALL_ID = "brief_1"

_BACKOFF_BASE_S = 1.0
_BACKOFF_MAX_S = 60.0


@dataclass(frozen=True, slots=True)
class GameToolResult:
    """Result of one harness tool execution.

    ``terminal_state`` is the transaction outcome the harness reported,
    when the result is one (SPEC D1); None for reads and for anything
    that is not a single submitted transaction.
    """

    content: str
    tx_hash: str | None = None
    terminal_state: str | None = None


@runtime_checkable
class GameTools(Protocol):
    """The harness-tool surface the loop needs (implemented in harness.py)."""

    @property
    def tool_defs(self) -> list[ToolDef]: ...

    def execute(self, name: str, args: dict[str, Any]) -> GameToolResult: ...


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
        self._cumulative_usd = cumulative_usd
        self._cumulative_tokens = cumulative_tokens
        self._sleep = sleep

        game_defs = list(game.tool_defs) if game is not None else []
        collisions = {t.name for t in game_defs} & SCAFFOLD_TOOL_NAMES
        if collisions:
            raise ValueError(f"harness tools shadow scaffold tools: {sorted(collisions)}")
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
        """Call the party-report tool once and inject its result verbatim.

        Runs before the first model call, so call 1 already carries the
        account's own kami state instead of spending turns rediscovering
        it. The result enters context as a normal tool result — an
        assistant turn holding the call, then its result — so the model
        sees what it would have seen had it made the call itself. Nothing
        is summarized, reordered, filtered, or annotated: the harness
        envelope is passed through under the same byte cap every tool
        result gets (P2), and the same is true of a failure.

        Degrade visibly, never block (SPEC X21): exactly one attempt, no
        retry and no fallback content. A failure is injected as the error
        result it is, telemetered like any other failed call, and the
        session proceeds — the failure is data, not a reason to abort.

        The brief consumes no ``session_tool_cap``, never advances the
        consecutive-error counter, and never feeds the repetition breaker
        (X20): those caps bound what the agent does. It is skipped
        entirely when the loaded surface does not carry the tool, which
        leaves no telemetry — an absent tool is visible in
        ``run_start.harness_tools`` and in ``tools_hash``.
        """
        if self._game is None or BRIEF_TOOL not in {t.name for t in self._game.tool_defs}:
            return
        intent = ToolCall(id=BRIEF_CALL_ID, name=BRIEF_TOOL, args=dict(BRIEF_ARGS))
        outcome = self._execute_intent(intent)
        messages.append(AssistantMessage(text=None, tool_calls=(intent,)))
        messages.append(
            ToolResultMessage(
                tool_call_id=intent.id,
                content=outcome["content"],
                is_error=not outcome["ok"],
            )
        )
        self._emit_tool_call(
            intent,
            source=outcome["source"],
            duration_ms=outcome["duration_ms"],
            ok=outcome["ok"],
            error=outcome.get("error"),
            truncated=outcome.get("truncated", False),
            original_bytes=outcome.get("original_bytes"),
            tx_hash=outcome.get("tx_hash"),
            terminal_state=outcome.get("terminal_state"),
            initiator=INITIATOR_SCAFFOLD,
        )

    # --- model calls (SPEC P8) ----------------------------------------------

    def _call_model(self, messages: list[Message], continuation: bool) -> AdapterResponse | None:
        attempt = 0
        while True:
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
                )
                if not exc.retryable or attempt >= self._caps.retry_max_attempts:
                    return None
                self._sleep(min(_BACKOFF_MAX_S, _BACKOFF_BASE_S * 2**attempt))
                attempt += 1
                continue
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
        empty_response: bool = False,
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
        }
        if reasoning_tokens is not None:
            fields["reasoning_tokens"] = reasoning_tokens
        if usage_unknown:
            fields["usage_unknown"] = True
        if continuation:
            fields["continuation"] = True
        if empty_response:
            fields["empty_response"] = True
        self._telemetry.emit("llm_call", session=self._session, **fields)

    # --- tool execution (SPEC P2, I12, I16) -------------------------------

    def _execute_batch(self, calls: tuple[ToolCall, ...], messages: list[Message]) -> str | None:
        """Execute intents strictly sequentially, in the order returned.

        Returns the session_end reason if the session must end, else None.
        """
        for index, intent in enumerate(calls):
            if self._scaffold.session_ended:
                # end_session took effect earlier in this batch (I12).
                self._emit_tool_call(
                    intent,
                    source=self._source_of(intent.name),
                    duration_ms=0.0,
                    ok=False,
                    initiator=INITIATOR_MODEL,
                    skipped=True,
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

        content, tx_hash, terminal_state = raw
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
            # Classified on the raw (pre-truncation) content: success-shaped
            # harness results can still carry an on-chain revert.
            "error_or_revert": is_error_or_revert(True, content),
        }

    def _run_with_timeout(self, intent: ToolCall) -> tuple[str, str | None, str | None]:
        """Run one intent in a watchdog thread (tool_timeout_s, P2)."""

        def dispatch() -> tuple[str, str | None, str | None]:
            if intent.name in SCAFFOLD_TOOL_NAMES:
                return self._scaffold.execute(intent.name, intent.args), None, None
            assert self._game is not None  # _source_of guarantees this
            result = self._game.execute(intent.name, intent.args)
            return result.content, result.tx_hash, result.terminal_state

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
            return "scaffold"
        if self._game is not None and name in {t.name for t in self._game.tool_defs}:
            return "harness"
        return "scaffold"  # unknown tools are rejected by the scaffold layer

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
    ) -> None:
        fields: dict[str, Any] = {
            "tool": intent.name,
            "source": source,
            # Provenance, not policy (SPEC P9): "source" names which layer
            # owns the tool, "initiator" names who asked for the call.
            "initiator": initiator,
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
