"""Budget governor: pinned price table, cost math (D16), boundary checks (D13, SPEC §9).

Enforcement happens only at session start; an in-flight session is never
terminated for budget or t_max. Budget state never reaches the agent
through any channel (D12).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from kami_agent.adapters.base import Usage

REASON_BUDGET = "budget"
REASON_T_MAX = "t_max"


@dataclass(frozen=True, slots=True)
class PriceTable:
    """List prices in USD per million tokens, pinned per run in the manifest.

    The cache columns default to None, which resolves to the input rate —
    a manifest without them prices cached tokens at full input price
    (the conservative pre-caching behavior: accounted >= invoiced).
    Provider list rates at pin time: Anthropic 5m cache write = 1.25 x
    input, read = 0.1 x input; OpenAI/Gemini bill no write premium
    (cache_write_tokens is 0 there anyway) and publish a cached-input
    read rate.
    """

    input_usd_per_mtok: float
    output_usd_per_mtok: float
    cache_read_usd_per_mtok: float | None = None
    cache_write_usd_per_mtok: float | None = None


def cost_usd(usage: Usage, prices: PriceTable) -> float:
    """Cache-aware cost (D16 as amended, SPEC §5.2).

    ``(input − cache_read − cache_write) × price_in + cache_read ×
    price_read + cache_write × price_write + output × price_out``.
    With all cache token fields zero this reduces exactly to the v0
    formula regardless of the cache rates.
    """
    read_rate = (
        prices.cache_read_usd_per_mtok
        if prices.cache_read_usd_per_mtok is not None
        else prices.input_usd_per_mtok
    )
    write_rate = (
        prices.cache_write_usd_per_mtok
        if prices.cache_write_usd_per_mtok is not None
        else prices.input_usd_per_mtok
    )
    uncached = usage.input_tokens - usage.cache_read_tokens - usage.cache_write_tokens
    return (
        uncached * prices.input_usd_per_mtok
        + usage.cache_read_tokens * read_rate
        + usage.cache_write_tokens * write_rate
        + usage.output_tokens * prices.output_usd_per_mtok
    ) / 1_000_000


def boundary_check(
    *,
    cumulative_usd: float,
    budget_usd: float,
    first_session_at: str | None,
    t_max_days: float,
    now: datetime,
) -> str | None:
    """Return the run_complete reason (``budget`` | ``t_max``) or None to proceed.

    Called only at the SPEC §3 step-3 boundary, with accounting rebuilt
    from telemetry (§7.1). Stop = min(budget, t_max).
    """
    if cumulative_usd >= budget_usd:
        return REASON_BUDGET
    if first_session_at is not None:
        started = datetime.fromisoformat(first_session_at)
        if now - started >= timedelta(days=t_max_days):
            return REASON_T_MAX
    return None


def overspend_usd(cumulative_usd: float, budget_usd: float) -> float:
    """Bounded overshoot past the soft cap, logged on run_complete (D13)."""
    return max(0.0, cumulative_usd - budget_usd)
