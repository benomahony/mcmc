import math
import random
from datetime import date

from forecast import daily_throughput, parse_dates, percentile, simulate_how_many, simulate_priority, simulate_when


def test_parse_dates_handles_lines_json_and_timestamps():
    assert parse_dates("2026-01-02\n\n2026-01-03T10:00:00Z\n") == [date(2026, 1, 2), date(2026, 1, 3)]
    assert parse_dates('["2026-01-02T23:59:00.000+0100"]') == [date(2026, 1, 2)]


def test_daily_throughput_includes_zero_days():
    dates = [date(2026, 1, 1), date(2026, 1, 1), date(2026, 1, 3)]
    assert daily_throughput(dates, date(2026, 1, 1), date(2026, 1, 4)) == [2, 0, 1, 0]


def test_constant_throughput_is_deterministic():
    assert set(simulate_when([2], 10, 100, random.Random(0))) == {5}
    assert set(simulate_how_many([2], 7, 100, random.Random(0))) == {14}


def test_scope_growth_slows_completion():
    rng = random.Random(1)
    history = [3, 1, 2, 0, 4] * 6
    plain = simulate_when(history, 50, 2000, rng)
    grown = simulate_when(history, 50, 2000, rng, arrivals=[1, 0, 1, 1, 0] * 6)
    assert percentile(grown, 85) > percentile(plain, 85)


def test_non_converging_runs_are_capped_as_inf():
    results = simulate_when([1], 5, 10, random.Random(0), arrivals=[1], max_days=50)
    assert all(r == math.inf for r in results)


def test_priority_sequential_and_wip():
    rng = random.Random(0)
    first, second = simulate_priority([2], [2, 4], 10, rng)
    assert set(first) == {1} and set(second) == {3}
    # Two at a time, round-robin: the small one finishes on day 2, the big one on day 3.
    first, second = simulate_priority([2], [2, 4], 10, rng, wip=2)
    assert set(first) == {2} and set(second) == {3}


def test_priority_share_slows_everything():
    rng = random.Random(0)
    (full,) = simulate_priority([4], [20], 500, rng)
    (half,) = simulate_priority([4], [20], 500, rng, share=0.5)
    assert percentile(half, 50) > percentile(full, 50)
