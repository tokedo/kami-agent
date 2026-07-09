"""Session lifecycle: lock, recover, boundary checks, session, persist, schedule (SPEC §3).

One process: start → run one session → persist → exit. Accounting is
always rebuilt by folding telemetry.jsonl (§7.1); state.json is written
back as a cache. Forced endings and boundary stops are silent to the
agent (D13).

Ordering note vs SPEC §3 step 4: the harness child is spawned *before*
session_start is emitted, because session_start carries ``tools_hash``,
which needs the loaded game tools. The hard constraint — the session
counter is persisted before the first model call, so crashes never reuse
a session number — is preserved.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from kami_agent.adapters.base import (
    AssistantMessage,
    Message,
    ModelAdapter,
    SamplingParams,
    ToolResultMessage,
    UserMessage,
)
from kami_agent.governor import PriceTable, boundary_check, overspend_usd
from kami_agent.harness import HarnessError, tools_hash
from kami_agent.loop import AgentLoop, GameTools, LoopCaps
from kami_agent.state import (
    RUN_COMPLETE,
    crashed_session,
    fold_telemetry,
    save_state,
    session_totals,
)
from kami_agent.supervisor import LOCK_FILENAME, acquire_lock, release_lock
from kami_agent.telemetry import TelemetryWriter, read_events
from kami_agent.tools.scaffold import SCAFFOLD_TOOL_DEFS, ScaffoldTools

# run_session outcomes (operator-facing, never agent-visible)
LOCK_HELD = "lock_held"
NOT_DUE = "not_due"
ALREADY_COMPLETE = "already_complete"
RUN_COMPLETED = "run_complete"
SESSION_RAN = "session_ran"
SESSION_ABORTED = "session_aborted"

TRIGGER_SCHEDULED = "scheduled"
TRIGGER_MANUAL = "manual"


@dataclass
class RunConfig:
    """The §9 parameters the runner needs, pinned per manifest."""

    run_dir: Path
    run_id: str
    model: str
    prices: PriceTable
    caps: LoopCaps
    params: SamplingParams = field(default_factory=lambda: SamplingParams(max_tokens=4096))
    budget_usd: float = 10.0
    t_max_days: float = 30.0
    wake_min_minutes: float = 5.0
    wake_max_minutes: float = 24 * 60.0
    wake_default_minutes: float = 60.0
    workspace_quota_bytes: int = 10 * 1024 * 1024
    lock_stale_s: float = 7200.0


def run_session(
    config: RunConfig,
    adapter: ModelAdapter,
    *,
    harness_factory: Callable[[], GameTools] | None = None,
    trigger: str = TRIGGER_SCHEDULED,
    clock: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] | None = None,
    disable_supervisor: Callable[[], None] | None = None,
) -> str:
    """Execute the full SPEC §3 lifecycle once; returns an outcome constant."""
    clock = clock or (lambda: datetime.now(UTC))
    run_dir = Path(config.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    telemetry_path = run_dir / "telemetry.jsonl"
    state_path = run_dir / "state.json"
    lock_path = run_dir / LOCK_FILENAME

    # 1. Acquire lock (staleness per §2). If held, exit.
    if not acquire_lock(lock_path, stale_s=config.lock_stale_s, clock=clock):
        return LOCK_HELD
    try:
        events = list(read_events(telemetry_path)) if telemetry_path.exists() else []
        state = fold_telemetry(events)

        # §2 wake gating: exit unless due (manual runs bypass).
        if (
            trigger != TRIGGER_MANUAL
            and state.next_wake_at is not None
            and clock() < datetime.fromisoformat(state.next_wake_at)
        ):
            return NOT_DUE

        with TelemetryWriter(telemetry_path, run_id=config.run_id, clock=clock) as writer:
            # 2. Recover: unmatched session_start → synthetic crash end (§3.2).
            crashed = crashed_session(events)
            if crashed is not None:
                record = writer.emit(
                    "session_end",
                    session=crashed,
                    reason="crash",
                    **session_totals(events, crashed),
                )
                events.append(record)
            save_state(state, state_path)  # cache refresh from the fold

            if state.run_status == RUN_COMPLETE:
                return ALREADY_COMPLETE

            # 3. Boundary checks (D13): only here, never mid-session.
            stop_reason = boundary_check(
                cumulative_usd=state.cumulative_usd,
                budget_usd=config.budget_usd,
                first_session_at=state.first_session_at,
                t_max_days=config.t_max_days,
                now=clock(),
            )
            if stop_reason is not None:
                writer.emit(
                    "run_complete",
                    session=state.session_counter,
                    reason=stop_reason,
                    totals={
                        "sessions": state.session_counter,
                        "llm_calls": sum(1 for e in events if e.get("event") == "llm_call"),
                        "cumulative_usd": state.cumulative_usd,
                        "cumulative_tokens": state.cumulative_tokens,
                        "overspend_usd": overspend_usd(state.cumulative_usd, config.budget_usd),
                    },
                )
                state.run_status = RUN_COMPLETE
                save_state(state, state_path)
                if disable_supervisor is not None:
                    disable_supervisor()
                return RUN_COMPLETED

            # 4. Start session: increment and persist the counter before any
            # model call, so crashes never reuse a session number.
            session = state.session_counter + 1
            state.session_counter = session
            save_state(state, state_path)

            return _run_one_session(
                config=config,
                adapter=adapter,
                harness_factory=harness_factory,
                trigger=trigger,
                clock=clock,
                sleep=sleep,
                writer=writer,
                state=state,
                state_path=state_path,
                run_dir=run_dir,
                session=session,
            )
    finally:
        release_lock(lock_path)


def _run_one_session(
    *,
    config: RunConfig,
    adapter: ModelAdapter,
    harness_factory: Callable[[], GameTools] | None,
    trigger: str,
    clock: Callable[[], datetime],
    sleep: Callable[[float], None] | None,
    writer: TelemetryWriter,
    state: Any,
    state_path: Path,
    run_dir: Path,
    session: int,
) -> str:
    scaffold = ScaffoldTools(
        run_dir,
        session_number=session,
        workspace_quota_bytes=config.workspace_quota_bytes,
        wake_min_minutes=config.wake_min_minutes,
        wake_max_minutes=config.wake_max_minutes,
        clock=clock,
        emit=lambda event, fields: writer.emit(event, session=session, **fields),
    )

    def emit_session_start(hash_value: str) -> dict[str, Any]:
        elapsed = 0.0
        if state.first_session_at is not None:
            elapsed = (clock() - datetime.fromisoformat(state.first_session_at)).total_seconds()
        return writer.emit(
            "session_start",
            session=session,
            trigger=trigger,
            budget_remaining_usd=config.budget_usd - state.cumulative_usd,
            wallclock_elapsed_s=elapsed,
            tools_hash=hash_value,
        )

    def emit_schedule(scaffold_tools: ScaffoldTools) -> None:
        # 9. Emitted every session, including the wake_default case (§8).
        if scaffold_tools.clamped_wake_min is not None:
            source = "agent"
            requested: float | None = scaffold_tools.requested_wake_min
            clamped = scaffold_tools.clamped_wake_min
        else:
            source = "default"
            requested = None
            clamped = config.wake_default_minutes
        next_wake_at = (clock() + timedelta(minutes=clamped)).isoformat()
        fields: dict[str, Any] = {
            "source": source,
            "clamped_min": clamped,
            "next_wake_at": next_wake_at,
        }
        if requested is not None:
            fields["requested_min"] = requested
        writer.emit("schedule_next", session=session, **fields)
        state.next_wake_at = next_wake_at

    # Spawn harness child + handshake (see module docstring on ordering).
    game: GameTools | None = None
    try:
        if harness_factory is not None:
            game = harness_factory()
    except HarnessError:
        # §2: handshake failure aborts the session — zero model calls,
        # next wake = wake_default.
        start_record = emit_session_start(tools_hash(list(SCAFFOLD_TOOL_DEFS)))
        if state.first_session_at is None:
            state.first_session_at = start_record["ts"]
        writer.emit(
            "session_end",
            session=session,
            reason="errors",
            llm_calls=0,
            tool_calls=0,
            session_cost_usd=0.0,
            session_tokens=0,
        )
        emit_schedule(scaffold)
        save_state(state, state_path)
        return SESSION_ABORTED

    try:
        game_defs = list(game.tool_defs) if game is not None else []
        start_record = emit_session_start(tools_hash(game_defs + list(SCAFFOLD_TOOL_DEFS)))
        if state.first_session_at is None:
            state.first_session_at = start_record["ts"]

        # 5. Build context: frozen system prompt + the file index (§3.5) —
        # full workspace/ tree, reference/ collapsed to one entry.
        prompts = _load_prompts(run_dir)
        system = prompts["system"] + "\n\n" + scaffold.workspace_list()

        # 6–7. Kickoff + agent loop.
        loop = AgentLoop(
            adapter=adapter,
            model=config.model,
            system=system,
            kickoff_text=prompts["kickoff"],
            continuation_text=prompts["continue"],
            scaffold=scaffold,
            game=game,
            telemetry=writer,
            session=session,
            params=config.params,
            prices=config.prices,
            caps=config.caps,
            cumulative_usd=state.cumulative_usd,
            cumulative_tokens=state.cumulative_tokens,
            **({"sleep": sleep} if sleep is not None else {}),
        )
        result = loop.run()

        # 8. Persist: session_end, transcript, state cache.
        writer.emit(
            "session_end",
            session=session,
            reason=result.reason,
            llm_calls=result.llm_calls,
            tool_calls=result.tool_calls,
            session_cost_usd=result.session_cost_usd,
            session_tokens=result.session_tokens,
        )
        _write_transcript(run_dir, session, result.messages)
        state.cumulative_usd = result.cumulative_usd
        state.cumulative_tokens = result.cumulative_tokens

        # 9. Schedule.
        emit_schedule(scaffold)
        save_state(state, state_path)
        return SESSION_RAN
    finally:
        if game is not None:
            close = getattr(game, "close", None)
            if callable(close):
                close()


def _load_prompts(run_dir: Path) -> dict[str, str]:
    prompts_dir = run_dir / "prompts"
    return {
        "system": (prompts_dir / "system.txt").read_text(encoding="utf-8").rstrip("\n"),
        "kickoff": (prompts_dir / "kickoff.txt").read_text(encoding="utf-8").rstrip("\n"),
        "continue": (prompts_dir / "continue.txt").read_text(encoding="utf-8").rstrip("\n"),
    }


def _write_transcript(run_dir: Path, session: int, messages: list[Message]) -> None:
    """Full message log, one file per session (§7); post-truncation (§8)."""
    transcripts = run_dir / "transcripts"
    transcripts.mkdir(parents=True, exist_ok=True)
    path = transcripts / f"session-{session:04d}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for message in messages:
            f.write(json.dumps(_message_dict(message), ensure_ascii=False) + "\n")


def _message_dict(message: Message) -> dict[str, Any]:
    if isinstance(message, UserMessage):
        return {"role": "user", "text": message.text}
    if isinstance(message, AssistantMessage):
        entry: dict[str, Any] = {
            "role": "assistant",
            "text": message.text,
            "tool_calls": [
                {"id": c.id, "name": c.name, "args": c.args} for c in message.tool_calls
            ],
        }
        if message.provider_state is not None:
            # Transcripts record messages as sent (D22); telemetry never
            # carries provider state.
            entry["provider_state"] = {
                "provider": message.provider_state.provider,
                "payload": _jsonable(message.provider_state.payload),
            }
        return entry
    if isinstance(message, ToolResultMessage):
        return {
            "role": "tool_result",
            "tool_call_id": message.tool_call_id,
            "content": message.content,
            "is_error": message.is_error,
        }
    raise TypeError(f"unknown message type: {message!r}")


def _jsonable(obj: Any) -> Any:
    """Best-effort JSON view of an opaque payload for the transcript."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, (list, tuple)):
        return [_jsonable(item) for item in obj]
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        return dump(mode="json", exclude_none=True)
    return repr(obj)
