"""Harness MCP client: spawns pinned kami-harness as a stdio child, loads game tools (SPEC §2).

Lifecycle per session (SPEC §2): the kami-harness MCP server is spawned
as a stdio child at the SHA pinned in the run manifest; a handshake
failure aborts the session before any model call (the runner writes
``session_end reason=errors`` and schedules ``wake_default``).

The MCP SDK is async; this client runs a private event loop on a
background thread and exposes the synchronous surface the loop needs
(``tool_defs`` + ``execute``, the ``GameTools`` protocol). The stdio
transport's context managers are entered and exited inside a single
manager task, as anyio requires.

Dev pin: kami-harness v1.0.0 (``352da9b`` candidate) — the run manifest
re-pins at launch; the SHA is manifest metadata, recorded on run_start.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import threading
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from kami_agent.adapters.base import ToolDef
from kami_agent.loop import GameToolResult
from kami_agent.tools.errors import ToolError

HARNESS_DEV_PIN_SHA = "352da9b"

DEFAULT_HANDSHAKE_TIMEOUT_S = 60.0


class HarnessError(Exception):
    """Handshake/spawn failure — aborts the session before any model call."""


def tools_hash(tools: list[ToolDef]) -> str:
    """Deterministic hash of the loaded tool surface (session_start.tools_hash)."""
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
        """Call one harness tool; MCP-level errors surface as ToolError (§5.4)."""
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
        return GameToolResult(content=text, tx_hash=_extract_tx_hash(result, text))

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


def _maybe_json(text: str) -> Any:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
