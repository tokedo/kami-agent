"""Agent loop: intents, strict serialization, error semantics (SPEC §5.3–5.5, D17, D18).

One session's model-call / tool-execution alternation. The loop is
provider-blind: it speaks only the canonical adapter types. Frozen
strings (kickoff, continuation) are injected by the runner from
``prompts/`` — no prompt text lives in code.

Forced endings (context guard, tool cap, errors) are silent (D13): no
warning message, no final model call.
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
from kami_agent.telemetry import TelemetryWriter
from kami_agent.tools.errors import ToolError
from kami_agent.tools.scaffold import SCAFFOLD_TOOL_DEFS, SCAFFOLD_TOOL_NAMES, ScaffoldTools
from kami_agent.tools.truncation import cap_tool_result

# session_end reasons produced by the loop (SPEC §8; "crash" is written by
# recovery, never by a live loop).
REASON_AGENT = "agent"
REASON_TOKEN_CAP = "token_cap"
REASON_TOOL_CAP = "tool_cap"
REASON_ERRORS = "errors"

_FILE_TOOLS = frozenset({"workspace_write", "workspace_read", "workspace_list", "workspace_delete"})

_BACKOFF_BASE_S = 1.0
_BACKOFF_MAX_S = 60.0


@dataclass(frozen=True, slots=True)
class GameToolResult:
    """Result of one harness tool execution."""

    content: str
    tx_hash: str | None = None


@runtime_checkable
class GameTools(Protocol):
    """The harness-tool surface the loop needs (implemented in harness.py)."""

    @property
    def tool_defs(self) -> list[ToolDef]: ...

    def execute(self, name: str, args: dict[str, Any]) -> GameToolResult: ...


@dataclass(frozen=True, slots=True)
class LoopCaps:
    """Per-session caps, pinned per manifest (SPEC §9).

    ``session_token_cap`` has no spec default — it is set per manifest from
    the model list (D17) — so it is required here.
    """

    session_token_cap: int
    session_tool_cap: int = 50
    max_consecutive_errors: int = 5
    retry_max_attempts: int = 5
    tool_timeout_s: float = 120.0
    tool_result_max_bytes: int = 65536


@dataclass
class SessionResult:
    """What the runner needs to emit session_end and update state."""

    reason: str
    llm_calls: int
    tool_calls: int
    session_cost_usd: float
    session_tokens: int
    cumulative_usd: float
    cumulative_tokens: int
    messages: list[Message] = field(default_factory=list)


class AgentLoop:
    """Runs SPEC §3 step 7: alternate model calls and tool executions."""

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
        # Game tools first, scaffold tools second (SPEC §6 order); the order
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

    # --- public --------------------------------------------------------------

    def run(self) -> SessionResult:
        messages: list[Message] = [UserMessage(text=self._kickoff_text)]
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
                    # D22: copied verbatim for same-session replay by the
                    # emitting adapter; the loop never inspects it.
                    provider_state=response.provider_state,
                )
            )
            # Context guard (D17): post-call, silent; the response's intents
            # are never executed.
            usage = response.usage
            if usage.input_tokens + usage.output_tokens >= self._caps.session_token_cap:
                return self._result(REASON_TOKEN_CAP, messages)
            if not response.tool_calls:
                # §5.4: the loop cannot advance on its own — send the frozen
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

    # --- model calls (SPEC §5.5) ----------------------------------------------

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
                # Failed attempt: usage unknowable, logged at cost 0 (§5.5).
                self._llm_calls += 1
                self._emit_llm_call(
                    input_tokens=0,
                    output_tokens=0,
                    reasoning_tokens=None,
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
        cost: float,
        latency_ms: float,
        stop_reason: str,
        retry_count: int,
        usage_unknown: bool,
        continuation: bool,
    ) -> None:
        fields: dict[str, Any] = {
            "model": self._model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
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
        self._telemetry.emit("llm_call", session=self._session, **fields)

    # --- tool execution (SPEC §5.3–5.4, D18, D19) -------------------------------

    def _execute_batch(self, calls: tuple[ToolCall, ...], messages: list[Message]) -> str | None:
        """Execute intents strictly sequentially, in the order returned.

        Returns the session_end reason if the session must end, else None.
        """
        for intent in calls:
            if self._scaffold.session_ended:
                # end_session took effect earlier in this batch (D18).
                self._emit_tool_call(
                    intent,
                    source=self._source_of(intent.name),
                    duration_ms=0.0,
                    ok=False,
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
                error=outcome.get("error"),
                truncated=outcome.get("truncated", False),
                original_bytes=outcome.get("original_bytes"),
                tx_hash=outcome.get("tx_hash"),
            )
            if outcome["ok"]:
                self._consecutive_errors = 0
            else:
                self._consecutive_errors += 1
                if self._consecutive_errors >= self._caps.max_consecutive_errors:
                    return REASON_ERRORS
            if (
                not self._scaffold.session_ended
                and self._executed_intents >= self._caps.session_tool_cap
            ):
                return REASON_TOOL_CAP
        return REASON_AGENT if self._scaffold.session_ended else None

    def _execute_intent(self, intent: ToolCall) -> dict[str, Any]:
        source = self._source_of(intent.name)
        start = time.perf_counter()

        def failure(message: str) -> dict[str, Any]:
            capped = cap_tool_result(message, self._caps.tool_result_max_bytes)
            return {
                "content": capped.content,
                "ok": False,
                "error": message,
                "source": source,
                "duration_ms": (time.perf_counter() - start) * 1000,
                "truncated": capped.truncated,
                "original_bytes": capped.original_bytes if capped.truncated else None,
            }

        validator = self._validators.get(intent.name)
        if validator is None:
            # Malformed tool call (§5.4): unknown tool.
            return failure(f"unknown tool: {intent.name}")
        schema_errors = sorted(validator.iter_errors(intent.args), key=str)
        if schema_errors:
            # Malformed tool call (§5.4): args failing schema validation.
            return failure(f"invalid arguments for {intent.name}: {schema_errors[0].message}")

        try:
            raw = self._run_with_timeout(intent)
        except _ToolTimeout:
            return failure(f"tool call timed out after {self._caps.tool_timeout_s:g} seconds")
        except ToolError as exc:
            return failure(str(exc))
        except Exception as exc:  # harness/executor failure (§5.4)
            return failure(f"tool execution failed: {exc}")

        content, tx_hash = raw
        # Slice-hint only where re-readable via workspace_read (D19).
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
        }

    def _run_with_timeout(self, intent: ToolCall) -> tuple[str, str | None]:
        """Run one intent in a watchdog thread (tool_timeout_s, §5.4)."""

        def dispatch() -> tuple[str, str | None]:
            if intent.name in SCAFFOLD_TOOL_NAMES:
                return self._scaffold.execute(intent.name, intent.args), None
            assert self._game is not None  # _source_of guarantees this
            result = self._game.execute(intent.name, intent.args)
            return result.content, result.tx_hash

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
        error: str | None = None,
        skipped: bool = False,
        truncated: bool = False,
        original_bytes: int | None = None,
        tx_hash: str | None = None,
    ) -> None:
        fields: dict[str, Any] = {
            "tool": intent.name,
            "source": source,
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
        self._tool_events += 1
        self._telemetry.emit("tool_call", session=self._session, **fields)

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
        )


class _ToolTimeout(Exception):
    """Internal: a tool exceeded tool_timeout_s."""
