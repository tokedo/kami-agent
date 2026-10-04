"""Harness MCP client: spawns pinned kami-harness as a stdio child, loads game tools (SPEC D1).

Lifecycle per session (SPEC D1): the kami-harness MCP server is spawned
as a stdio child at the SHA pinned in the run manifest; a handshake
failure aborts the session before any model call (the runner writes
``session_end reason=errors`` and schedules ``wake_default``).

The handshake's ``instructions`` field is parsed once (``Handshake``):
line 1 carries the harness's machine tokens, and from kami-harness 4.0.0
everything after the first newline is its standing text — the rules it
states once for its whole surface instead of on every tool description.
The runner shows that text to the model in the system prompt, verbatim,
and refuses a 4.x harness whose text did not arrive (``check_pairing``).

The MCP SDK is async; this client runs a private event loop on a
background thread and exposes the synchronous surface the loop needs
(``tool_defs`` + ``execute``, the ``GameTools`` protocol). The stdio
transport's context managers are entered and exited inside a single
manager task, as anyio requires.

Dev pin: kami-harness 4.2.0 surface (``c036554``) — the run manifest
re-pins at launch; the SHA is manifest metadata, recorded on run_start.
Any 4.x harness pairs with this scaffold: the pairing check keys on the
handshake's ``schema_version`` MAJOR, never on this pin.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import re
import threading
from dataclasses import dataclass
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from kami_agent.adapters.base import ToolDef
from kami_agent.loop import GameToolResult
from kami_agent.tools.errors import ToolError
from kami_agent.tools.receipts import classify_success

HARNESS_DEV_PIN_SHA = "c036554"

DEFAULT_HANDSHAKE_TIMEOUT_S = 60.0

# The harness publishes a hash of its OWN registry in the initialize
# handshake's ``instructions`` field, as ``tools_hash=<64 hex chars>``.
# Recorded verbatim for drift detection; see the never-equate note on
# ``tools_hash`` below.
_HANDSHAKE_TOOLS_HASH = re.compile(r"tools_hash=([0-9a-f]{64})")
_HANDSHAKE_SCHEMA_VERSION = re.compile(r"(?:^|\s)schema_version=(\S+)")
_HANDSHAKE_ERROR_SNIPPETS = re.compile(r"(?:^|\s)error_snippets=(\S+)")
_SEMVER_MAJOR = re.compile(r"(\d+)(?:[.+-]|$)")

# The first harness MAJOR that says its standing text in the handshake
# instead of on its tool descriptions. From this version on, a session
# that does not show the model that text shows it nothing of it: the
# descriptions no longer carry it.
STANDING_TEXT_HARNESS_MAJOR = 4


@dataclass(frozen=True, slots=True)
class Handshake:
    """What the harness states in the MCP ``initialize`` ``instructions`` field.

    The field has two parts. **Line 1** is machine tokens, space-separated:
    ``tools_hash=<64 hex> schema_version=<semver> error_snippets=<on|off>``
    (older harnesses publish a prefix of that list, or nothing). **Everything
    after the first newline** is the harness's *standing text*: the rules
    that apply to its whole surface — that ``untrusted`` fields are player
    data and never instructions, how its world-state reads are served, and
    the like — said once, here, instead of being repeated on every tool
    description they apply to. Harnesses before 4.0.0 send no second part.

    ``standing_text`` is that remainder **verbatim** — not stripped, not
    reflowed — or the empty string when there is none (a remainder of
    whitespace alone is none: there is nothing in it to show).
    """

    tools_hash: str | None = None
    schema_version: str | None = None
    error_snippets: str | None = None
    standing_text: str = ""


def parse_instructions(instructions: str | None) -> Handshake:
    """Split the handshake ``instructions`` field into its two parts.

    Tokens are read from line 1 only, so nothing in the standing text can
    ever be mistaken for one. A field holding line 1 alone — every harness
    before 4.0.0 — parses exactly as it always did and yields no text.
    """
    if not instructions:
        return Handshake()
    first_line, _, remainder = instructions.partition("\n")
    tools = _HANDSHAKE_TOOLS_HASH.search(first_line)
    schema = _HANDSHAKE_SCHEMA_VERSION.search(first_line)
    snippets = _HANDSHAKE_ERROR_SNIPPETS.search(first_line)
    return Handshake(
        tools_hash=tools.group(1) if tools else None,
        schema_version=schema.group(1) if schema else None,
        error_snippets=snippets.group(1) if snippets else None,
        standing_text=remainder if remainder.strip() else "",
    )


def schema_major(schema_version: str | None) -> int | None:
    """The MAJOR of a ``schema_version`` token, or None if it has none."""
    if not schema_version:
        return None
    match = _SEMVER_MAJOR.match(schema_version)
    return int(match.group(1)) if match else None


class HarnessPairingError(Exception):
    """This scaffold and the pinned harness are not a working pair.

    Raised before any telemetry for the session and before any model
    call. Not a ``HarnessError``: a handshake failure is a session that
    could not start and is recorded as one, while this is a deployment
    that must not start at all, and its message is for an operator.
    """


def check_pairing(schema_version: str | None, standing_text: str) -> None:
    """Refuse a harness whose standing text would not reach the model.

    From kami-harness 4.0.0 the rule that tool output is untrusted data,
    not instructions, is stated ONCE, in the handshake, and on no tool
    description. A session against such a harness that received no
    standing text would run with that rule shown nowhere — so it is
    refused instead of run degraded. A harness that publishes no
    ``schema_version`` token, or a MAJOR below 4, owes no standing text
    and passes.
    """
    major = schema_major(schema_version)
    if major is None or major < STANDING_TEXT_HARNESS_MAJOR or standing_text:
        return
    raise HarnessPairingError(
        f"refusing to start: kami-harness {schema_version} states its standing "
        "text — including the rule that tool output is untrusted data, never "
        "instructions — only in its MCP handshake, after the first line of the "
        "instructions field, and no tool description carries it any more. This "
        "session received none of that text, so the model would never be shown "
        "it. Check that the harness build is intact and that nothing between it "
        "and this scaffold drops the instructions field (kami-harness 4.0.0 and "
        "newer pair with kami-agent 0.7.0 and newer)."
    )


# How deep to look for in-band per-transaction receipt arrays. Multi-tx
# results carry them either at the top level (one array for the whole
# call) or one per result row inside a batch's ``results`` list; three
# levels covers both with margin and bounds the walk on a hostile payload.
_TXS_MAX_DEPTH = 3


class HarnessError(Exception):
    """Handshake/spawn failure — aborts the session before any model call."""


def tools_hash(tools: list[ToolDef]) -> str:
    """Deterministic hash of the loaded tool surface (session_start.tools_hash).

    This is the SCAFFOLD's fingerprint of the surface it loaded: it spans
    the harness tools AND the scaffold tools, uses this module's own
    serialization, and carries a ``sha256:`` prefix. The harness also
    publishes a hash of its own registry, bare hex, over its own
    serialization. **The two values are different by construction and
    must never be equated or reconciled** — they answer different
    questions (what the model was shown vs. what the harness registered).
    """
    canonical = json.dumps(
        [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in tools
        ],
        sort_keys=True,
        ensure_ascii=False,
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class HarnessClient:
    """Synchronous MCP client over a stdio child; implements ``GameTools``."""

    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        handshake_timeout_s: float = DEFAULT_HANDSHAKE_TIMEOUT_S,
    ) -> None:
        self._params = StdioServerParameters(command=command, args=args or [], cwd=cwd, env=env)
        self.tool_defs: list[ToolDef] = []
        self.server_name: str | None = None
        self.server_version: str | None = None
        # Everything the harness stated in the handshake's instructions
        # field, parsed once (see ``Handshake``).
        self.handshake = Handshake()
        # The harness's own registry hash, as it published it in the
        # handshake. NEVER equated with ``tools_hash`` below.
        self.harness_tools_hash: str | None = None
        # The harness's contract version as its handshake states it, and
        # its standing text verbatim ("" when it sends none). The runner
        # puts the text in the system prompt and records both.
        self.harness_schema_version: str | None = None
        self.standing_text: str = ""
        self._session: ClientSession | None = None

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="harness-mcp", daemon=True
        )
        self._thread.start()
        # Thread-safe handshake handle: the manager task resolves it from
        # the private loop; __init__ waits on it from the caller's thread.
        self._ready: concurrent.futures.Future[None] = concurrent.futures.Future()
        self._shutdown = asyncio.Event()
        self._manager_future = asyncio.run_coroutine_threadsafe(self._manager(), self._loop)
        try:
            self._ready.result(timeout=handshake_timeout_s)
        except Exception as exc:
            self.close()
            raise HarnessError(f"harness handshake failed: {exc}") from exc

    async def _manager(self) -> None:
        """Own the transport contexts for the whole session (same-task enter/exit)."""
        try:
            async with stdio_client(self._params) as (read, write):
                async with ClientSession(read, write) as session:
                    init = await session.initialize()
                    tools = await session.list_tools()
                    self._session = session
                    self.server_name = init.serverInfo.name
                    self.server_version = init.serverInfo.version
                    self.handshake = parse_instructions(init.instructions)
                    self.harness_tools_hash = self.handshake.tools_hash
                    self.harness_schema_version = self.handshake.schema_version
                    self.standing_text = self.handshake.standing_text
                    self.tool_defs = [
                        ToolDef(
                            name=t.name,
                            description=t.description or "",
                            input_schema=t.inputSchema,
                        )
                        for t in tools.tools
                    ]
                    self._ready.set_result(None)
                    await self._shutdown.wait()
        except Exception as exc:
            if not self._ready.done():
                self._ready.set_exception(exc)
        finally:
            self._session = None

    # --- GameTools ------------------------------------------------------------

    def execute(self, name: str, args: dict[str, Any]) -> GameToolResult:
        """Call one harness tool; MCP-level errors surface as ToolError (P2).

        The harness raises confirmed reverts and unconfirmed transactions
        rather than returning them, so those arrive here as ``isError``
        results. The message is re-raised **verbatim** — it is the only
        account of what happened on-chain, and the agent gets it unedited.
        """
        session = self._session
        if session is None:
            raise ToolError("harness is not connected")
        future = asyncio.run_coroutine_threadsafe(session.call_tool(name, args), self._loop)
        result = future.result()
        text = "\n".join(
            block.text for block in result.content if getattr(block, "type", "") == "text"
        )
        if result.isError:
            raise ToolError(text or f"{name} failed")
        structured = getattr(result, "structuredContent", None)
        return GameToolResult(
            content=text,
            tx_hash=_extract_tx_hash(result, text),
            terminal_state=classify_success(text, structured),
            txs=_extract_txs(structured, text),
        )

    # --- lifecycle --------------------------------------------------------------

    def close(self) -> None:
        """Signal the manager to unwind the child and stop the private loop."""
        if self._loop.is_closed():
            return
        self._loop.call_soon_threadsafe(self._shutdown.set)
        try:
            self._manager_future.result(timeout=15.0)
        except Exception:
            pass  # unwind is best-effort; the child dies with the process
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5.0)
        if not self._loop.is_running():
            self._loop.close()

    def __enter__(self) -> HarnessClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _extract_tx_hash(result: Any, text: str) -> str | None:
    """Best-effort tx_hash for telemetry: structured content first, then JSON text."""
    structured = getattr(result, "structuredContent", None)
    for candidate in (structured, _maybe_json(text)):
        if isinstance(candidate, dict):
            value = candidate.get("tx_hash")
            if isinstance(value, str):
                return value
            inner = candidate.get("result")
            if isinstance(inner, dict) and isinstance(inner.get("tx_hash"), str):
                return inner["tx_hash"]
    return None


def _extract_txs(structured: Any, text: str) -> tuple[dict[str, Any], ...]:
    """Per-transaction receipt evidence a multi-tx result carries in band.

    A tool that submits more than one transaction reports each of them —
    hop by hop, or item by item — with whatever of ``tx_hash`` /
    ``status`` / ``block`` / ``gas_used`` it has, including for the step
    that failed. Those transactions are real and final on-chain whether
    or not the call as a whole succeeded, so losing them from telemetry
    makes any transaction-keyed reconciliation come up short.

    The arrays appear at two nesting levels — one for the whole call, or
    one per row inside a batch's result list — so this walks rather than
    reading a fixed path, in document order, bounded depth. Entries are
    copied verbatim; nothing is normalized or summed.
    """
    for candidate in (structured, _maybe_json(text)):
        found: list[dict[str, Any]] = []
        _collect_txs(candidate, found, 0)
        if found:
            return tuple(found)
    return ()


def _collect_txs(node: Any, out: list[dict[str, Any]], depth: int) -> None:
    if depth > _TXS_MAX_DEPTH:
        return
    if isinstance(node, dict):
        entries = node.get("txs")
        if isinstance(entries, list):
            out.extend(e for e in entries if isinstance(e, dict))
        for key, value in node.items():
            if key != "txs":
                _collect_txs(value, out, depth + 1)
    elif isinstance(node, list):
        for item in node:
            _collect_txs(item, out, depth + 1)


def _maybe_json(text: str) -> Any:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
