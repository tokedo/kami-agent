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


# --- cache-aware cost (SPEC §5.2, D16 as amended) ----------------------------

CACHED_PRICES = PriceTable(
    input_usd_per_mtok=1.0,
    output_usd_per_mtok=5.0,
    cache_read_usd_per_mtok=0.10,
    cache_write_usd_per_mtok=1.25,
)


def test_cache_zero_reduces_exactly_to_v0_formula():
    # Backward compatibility: with all cache token fields zero the formula
    # is the v0 formula to the cent, cache rates present or not.
    usage = Usage(input_tokens=123_456, output_tokens=7_890)
    v0 = (123_456 * 1.0 + 7_890 * 5.0) / 1e6
    assert cost_usd(usage, CACHED_PRICES) == v0
    bare = PriceTable(input_usd_per_mtok=1.0, output_usd_per_mtok=5.0)
    assert cost_usd(usage, bare) == v0


def test_synthetic_cached_call_priced_by_component():
    # input 23,242 total = 314 uncached + 22,000 read + 928 written.
    usage = Usage(
        input_tokens=23_242,
        output_tokens=1_000,
        cache_read_tokens=22_000,
        cache_write_tokens=928,
    )
    expected = (314 * 1.0 + 22_000 * 0.10 + 928 * 1.25 + 1_000 * 5.0) / 1e6
    assert cost_usd(usage, CACHED_PRICES) == pytest.approx(expected)
    # And a cache hit is strictly cheaper than the same call uncached.
    uncached = Usage(input_tokens=23_242, output_tokens=1_000)
    assert cost_usd(usage, CACHED_PRICES) < cost_usd(uncached, CACHED_PRICES)


def test_absent_cache_rates_fall_back_to_input_rate():
    # A manifest without the cache columns prices cached tokens at the
    # full input rate — the conservative pre-caching behavior (accounted
    # >= invoiced), never a silent under-count.
    bare = PriceTable(input_usd_per_mtok=1.0, output_usd_per_mtok=5.0)
    cached = Usage(
        input_tokens=10_000, output_tokens=100, cache_read_tokens=9_000, cache_write_tokens=500
    )
    flat = Usage(input_tokens=10_000, output_tokens=100)
    assert cost_usd(cached, bare) == cost_usd(flat, bare)


def test_read_only_cache_pricing_no_write_premium():
    # OpenAI/Gemini shape: reads at the published cached-input rate,
    # cache_write_tokens always 0.
    prices = PriceTable(
        input_usd_per_mtok=0.15,
        output_usd_per_mtok=0.60,
        cache_read_usd_per_mtok=0.075,
        cache_write_usd_per_mtok=0.15,
    )
    usage = Usage(input_tokens=13_741, output_tokens=200, cache_read_tokens=12_800)
    expected = (941 * 0.15 + 12_800 * 0.075 + 200 * 0.60) / 1e6
    assert cost_usd(usage, prices) == pytest.approx(expected)


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
