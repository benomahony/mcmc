"""Properties of the simulator, checked over generated histories, backlogs and seeds.

Hypothesis drives real code with many inputs, so these also exercise the simulator's own
internal assertions (sorted output, one result per run, sequential epics in order...).
"""

import json
import math
import random
from datetime import date, timedelta

from forecast import (
    Plan,
    Sim,
    daily_throughput,
    histogram,
    parse_dates,
    percentile,
    simulate_how_many,
    simulate_priority,
    simulate_when,
    when_date,
)
from hypothesis import given, settings
from hypothesis import strategies as st

# A day's completions: mostly small, sometimes zero; at least one non-zero day per history.
histories = st.lists(st.integers(0, 8), min_size=1, max_size=60).filter(any)
seeds = st.integers(0, 2**32 - 1)
sims = st.builds(lambda runs, seed: Sim(runs, random.Random(seed)), st.integers(1, 40), seeds)
dates = st.dates(date(2020, 1, 1), date(2030, 12, 31))
FAST = settings(max_examples=150, deadline=None)


# --- parsing and history ------------------------------------------------------------


@given(st.lists(dates, max_size=30), st.sampled_from(["", "T09:30:00Z", "T23:59:00.000+0100"]))
def test_parse_dates_reads_lines_and_json_alike(ds, suffix):
    stamps = [f"{d.isoformat()}{suffix}" for d in ds]
    from_lines = parse_dates("\n\n".join(stamps) + "\n")
    from_json = parse_dates(json.dumps(stamps))
    assert from_lines == ds, f"line format: parsed {from_lines} from {stamps}"
    assert from_json == ds, f"JSON format: parsed {from_json} from {stamps}"


@given(st.lists(dates, max_size=80), dates, st.integers(0, 120))
def test_daily_throughput_counts_every_in_window_date_once(ds, start, span):
    end = start + timedelta(days=span)
    series = daily_throughput(ds, start, end)
    in_window = [d for d in ds if start <= d <= end]
    assert len(series) == span + 1, f"{len(series)} days for a {span + 1}-day window"
    assert sum(series) == len(in_window), f"series totals {sum(series)}, but {len(in_window)} dates fall in the window"
    assert all(series[(d - start).days] > 0 for d in in_window), f"an in-window date landed on a zero day: {series}"


def test_daily_throughput_keeps_zero_days():
    day1 = date(2026, 1, 1)
    series = daily_throughput([day1, day1, day1 + timedelta(2)], day1, day1 + timedelta(3))
    assert series == [2, 0, 1, 0], f"expected two on day 1, one on day 3 and zero days kept, got {series}"
    assert len(series) == 4, f"a 4-day window needs 4 entries, got {len(series)}"


# --- percentiles ---------------------------------------------------------------------


@given(st.lists(st.integers(0, 1000), min_size=1, max_size=200), st.integers(0, 100), st.integers(0, 100))
def test_percentile_is_a_member_and_monotone(values, p, q):
    values.sort()
    lo, hi = sorted((p, q))
    assert percentile(values, lo) in values, f"p{lo} of {values} isn't one of the values"
    assert percentile(values, lo) <= percentile(values, hi), f"p{lo} > p{hi} for {values}"


@given(st.lists(st.integers(0, 1000), min_size=1, max_size=200), st.integers(1, 100))
def test_at_least_p_percent_of_values_are_at_or_below_the_pth_percentile(values, p):
    values.sort()
    v = percentile(values, p)
    share = sum(x <= v for x in values) / len(values)
    assert share >= p / 100, f"only {share:.2%} of values are <= p{p}={v}"
    assert v <= values[-1], f"p{p}={v} beyond the largest value {values[-1]}"


# --- when will N items be done -------------------------------------------------------


@FAST
@given(histories, st.integers(1, 60), sims)
def test_when_results_are_bounded_by_the_best_and_worst_days(history, items, sim):
    results = simulate_when(history, items, sim)
    fastest = math.ceil(items / max(history))
    assert len(results) == sim.runs, f"one result per run: {len(results)} results for {sim.runs} runs"
    assert results == sorted(results), f"results must come back ascending for percentiles to work: {results}"
    assert all(r >= fastest for r in results), f"faster than {fastest} days is impossible, got {results[0]}"
    if min(history) > 0:  # every day completes something, so there is a worst case too
        slowest = math.ceil(items / min(history))
        assert results[-1] <= slowest, f"slower than {slowest} days is impossible, got {results[-1]}"


@FAST
@given(st.integers(1, 8), st.integers(1, 60), sims)
def test_constant_throughput_gives_one_exact_answer(per_day, items, sim):
    results = simulate_when([per_day], items, sim)
    expected = math.ceil(items / per_day)
    assert set(results) == {expected}, f"{items} items at {per_day}/day must take {expected} days, got {set(results)}"
    assert len(results) == sim.runs, f"{len(results)} results for {sim.runs} runs"


@FAST
@given(histories, st.integers(1, 40), st.integers(1, 30), seeds)
def test_zero_arrivals_change_nothing(history, items, runs, seed):
    plain = simulate_when(history, items, Sim(runs, random.Random(seed)))
    zero = simulate_when(history, items, Sim(runs, random.Random(seed)), arrivals=[0] * len(history))
    assert plain == zero, f"zero arrivals changed the forecast: {plain} vs {zero}"
    assert len(plain) == runs, f"{len(plain)} results for {runs} runs"


@FAST
@given(histories, st.integers(1, 40), st.integers(1, 30), seeds)
def test_chance_by_the_pth_date_is_at_least_p(history, items, runs, seed):
    results = simulate_when(history, items, Sim(runs, random.Random(seed)))
    for p in (50, 85, 95):
        cutoff = percentile(results, p)
        share = sum(r <= cutoff for r in results) / len(results)
        assert share >= p / 100, f"only {share:.0%} of runs finish by the p{p} answer ({cutoff} days)"
        assert cutoff in results, f"p{p}={cutoff} isn't a simulated outcome"


def test_new_work_arriving_slows_completion():
    history = [3, 1, 2, 0, 4] * 6
    plain = simulate_when(history, 50, Sim(2000, random.Random(1)))
    grown = simulate_when(history, 50, Sim(2000, random.Random(1)), arrivals=[1, 0, 1, 1, 0] * 6)
    assert percentile(grown, 85) > percentile(plain, 85), f"p85 {percentile(grown, 85)} with arrivals vs {percentile(plain, 85)}"
    assert percentile(grown, 50) > percentile(plain, 50), f"p50 {percentile(grown, 50)} with arrivals vs {percentile(plain, 50)}"


def test_runs_that_never_finish_are_infinite():
    results = simulate_when([1], 5, Sim(10, random.Random(0), max_days=50), arrivals=[1])
    assert all(r == math.inf for r in results), f"one in, one out never finishes, got {results}"
    assert when_date(date(2026, 1, 1), results[0]) is None, f"a run that never finishes ({results[0]}) has no date"


# --- how many by a date --------------------------------------------------------------


@FAST
@given(histories, st.integers(1, 120), sims)
def test_how_many_is_bounded_by_the_best_and_worst_days(history, days, sim):
    totals = simulate_how_many(history, days, sim)
    assert len(totals) == sim.runs, f"one total per run: {len(totals)} totals for {sim.runs} runs"
    assert totals == sorted(totals), f"totals must come back ascending for percentiles to work: {totals}"
    assert days * min(history) <= totals[0], f"fewer than {days * min(history)} items is impossible, got {totals[0]}"
    assert totals[-1] <= days * max(history), f"more than {days * max(history)} items is impossible, got {totals[-1]}"


# --- epics in priority order ---------------------------------------------------------


@FAST
@given(histories, st.lists(st.integers(0, 12), min_size=1, max_size=5), sims, st.integers(1, 3))
def test_priority_finish_days_are_bounded(history, remaining, sim, wip):
    finish = simulate_priority(history, remaining, sim, Plan(wip=wip))
    assert len(finish) == len(remaining), f"{len(finish)} streams back for {len(remaining)} scheduled"
    for f, n in zip(finish, remaining, strict=True):
        fastest = math.ceil(n / max(history))
        assert f == sorted(f) and len(f) == sim.runs, f"stream of {n}: expected {sim.runs} sorted days, got {f}"
        assert f[0] >= fastest, f"a stream of {n} items can't finish in under {fastest} days, got {f[0]}"


@FAST
@given(histories, st.lists(st.integers(1, 12), min_size=1, max_size=5), sims)
def test_last_sequential_stream_takes_as_long_as_all_the_work(history, remaining, sim):
    finish = simulate_priority(history, remaining, sim, Plan())
    together = math.ceil(sum(remaining) / max(history))
    assert finish[-1][0] >= together, f"all {sum(remaining)} items can't be done in under {together} days"
    assert all(a[0] <= b[0] for a, b in zip(finish, finish[1:], strict=False)), f"streams finished out of order: {finish}"


def test_priority_order_and_wip():
    first, second = simulate_priority([2], [2, 4], Sim(10, random.Random(0)), Plan())
    assert (set(first), set(second)) == ({1}, {3}), f"one at a time: day 1 then day 3, got {set(first)}, {set(second)}"
    # Two at a time, round-robin: the small one finishes on day 2, the big one on day 3.
    first, second = simulate_priority([2], [2, 4], Sim(10, random.Random(0)), Plan(wip=2))
    assert (set(first), set(second)) == ({2}, {3}), f"two at a time: day 2 and day 3, got {set(first)}, {set(second)}"


def test_a_smaller_share_of_throughput_slows_the_plan():
    (full,) = simulate_priority([4], [20], Sim(500, random.Random(0)), Plan())
    (half,) = simulate_priority([4], [20], Sim(500, random.Random(0)), Plan(share=0.5))
    assert percentile(half, 50) > percentile(full, 50), f"half the capacity: p50 {percentile(half, 50)} vs {percentile(full, 50)}"
    assert set(full) == {5}, f"20 items at 4/day with all capacity take 5 days, got {set(full)}"


# --- presentation ----------------------------------------------------------------------


@given(st.lists(st.integers(0, 500), min_size=1, max_size=300), st.integers(1, 20))
def test_histogram_shows_every_value_in_at_most_bins_rows(values, bins):
    values.sort()
    rows = histogram(values, bins=bins).splitlines()
    shown = sum(int(row.rsplit(" ", 1)[1]) for row in rows)
    assert shown == len(values), f"histogram shows {shown} of {len(values)} values"
    assert len(rows) <= bins + 1, f"{len(rows)} rows for {bins} bins"


@given(dates, st.integers(0, 3650), st.integers(0, 3650))
def test_when_date_moves_forward_with_days(start, a, b):
    lo, hi = sorted((a, b))
    assert when_date(start, lo) == (start + timedelta(days=lo)).isoformat(), f"{lo} days after {start}"
    assert (when_date(start, lo) or "") <= (when_date(start, hi) or ""), f"{lo} days lands after {hi} days"
