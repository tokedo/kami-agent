"""Session lifecycle: lock, recover, boundary checks, session, persist, schedule (SPEC P1).

One process: start → run one session → persist → exit. Accounting is
always rebuilt by folding telemetry.jsonl (P3); state.json is written
back as a cache. Forced endings and boundary stops are silent to the
agent (I4, I2).

Ordering note (SPEC X11): the harness child is spawned *before*
session_start is emitted, because session_start carries ``tools_hash``,
which needs the loaded game tools. The hard constraint — the session
counter is persisted before the first model call (P1.7), so crashes never
reuse a session number — is preserved.

The profile's prompt assets are read before that spawn (P1, P13): they
are a pure filesystem read, and a profile whose pinned asset is missing
from the run directory is a mis-provisioned arm, which must fail loudly
and leave no half-started session behind. The same is true of a harness
that states its standing text only in the handshake and did not deliver
it (D1): it is refused right after the spawn, before ``session_start``,
leaving one ``session_refused`` line and consuming no session number.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from kami_agent import journal
from kami_agent.adapters.base import (
    AssistantMessage,
    Message,
    ModelAdapter,
    SamplingParams,
    ToolResultMessage,
    UserMessage,
)
from kami_agent.errorlog import ERRORS_FILENAME, ErrorLog
from kami_agent.governor import PriceTable, boundary_check, overspend_usd
from kami_agent.harness import HarnessError, HarnessPairingError, check_pairing, tools_hash
from kami_agent.lens import STATUS_QUERY, LensQuery
from kami_agent.loop import (
    CARRIED_APPLIED,
    CARRIED_INVALID,
    AgentLoop,
    GameTools,
    LoopCaps,
)
from kami_agent.state import (
    RUN_COMPLETE,
    crashed_session,
    fold_telemetry,
    phantom_requests,
    save_state,
    session_totals,
)
from kami_agent.supervisor import LOCK_FILENAME, acquire_lock, release_lock
from kami_agent.telemetry import TelemetryWriter, read_events
from kami_agent.tools.scaffold import (
    DEFAULT_PROFILE,
    PROFILE_ORIENTATION,
    PROFILE_PLANNING,
    ScaffoldTools,
    profile_at_least,
)

# run_session outcomes (operator-facing, never agent-visible)
LOCK_HELD = "lock_held"
NOT_DUE = "not_due"
ALREADY_COMPLETE = "already_complete"
RUN_COMPLETED = "run_complete"
SESSION_RAN = "session_ran"
SESSION_ABORTED = "session_aborted"

TRIGGER_SCHEDULED = "scheduled"
TRIGGER_MANUAL = "manual"

# session_refused reasons (SPEC P9; closed enum in the schema).
REFUSED_STANDING_TEXT_MISSING = "standing_text_missing"

_BARE_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass
class RunConfig:
    """The D3 parameters the runner needs, pinned per manifest."""

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
    # The harness presentation mode this run pins, recorded on every
    # session_start so analysis can key on it without reading the config
    # copy (SPEC D1). The scaffold passes the manifest value through and
    # validates nothing: an unsupported mode must fail at the harness,
    # loudly, not be normalized here. None = the manifest pinned no mode
    # and the harness applied its own default, which is recorded as
    # absence rather than guessed at.
    presentation_mode: str | None = None
    # Which rung of the knowledge-delivery ladder this run's scaffold is
    # (SPEC D3, P10, P13): it selects the tool surface, the prompt
    # appendices, and the plan-file injection, and it is recorded on every
    # session_start. `control` is the 0.4.0 scaffold plus gas visibility.
    scaffold_profile: str = DEFAULT_PROFILE


def run_session(
    config: RunConfig,
    adapter: ModelAdapter,
    *,
    harness_factory: Callable[[], GameTools] | None = None,
    lens_factory: Callable[[], LensQuery] | None = None,
    trigger: str = TRIGGER_SCHEDULED,
    clock: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] | None = None,
    disable_supervisor: Callable[[], None] | None = None,
) -> str:
    """Execute the full SPEC P1 lifecycle once; returns an outcome constant."""
    clock = clock or (lambda: datetime.now(UTC))
    run_dir = Path(config.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    telemetry_path = run_dir / "telemetry.jsonl"
    state_path = run_dir / "state.json"
    lock_path = run_dir / LOCK_FILENAME

    # 1. Acquire lock (staleness per P4). If held, exit.
    if not acquire_lock(lock_path, stale_s=config.lock_stale_s, clock=clock):
        return LOCK_HELD
    try:
        events = list(read_events(telemetry_path)) if telemetry_path.exists() else []
        state = fold_telemetry(events)

        # P1.3 wake gating: exit unless due (manual runs bypass).
        if (
            trigger != TRIGGER_MANUAL
            and state.next_wake_at is not None
            and clock() < datetime.fromisoformat(state.next_wake_at)
        ):
            return NOT_DUE

        error_log = ErrorLog(run_dir / ERRORS_FILENAME, run_id=config.run_id, clock=clock)
        telemetry = TelemetryWriter(telemetry_path, run_id=config.run_id, clock=clock)
        with telemetry as writer, error_log:
            # 2. Recover: unmatched session_start → synthetic crash end (P3).
            crashed = crashed_session(events)
            if crashed is not None:
                # Phantom model requests first, so the crash session_end
                # totals below count them. A request written ahead but never
                # completed may have been billed; its usage is unknowable, so
                # it is recorded on exactly the terms any other
                # failed-but-billed attempt is (cost 0, usage_unknown) and
                # flagged as synthetic. Idempotent: the row it writes carries
                # the same request_seq, so a second pass finds it completed.
                for seq in phantom_requests(events, crashed):
                    events.append(
                        writer.emit(
                            "llm_call",
                            session=crashed,
                            model=config.model,
                            input_tokens=0,
                            output_tokens=0,
                            cache_read_tokens=0,
                            cache_write_tokens=0,
                            cost_usd=0.0,
                            cumulative_usd=state.cumulative_usd,
                            cumulative_tokens=state.cumulative_tokens,
                            latency_ms=0.0,
                            stop_reason="error",
                            retry_count=0,
                            usage_unknown=True,
                            request_seq=seq,
                            phantom=True,
                        )
                    )
                record = writer.emit(
                    "session_end",
                    session=crashed,
                    reason="crash",
                    **session_totals(events, crashed),
                )
                events.append(record)
                # A crashed session wrote no journal entry of its own — the
                # process died before session end. Without this the elapsed
                # time the NEXT entry reports would silently span two
                # sessions, which is the one number the journal exists to
                # make honest (P15). Written from the folded stream, so it
                # carries what telemetry knows and nothing it does not: the
                # roster is absent, because the brief's answer lived only
                # in a transcript that was never written.
                _journal_crashed_session(run_dir, events, crashed, config.caps.journal_max_bytes)
            save_state(state, state_path)  # cache refresh from the fold

            if state.run_status == RUN_COMPLETE:
                return ALREADY_COMPLETE

            # 3. Boundary checks (I2): only here, never mid-session.
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
                error_log=error_log,
                harness_factory=harness_factory,
                lens_factory=lens_factory,
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
    error_log: ErrorLog,
    harness_factory: Callable[[], GameTools] | None,
    lens_factory: Callable[[], LensQuery] | None,
    trigger: str,
    clock: Callable[[], datetime],
    sleep: Callable[[float], None] | None,
    writer: TelemetryWriter,
    state: Any,
    state_path: Path,
    run_dir: Path,
    session: int,
) -> str:
    # Read before the harness child is spawned and before any telemetry:
    # a profile whose pinned asset is missing dies here, with a named
    # cause, leaving no unmatched session_start for recovery to close.
    prompts = _load_prompts(run_dir, config.scaffold_profile)

    scaffold = ScaffoldTools(
        run_dir,
        session_number=session,
        profile=config.scaffold_profile,
        workspace_quota_bytes=config.workspace_quota_bytes,
        wake_min_minutes=config.wake_min_minutes,
        wake_max_minutes=config.wake_max_minutes,
        wait_max_seconds=config.caps.wait_max_seconds,
        clock=clock,
        emit=lambda event, fields: writer.emit(event, session=session, **fields),
    )

    def emit_session_start(
        hash_value: str,
        published: str | None = None,
        lens_provenance: dict[str, Any] | None = None,
        harness_provenance: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        elapsed = 0.0
        if state.first_session_at is not None:
            elapsed = (clock() - datetime.fromisoformat(state.first_session_at)).total_seconds()
        fields: dict[str, Any] = {
            "trigger": trigger,
            "budget_remaining_usd": config.budget_usd - state.cumulative_usd,
            "wallclock_elapsed_s": elapsed,
            "tools_hash": hash_value,
            # The rung this arm is. Recorded on every session_start because
            # it is what the family varies, and because tools_hash above
            # differs BY DESIGN between profiles that add a tool.
            "scaffold_profile": config.scaffold_profile,
        }
        if config.presentation_mode is not None:
            fields["presentation_mode"] = config.presentation_mode
        # The harness's hash of its OWN registry, as published in the
        # handshake. Recorded next to ours, never against it: the two are
        # different by construction (D1).
        if published is not None:
            fields["harness_tools_hash"] = published
        # The contract version the harness stated, and the fingerprint of
        # the standing text it sent — which this session put in the
        # system prompt verbatim (D1). Part of the environment definition,
        # pinned with the harness, so it is recorded beside its hash.
        if harness_provenance:
            fields.update(harness_provenance)
        # Which daemon actually served this session (D7). Operator-side
        # only: session_start is not an agent-visible channel (P9, I1).
        if lens_provenance:
            fields.update(lens_provenance)
        return writer.emit("session_start", session=session, **fields)

    def emit_schedule(scaffold_tools: ScaffoldTools, carried_wake: str | None = None) -> None:
        # 9. Emitted every session, including the wake_default case (I15).
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
        # Carried set_next_wake: the applied value came from a cap-skipped
        # final-turn intent; an invalid one was discarded (the discard is
        # recorded, prior wake state — if any — stands).
        if carried_wake == CARRIED_APPLIED:
            fields["carried"] = True
        elif carried_wake == CARRIED_INVALID:
            fields["carried_invalid"] = True
        writer.emit("schedule_next", session=session, **fields)
        state.next_wake_at = next_wake_at

    # Spawn harness child + handshake (see module docstring on ordering).
    game: GameTools | None = None
    try:
        if harness_factory is not None:
            game = harness_factory()
    except HarnessError:
        # D1: handshake failure aborts the session — zero model calls,
        # next wake = wake_default.
        start_record = emit_session_start(tools_hash(list(scaffold.tool_defs)))
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

    # The world-state daemon the session-start brief reads (D7). Constructed
    # after the harness on purpose: a lens client opens no connection until
    # it is queried, so it cannot fail here and can never abort a session —
    # an unreachable daemon is discovered by the brief and degrades there.
    lens = lens_factory() if lens_factory is not None else None

    # Read before the session runs: this session appends its own entry at
    # the end, and reading afterwards would measure the gap from itself.
    previous_journal_end = journal.last_ended_at(run_dir)

    try:
        # The harness's standing text (D1): the rules it states once, in
        # the handshake, for its whole surface — from 4.0.0 including the
        # rule that tool output is untrusted data, not instructions, which
        # no tool description carries any more. A harness that owes that
        # text and did not deliver it is refused here, before session_start
        # and any model call, rather than run with the rule shown nowhere.
        standing_text = _standing_text_of(game)
        schema_version = _schema_version_of(game)
        try:
            check_pairing(schema_version, standing_text)
        except HarnessPairingError as exc:
            # Visible, and not a session. One `session_refused` line per
            # refused attempt, so a scheduler polling a misconfigured
            # deployment produces a readable stream of refusals instead of
            # a silent sterile loop. It carries the LAST session number the
            # run used, not the one claimed for this attempt: the counter is
            # folded as max(session) over the stream (P3), so this attempt
            # consumes no number, and the cache claimed above is given back
            # so `status` agrees with the stream.
            refused_fields: dict[str, Any] = {
                "reason": REFUSED_STANDING_TEXT_MISSING,
                "message": str(exc),
                "trigger": trigger,
            }
            if schema_version is not None:
                refused_fields["harness_schema_version"] = schema_version
            published = getattr(game, "harness_tools_hash", None)
            # Shape-checked here, unlike on session_start: a value the schema
            # would reject must not turn the refusal into a validation error.
            if isinstance(published, str) and _BARE_SHA256.fullmatch(published):
                refused_fields["harness_tools_hash"] = published
            writer.emit("session_refused", session=session - 1, **refused_fields)
            state.session_counter = session - 1
            save_state(state, state_path)
            raise

        game_defs = list(game.tool_defs) if game is not None else []
        start_record = emit_session_start(
            tools_hash(game_defs + list(scaffold.tool_defs)),
            getattr(game, "harness_tools_hash", None),
            _lens_provenance(lens),
            _harness_provenance(schema_version, standing_text),
        )
        if state.first_session_at is None:
            state.first_session_at = start_record["ts"]

        # 5. Build context: frozen system prompt + this profile's pinned
        # appendices + the harness's standing text, verbatim, when it sent
        # any + the file index (P1.11) — full workspace/ tree, reference/
        # collapsed to one entry. The standing text sits after the
        # scaffold's own frozen text and before the one part that changes
        # between sessions, so everything fixed for the run is one prefix.
        system = "\n\n".join(
            [
                *prompts["system_parts"],
                *([standing_text] if standing_text else []),
                scaffold.workspace_list(),
            ]
        )

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
            error_log=error_log,
            **({"sleep": sleep} if sleep is not None else {}),
        )
        result = loop.run()

        # 8. Persist: session_end, transcript, state cache. A repetition
        # ending carries its rule and trigger stats (SPEC P9, additive).
        end_fields: dict[str, Any] = {
            "reason": result.reason,
            "llm_calls": result.llm_calls,
            "tool_calls": result.tool_calls,
            "session_cost_usd": result.session_cost_usd,
            "session_tokens": result.session_tokens,
        }
        if result.repetition is not None:
            end_fields["repetition_rule"] = result.repetition.rule
            end_fields.update(result.repetition.fields)
        end_record = writer.emit("session_end", session=session, **end_fields)
        # The session's own record, written whatever the model wrote for
        # itself (P15). AFTER session_end so the entry's ended_at is the
        # stream's own timestamp for the ending rather than a second
        # reading of the clock, and BEFORE the transcript so an entry
        # exists even if writing the transcript fails.
        #
        # `previous_ended_at` is read from the journal, not from the
        # in-memory session: the whole point of the elapsed figure is that
        # it survives a process that was not running in between.
        journal.append(
            run_dir,
            journal.build_entry(
                session=session,
                started_at=start_record["ts"],
                ended_at=end_record["ts"],
                previous_ended_at=previous_journal_end,
                tools=result.journal_tools,
                tx_hashes=result.journal_tx_hashes,
                roster=result.journal_roster,
            ),
            max_bytes=config.caps.journal_max_bytes,
        )
        _write_transcript(run_dir, session, result.messages)
        state.cumulative_usd = result.cumulative_usd
        state.cumulative_tokens = result.cumulative_tokens

        # 9. Schedule.
        emit_schedule(scaffold, result.carried_wake)
        save_state(state, state_path)
        return SESSION_RAN
    finally:
        for component in (game, lens):
            if component is None:
                continue
            close = getattr(component, "close", None)
            if callable(close):
                close()


def _journal_crashed_session(
    run_dir: Path,
    events: list[dict[str, Any]],
    session: int,
    max_bytes: int,
) -> None:
    """Journal a session that died before it could journal itself (P15, P3).

    Idempotent like every other part of recovery: a session already in the
    journal is left alone, so a second pass writes nothing.

    Built from the folded stream, which bounds what it can say. Tool
    counts come from the session's own ``initiator: model`` rows and the
    transaction hashes from their ``tx_hash`` / ``txs`` fields — both
    exact. The roster is absent: the brief's answer was only ever in a
    transcript, and a crashed session never wrote one.
    """
    if journal.has_session(run_dir, session):
        return
    rows = [e for e in events if e.get("session") == session]
    started = next((e["ts"] for e in rows if e.get("event") == "session_start"), None)
    ended = next(
        (e["ts"] for e in reversed(rows) if e.get("event") == "session_end"),
        started,
    )
    if started is None or ended is None:
        return
    tools: dict[str, int] = {}
    tx_hashes: list[str] = []
    for row in rows:
        if row.get("event") != "tool_call" or row.get("initiator") != "model":
            continue
        name = row.get("tool")
        if isinstance(name, str):
            tools[name] = tools.get(name, 0) + 1
        for candidate in [row.get("tx_hash"), *(_tx_hashes_of(row))]:
            if isinstance(candidate, str) and candidate and candidate not in tx_hashes:
                tx_hashes.append(candidate)
    journal.append(
        run_dir,
        journal.build_entry(
            session=session,
            started_at=started,
            ended_at=ended,
            previous_ended_at=journal.last_ended_at(run_dir),
            tools=tools,
            tx_hashes=tx_hashes,
        ),
        max_bytes=max_bytes,
    )


def _tx_hashes_of(row: dict[str, Any]) -> list[Any]:
    return [r.get("tx_hash") for r in row.get("txs") or () if isinstance(r, dict)]


def _standing_text_of(game: GameTools | None) -> str:
    """The harness's standing text, verbatim, or "" (D1).

    Read off the game tools as an optional attribute: a stand-in or a
    wrapper that does not carry it yields "" — and if it does carry the
    harness's schema version, ``check_pairing`` refuses that combination
    against a 4.x harness rather than letting the text go missing.
    """
    text = getattr(game, "standing_text", "") if game is not None else ""
    return text if isinstance(text, str) else ""


def _schema_version_of(game: GameTools | None) -> str | None:
    version = getattr(game, "harness_schema_version", None) if game is not None else None
    return version if isinstance(version, str) else None


def _harness_provenance(schema_version: str | None, standing_text: str) -> dict[str, Any]:
    """session_start fields for what the handshake stated (D1, P9).

    The standing text is recorded by fingerprint and length, never by
    content: it is fixed by the harness pin and that harness's own
    configuration (its call time box is stated in it), so the hash
    identifies it the way ``harness_tools_hash`` identifies the registry —
    and, unlike the registry hash, it moves when that configuration does.
    Absent when nothing was injected; absence never means an empty text
    was shown.
    """
    fields: dict[str, Any] = {}
    if schema_version is not None:
        fields["harness_schema_version"] = schema_version
    if standing_text:
        fields["harness_standing_text_sha256"] = hashlib.sha256(
            standing_text.encode("utf-8")
        ).hexdigest()
        fields["harness_standing_text_chars"] = len(standing_text)
    return fields


# The daemon query that answers "which lens served this run?" (SPEC D7).
# A general query on the daemon's own registry, not a special path: it
# takes no arguments, needs no mirror, and is answered during bootstrap
# as readily as when live.
_LENS_STATUS_QUERY = "status"


def _lens_provenance(lens: LensQuery | None) -> dict[str, Any]:
    """One operator-side `status` query: which daemon is serving (SPEC D7).

    A run's live daemon version was recorded NOWHERE, so a host running a
    different build than its manifest pins was invisible in the record and
    could only be caught by looking at the host while the run was still
    up. The roster brief's own envelope cannot answer this — its meta
    carries block number, staleness and mode, and no identity at all — so
    this is a second query, and the only one the scaffold makes that the
    agent never sees any part of.

    Never agent-visible, never injected, never a tool_call row: it lands
    on session_start, which is telemetry (P9, I1).

    Degrades to nothing. One attempt, no retry (X21), every failure
    swallowed: a daemon that cannot answer must not cost a session, and
    absence here is read as "not recorded", never as agreement with the
    manifest's pin (N10).
    """
    if lens is None:
        return {}
    try:
        envelope = lens.query(STATUS_QUERY)
    except Exception:
        # Deliberately broad, for the same reason P8's unnormalized catch
        # is: no failure of a diagnostic read may reach the session.
        return {}
    data = envelope.get("data") if isinstance(envelope, dict) else None
    if not isinstance(data, dict):
        return {}
    config = data.get("config")
    config = config if isinstance(config, dict) else {}
    fields: dict[str, Any] = {}
    if isinstance(data.get("version"), str):
        fields["lens_version"] = data["version"]
    if isinstance(data.get("upstreamPin"), str):
        fields["lens_upstream_pin"] = data["upstreamPin"]
    if isinstance(config.get("enrich"), bool):
        fields["lens_enrich"] = config["enrich"]
    if config.get("defaultOperator") is not None:
        fields["lens_default_operator"] = str(config["defaultOperator"])
    return fields


def _load_prompts(run_dir: Path, profile: str = DEFAULT_PROFILE) -> dict[str, Any]:
    """The frozen strings this profile needs (SPEC P13).

    ``system_parts`` is the base prompt followed by the profile's pinned
    appendices, in ladder order; the caller joins them and appends the
    file index. Each asset is a separate frozen file, so what a profile
    was shown is a byte-exact artifact rather than a string built at
    runtime.

    A missing asset raises: the profile named a rung its run directory
    cannot deliver, which is a provisioning error, not a condition to
    absorb.
    """
    prompts_dir = run_dir / "prompts"

    def read(name: str) -> str:
        path = prompts_dir / name
        if not path.is_file():
            raise FileNotFoundError(
                f"scaffold_profile {profile!r} needs prompts/{name}, which is not in {run_dir}"
            )
        return path.read_text(encoding="utf-8").rstrip("\n")

    system_parts = [read("system.txt")]
    if profile_at_least(profile, PROFILE_ORIENTATION):
        system_parts.append(read("orientation.txt"))
    if profile_at_least(profile, PROFILE_PLANNING):
        system_parts.append(read("planning.txt"))
    return {
        "system_parts": system_parts,
        "kickoff": read("kickoff.txt"),
        "continue": read("continue.txt"),
    }


def _write_transcript(run_dir: Path, session: int, messages: list[Message]) -> None:
    """Full message log, one file per session (P12); post-truncation (P9)."""
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
        if message.initiator is not None:
            # The one key in a transcript that was never sent to the
            # provider (P12). A session-start injection is a real assistant
            # turn in context — that is what makes the model read it as a
            # completed call — but it is not a turn the model produced, and
            # without this nothing in the file says so.
            entry["initiator"] = message.initiator
        if message.provider_state is not None:
            # Transcripts record messages as sent (I17); telemetry never
            # carries provider state.
            entry["provider_state"] = {
                "provider": message.provider_state.provider,
                "payload": _jsonable(message.provider_state.payload),
            }
        return entry
    if isinstance(message, ToolResultMessage):
        result: dict[str, Any] = {
            "role": "tool_result",
            "tool_call_id": message.tool_call_id,
            "content": message.content,
            "is_error": message.is_error,
        }
        if message.initiator is not None:
            result["initiator"] = message.initiator
        return result
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
