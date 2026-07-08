"""Governor: cost math (D16) and boundary checks (D13, SPEC §9)."""

from datetime import UTC, datetime

import pytest

from kami_agent.adapters.base import Usage
from kami_agent.governor import PriceTable, boundary_check, cost_usd, overspend_usd

PRICES = PriceTable(input_usd_per_mtok=3.0, output_usd_per_mtok=15.0)
NOW = datetime(2026, 7, 8, 12, 0, 0, tzinfo=UTC)


def test_cost_is_list_price_times_reported_tokens():
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert cost_usd(usage, PRICES) == pytest.approx(18.0)
    usage = Usage(input_tokens=2500, output_tokens=400)
    assert cost_usd(usage, PRICES) == pytest.approx(2500 * 3.0 / 1e6 + 400 * 15.0 / 1e6)
    assert cost_usd(Usage(input_tokens=0, output_tokens=0), PRICES) == 0.0


def test_reasoning_tokens_do_not_double_count():
    # output_tokens already includes reasoning tokens (D16); the subset
    # field never enters the cost formula.
    with_subset = Usage(input_tokens=100, output_tokens=500, reasoning_tokens=300)
    without = Usage(input_tokens=100, output_tokens=500)
    assert cost_usd(with_subset, PRICES) == cost_usd(without, PRICES)


def test_boundary_budget_reached():
    assert (
        boundary_check(
            cumulative_usd=10.0,
            budget_usd=10.0,
            first_session_at=None,
            t_max_days=30,
            now=NOW,
        )
        == "budget"
    )


def test_boundary_t_max_reached():
    assert (
        boundary_check(
            cumulative_usd=1.0,
            budget_usd=10.0,
            first_session_at="2026-06-01T00:00:00+00:00",
            t_max_days=30,
            now=NOW,
        )
        == "t_max"
    )


def test_boundary_proceeds_otherwise():
    assert (
        boundary_check(
            cumulative_usd=9.99,
            budget_usd=10.0,
            first_session_at="2026-07-01T00:00:00+00:00",
            t_max_days=30,
            now=NOW,
        )
        is None
    )
    # A run with no sessions yet has no t_max clock.
    assert (
        boundary_check(
            cumulative_usd=0.0, budget_usd=10.0, first_session_at=None, t_max_days=30, now=NOW
        )
        is None
    )


def test_budget_checked_before_t_max():
    assert (
        boundary_check(
            cumulative_usd=11.0,
            budget_usd=10.0,
            first_session_at="2026-01-01T00:00:00+00:00",
            t_max_days=30,
            now=NOW,
        )
        == "budget"
    )


def test_overspend():
    assert overspend_usd(10.37, 10.0) == pytest.approx(0.37)
    assert overspend_usd(9.5, 10.0) == 0.0
