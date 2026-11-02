"""Small shared helpers for tests that run a whole session with no harness."""

from __future__ import annotations

from pathlib import Path

from kami_agent import cli
from kami_agent.adapters.base import AdapterResponse, StopReason, ToolCall, Usage
from kami_agent.governor import PriceTable
from kami_agent.loop import LoopCaps


def materialized(run_dir: Path) -> Path:
    """A run directory carrying every frozen prompt asset, as `init` writes it."""
    prompts = run_dir / "prompts"
    prompts.mkdir(parents=True, exist_ok=True)
    source = cli._prompts_source()
    for name in cli.PROMPT_NAMES:
        (prompts / name).write_text((source / name).read_text(encoding="utf-8"), encoding="utf-8")
    (run_dir / "workspace").mkdir(exist_ok=True)
    return run_dir


class EndOnlyAdapter:
    """Answers every call with end_session; records what it was sent."""

    prices = PriceTable(input_usd_per_mtok=3.0, output_usd_per_mtok=15.0)
    caps = LoopCaps(session_token_cap=100_000)

    def __init__(self) -> None:
        self.requests: list[dict] = []

    def complete(self, system, messages, tools, params):
        self.requests.append({"system": system, "messages": list(messages), "tools": tools})
        return AdapterResponse(
            text_blocks=(),
            tool_calls=(ToolCall(id="t-end", name="end_session", args={"reason": "done"}),),
            stop_reason=StopReason.TOOL_USE,
            usage=Usage(input_tokens=10, output_tokens=5),
        )


def end_only_adapter() -> EndOnlyAdapter:
    return EndOnlyAdapter()
