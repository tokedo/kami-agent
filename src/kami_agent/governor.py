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
    """List prices in USD per million tokens, pinned per run in the manifest."""

    input_usd_per_mtok: float
    output_usd_per_mtok: float


def cost_usd(usage: Usage, prices: PriceTable) -> float:
    """``input_tokens x price_in + output_tokens x price_out`` (D16).

    Caching-neutral: the scaffold never requests caching, and provider-side
    auto-caching is invisible here — actual invoices are therefore <= the
    accounted figure.
    """
    return (
        usage.input_tokens * prices.input_usd_per_mtok
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
