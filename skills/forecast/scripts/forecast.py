#!/usr/bin/env python3
"""Monte Carlo throughput forecast from a list of completion dates.

Reads completion dates (one ISO date/datetime per line, or a JSON list) from a
file or stdin, builds a daily throughput history (zero days included), then
resamples it to answer either:

  --items N        "When will N items be done?"  -> completion dates by percentile
  --by YYYY-MM-DD  "How many items by this date?" -> item counts by percentile

Assertions here guard internal invariants (a broken one means a wrong forecast, not bad
input); problems with the user's input exit with a message instead.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

PERCENTILES = (50, 70, 85, 95)


@dataclass(frozen=True)
class Sim:
    """How to run a simulation: how many runs, with which random source, capped at how many days."""

    runs: int
    rng: random.Random
    max_days: int = 3650

    def __post_init__(self) -> None:
        assert self.runs > 0, f"a simulation needs at least one run, got runs={self.runs}"
        assert self.max_days > 0, f"max_days must be positive, got {self.max_days}"


@dataclass(frozen=True)
class Plan:
    """How epic work is scheduled: the share of team throughput it gets, and how many run at once."""

    share: float = 1.0
    wip: int = 1

    def __post_init__(self) -> None:
        assert 0 < self.share <= 1, f"share of throughput must be in (0, 1], got {self.share}"
        assert self.wip >= 1, f"at least one epic must be in progress, got wip={self.wip}"


def _check_history(history: Sequence[int]) -> None:
    assert history, "history must cover at least one day"
    assert min(history) >= 0, f"daily counts can't be negative, got min {min(history)}"
    if not any(history):
        sys.exit("error: no completions in the history window, so there is nothing to resample; "
                 "widen the window or check the scope's JQL")


def parse_dates(raw: str) -> list[date]:
    """Dates from one ISO date/datetime per line, or from a JSON list of them."""
    raw = raw.strip()
    values = json.loads(raw) if raw.startswith("[") else raw.splitlines()
    assert isinstance(values, list), f"expected a JSON list of dates, got {type(values).__name__}"
    dates = [datetime.fromisoformat(v.strip().replace("Z", "+00:00")[:10]).date() for v in values if v and v.strip()]
    assert len(dates) <= len(values), f"parsed {len(dates)} dates from only {len(values)} values"
    return dates


def daily_throughput(dates: Sequence[date], start: date, end: date) -> list[int]:
    """Items per day from start to end inclusive, zero days included."""
    assert start <= end, f"window starts {start} after it ends {end}"
    counts = Counter(d for d in dates if start <= d <= end)
    series = [counts[start + timedelta(days=i)] for i in range((end - start).days + 1)]
    assert len(series) == (end - start).days + 1, f"{len(series)} days for a {start}..{end} window"
    assert sum(series) == sum(counts.values()), "every in-window date must land on exactly one day"
    return series


def percentile(sorted_values: Sequence[float], p: int) -> float:
    """The value p% of the way through an ascending sequence (nearest rank, rounding down)."""
    assert sorted_values, "can't take a percentile of no values"
    assert 0 <= p <= 100, f"percentile must be between 0 and 100, got {p}"
    value = sorted_values[min(len(sorted_values) - 1, int(len(sorted_values) * p / 100))]
    assert sorted_values[0] <= value <= sorted_values[-1], f"p{p}={value} outside the values; are they sorted?"
    return value


def simulate_when(history: Sequence[int], items: int, sim: Sim) -> list[float]:
    """Days until `items` are done, one per run, ascending; math.inf where a run passes sim.max_days.

    The backlog is taken as it is now: when new work arrives, forecast again.
    """
    _check_history(history)
    assert items > 0, f"nothing to forecast: items={items}"
    results: list[float] = []
    for _ in range(sim.runs):
        remaining, days = items, 0
        while remaining > 0 and days < sim.max_days:
            remaining -= sim.rng.choice(history)
            days += 1
        results.append(days if remaining <= 0 else math.inf)
    results.sort()
    assert len(results) == sim.runs, f"{len(results)} results for {sim.runs} runs"
    fastest = math.ceil(items / max(history))
    assert results[0] >= fastest, f"a run took {results[0]} days for {items} items; the best day allows no fewer than {fastest}"
    return results


def _next_streams(done_day: list[float], wip: int) -> list[int]:
    """Indexes of the first `wip` streams not yet finished, in priority order."""
    active = [i for i, d in enumerate(done_day) if d == math.inf][:wip]
    assert len(active) <= wip, f"{len(active)} streams active with wip={wip}"
    assert active == sorted(active), f"active streams {active} must stay in priority order"
    return active


def _one_priority_run(history: Sequence[int], remaining: Sequence[int], sim: Sim, plan: Plan) -> list[float]:
    """Finish day of each stream in one simulated run (math.inf if past sim.max_days)."""
    assert len(remaining) > 0, f"no streams to schedule (remaining={list(remaining)})"
    left = list(remaining)
    done_day: list[float] = [0 if n <= 0 else math.inf for n in left]
    day = turn = 0
    while day < sim.max_days and math.inf in done_day:
        day += 1
        for _ in range(sim.rng.choice(history)):
            if plan.share < 1 and sim.rng.random() >= plan.share:
                continue  # this completion went to work outside the plan
            active = _next_streams(done_day, plan.wip)
            if not active:
                break
            j = active[turn % len(active)]
            turn += 1
            left[j] -= 1
            if left[j] <= 0:
                done_day[j] = day
    finished = [d for d in done_day if d != math.inf]
    assert all(0 <= d <= sim.max_days for d in finished), f"finish days {finished} outside 0..{sim.max_days}"
    return done_day


def simulate_priority(history: Sequence[int], remaining: Sequence[int], sim: Sim, plan: Plan) -> list[list[float]]:
    """Days until each of several work streams (e.g. epics), worked in the given order, is done.

    Each completed item goes to this work with probability `plan.share` (the rest of the team's
    throughput is spent elsewhere) and is assigned round-robin across the first `plan.wip`
    unfinished streams. Returns one ascending list of finish days per stream.
    """
    _check_history(history)
    assert remaining, "no streams to schedule"
    runs = [_one_priority_run(history, remaining, sim, plan) for _ in range(sim.runs)]
    finish = [sorted(run[i] for run in runs) for i in range(len(remaining))]
    assert all(len(f) == sim.runs for f in finish), (
        f"every stream needs one finish day per run ({sim.runs}); got {[len(f) for f in finish]}"
    )
    if plan.wip == 1:
        # Strictly sequential: a stream with work left can't finish before any stream ahead of it.
        # That holds run by run, so it also holds between the sorted per-stream lists.
        with_work = [f for f, n in zip(finish, remaining) if n > 0]
        for ahead, behind in zip(with_work, with_work[1:]):
            early = next(((a, b) for a, b in zip(ahead, behind) if a > b), None)
            assert early is None, f"a later stream finished on day {early and early[1]}, before day {early and early[0]}"
    return finish


def simulate_how_many(history: Sequence[int], days: int, sim: Sim) -> list[int]:
    """Items completed over `days` days, one total per run, ascending."""
    _check_history(history)
    assert days > 0, f"forecast horizon must be at least a day, got {days}"
    totals = sorted(sum(sim.rng.choices(history, k=days)) for _ in range(sim.runs))
    assert len(totals) == sim.runs, f"{len(totals)} totals for {sim.runs} runs"
    best = days * max(history)
    assert totals[-1] <= best, f"a run finished {totals[-1]} items in {days} days; the best day every day gives {best}"
    return totals


def histogram(values: Sequence[float], bins: int = 15, width: int = 40) -> str:
    """A text histogram of the finite values (ascending input), `bins` rows at most."""
    assert bins > 0, f"need at least one bin, got {bins}"
    assert width > 0, f"bars need a positive width, got {width}"
    finite = [v for v in values if v != math.inf]
    if not finite:
        return "(no runs finished)"
    lo, hi = finite[0], finite[-1]
    step = max(1, -(-(hi - lo + 1) // bins))
    counts = Counter((v - lo) // step for v in finite)
    peak = max(counts.values())
    return "\n".join(
        f"{lo + b * step:>6} | {'#' * round(counts[b] / peak * width)} {counts[b]}" for b in range(int(max(counts)) + 1)
    )


def when_date(start: date, days: float) -> str | None:
    """ISO date `days` after start, or None for a run that never finished."""
    assert days >= 0, f"a run can't finish before it starts, got {days} days"
    if days == math.inf:
        return None
    finish = start + timedelta(days=int(days))
    assert finish >= start, f"finish {finish} before start {start}"
    return finish.isoformat()


def summarise(args: argparse.Namespace, dates: list[date]) -> tuple[dict, list]:
    """The forecast answer as a dict, plus the raw simulation results for the histogram."""
    h_start = args.history_start or min(dates)
    h_end = args.history_end or date.today()
    if h_start > h_end:
        sys.exit(f"error: --history-start {h_start} is after the history end {h_end}")
    history = daily_throughput(dates, h_start, h_end)
    sim = Sim(args.runs, random.Random(args.seed))
    summary: dict = {
        "history": {
            "start": h_start.isoformat(),
            "end": h_end.isoformat(),
            "days": len(history),
            "items": sum(history),
            "items_per_week": round(sum(history) / len(history) * 7, 2),
        },
        "runs": args.runs,
    }
    if args.items is not None:
        results: list = simulate_when(history, args.items, sim)
        summary["question"] = f"When will {args.items} items be done (starting {args.start})?"
        summary["percentiles"] = {f"p{p}": when_date(args.start, percentile(results, p)) for p in PERCENTILES}
        summary["unit"] = "days"
    else:
        days = (args.by - args.start).days
        if days <= 0:
            sys.exit("error: --by must be after --start")
        results = simulate_how_many(history, days, sim)
        summary["question"] = f"How many items done between {args.start} and {args.by}?"
        # Higher confidence means *fewer* items for a how-many forecast.
        summary["percentiles"] = {f"p{p}": percentile(results, 100 - p) for p in PERCENTILES}
        summary["unit"] = "items"
    assert len(results) == args.runs, f"{len(results)} results for {args.runs} runs"
    assert set(summary["percentiles"]) == {f"p{p}" for p in PERCENTILES}, (
        f"every percentile needs an answer, got {sorted(summary['percentiles'])}"
    )
    return summary, results


def forecast_cli() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", help="file of completion dates (default: stdin)")
    goal = ap.add_mutually_exclusive_group(required=True)
    goal.add_argument("--items", type=int, help="remaining items to forecast a completion date for")
    goal.add_argument("--by", type=date.fromisoformat, help="target date to forecast item count for")
    ap.add_argument("--history-start", type=date.fromisoformat, help="default: earliest completion date")
    ap.add_argument("--history-end", type=date.fromisoformat, help="default: today")
    ap.add_argument("--start", type=date.fromisoformat, default=date.today(), help="forecast start (default: today)")
    ap.add_argument("--runs", type=int, default=10_000)
    ap.add_argument("--seed", type=int)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()
    assert args.items is not None or args.by is not None, "argparse requires --items or --by"

    dates = parse_dates(Path(args.file).read_text() if args.file else sys.stdin.read())
    if not dates:
        sys.exit("error: no completion dates supplied; pass a file with one ISO date per line, or pipe them in")
    summary, results = summarise(args, dates)
    assert summary["history"]["days"] >= 1, f"history must cover at least one day, got {summary['history']}"
    if args.json:
        print(json.dumps(summary, indent=2))
        return
    h = summary["history"]
    print(summary["question"])
    print(f"History: {h['items']} items over {h['days']} days ({h['start']} → {h['end']}), {h['items_per_week']}/week")
    print(f"Runs: {args.runs}\n")
    for k, v in summary["percentiles"].items():
        print(f"  {k[1:]}% confidence: {'not within simulation limit' if v is None else v}")
    print(f"\nDistribution ({summary['unit']}):\n{histogram(results)}")


if __name__ == "__main__":
    forecast_cli()
