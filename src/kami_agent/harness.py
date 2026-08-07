"""Harness MCP client: spawns pinned kami-harness as a stdio child, loads game tools (SPEC D1).

Lifecycle per session (SPEC D1): the kami-harness MCP server is spawned
as a stdio child at the SHA pinned in the run manifest; a handshake
failure aborts the session before any model call (the runner writes
``session_end reason=errors`` and schedules ``wake_default``).

The MCP SDK is async; this client runs a private event loop on a
background thread and exposes the synchronous surface the loop needs
(``tool_defs`` + ``execute``, the ``GameTools`` protocol). The stdio
transport's context managers are entered and exited inside a single
manager task, as anyio requires.

Dev pin: kami-harness 2.1.0 surface (``ba62fc9``) — the run manifest
re-pins at launch; the SHA is manifest metadata, recorded on run_start.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import re
import threading
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from kami_agent.adapters.base import ToolDef
from kami_agent.loop import GameToolResult
from kami_agent.tools.errors import ToolError
from kami_agent.tools.receipts import classify_success

HARNESS_DEV_PIN_SHA = "ba62fc9"

DEFAULT_HANDSHAKE_TIMEOUT_S = 60.0

# The harness publishes a hash of its OWN registry in the initialize
# handshake's ``instructions`` field, as ``tools_hash=<64 hex chars>``.
# Recorded verbatim for drift detection; see the never-equate note on
# ``tools_hash`` below.
_HANDSHAKE_TOOLS_HASH = re.compile(r"tools_hash=([0-9a-f]{64})")

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
        # The harness's own registry hash, as it published it in the
        # handshake. NEVER equated with ``tools_hash`` below.
        self.harness_tools_hash: str | None = None
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
                    published = _HANDSHAKE_TOOLS_HASH.search(init.instructions or "")
                    self.harness_tools_hash = published.group(1) if published else None
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
