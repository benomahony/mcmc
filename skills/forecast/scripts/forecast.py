#!/usr/bin/env python3
"""Monte Carlo throughput forecast from a list of completion dates.

Reads completion dates (one ISO date/datetime per line, or a JSON list) from a
file or stdin, builds a daily throughput history (zero days included), then
resamples it to answer either:

  --items N        "When will N items be done?"  -> completion dates by percentile
  --by YYYY-MM-DD  "How many items by this date?" -> item counts by percentile
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from collections import Counter
from collections.abc import Sequence
from datetime import date, datetime, timedelta

PERCENTILES = (50, 70, 85, 95)


def parse_dates(raw: str) -> list[date]:
    raw = raw.strip()
    values = json.loads(raw) if raw.startswith("[") else raw.splitlines()
    return [
        datetime.fromisoformat(v.strip().replace("Z", "+00:00")[:10]).date()
        for v in values
        if v and v.strip()
    ]


def daily_throughput(dates: list[date], start: date, end: date) -> list[int]:
    counts = Counter(d for d in dates if start <= d <= end)
    return [counts[start + timedelta(days=i)] for i in range((end - start).days + 1)]


def percentile(sorted_values: Sequence[float], p: int) -> float:
    return sorted_values[min(len(sorted_values) - 1, int(len(sorted_values) * p / 100))]


def simulate_when(
    history: list[int],
    items: int,
    runs: int,
    rng: random.Random,
    arrivals: list[int] | None = None,
    max_days: int = 3650,
) -> list[float]:
    """Days until `items` are done; math.inf for runs that don't finish within max_days.

    With `arrivals` (daily new-item counts aligned with `history`), each simulated day
    samples one historical day and applies both its throughput and its arrivals.
    """
    if not any(history):
        sys.exit("error: no completions in the history window, cannot forecast")
    if arrivals is not None and len(arrivals) != len(history):
        raise ValueError("arrivals must align with history")
    days_idx = range(len(history))
    results: list[float] = []
    for _ in range(runs):
        remaining, days = items, 0
        while remaining > 0 and days < max_days:
            i = rng.choice(days_idx)
            remaining -= history[i] - (arrivals[i] if arrivals else 0)
            days += 1
        results.append(days if remaining <= 0 else math.inf)
    return sorted(results)


def simulate_priority(
    history: list[int],
    remaining: list[int],
    runs: int,
    rng: random.Random,
    share: float = 1.0,
    wip: int = 1,
    max_days: int = 3650,
) -> list[list[float]]:
    """Days until each of several work streams (e.g. epics), worked in the given order, is done.

    Each completed item goes to this work with probability `share` (the rest of the team's
    throughput is spent elsewhere) and is assigned round-robin across the first `wip`
    unfinished streams. Returns one sorted list of finish days per stream (math.inf if not
    finished within max_days).
    """
    if not any(history):
        sys.exit("error: no completions in the history window, cannot forecast")
    finish: list[list[float]] = [[] for _ in remaining]
    for _ in range(runs):
        left = list(remaining)
        done_day = [0 if n <= 0 else math.inf for n in left]
        day = turn = 0
        while day < max_days and math.inf in done_day:
            day += 1
            for _ in range(rng.choice(history)):
                if share < 1 and rng.random() >= share:
                    continue
                active = [i for i, d in enumerate(done_day) if d == math.inf][:wip]
                if not active:
                    break
                j = active[turn % len(active)]
                turn += 1
                left[j] -= 1
                if left[j] <= 0:
                    done_day[j] = day
        for i, d in enumerate(done_day):
            finish[i].append(d)
    return [sorted(f) for f in finish]


def simulate_how_many(history: list[int], days: int, runs: int, rng: random.Random) -> list[int]:
    return sorted(sum(rng.choices(history, k=days)) for _ in range(runs))


def histogram(values: Sequence[float], bins: int = 15, width: int = 40) -> str:
    values = [v for v in values if v != math.inf]
    if not values:
        return "(no runs finished)"
    lo, hi = values[0], values[-1]
    step = max(1, -(-(hi - lo + 1) // bins))
    counts = Counter((v - lo) // step for v in values)
    peak = max(counts.values())
    return "\n".join(
        f"{lo + b * step:>6} | {'#' * round(counts[b] / peak * width)} {counts[b]}"
        for b in range(int(max(counts)) + 1)
    )


def when_date(start: date, days: float) -> str | None:
    return None if days == math.inf else (start + timedelta(days=int(days))).isoformat()


def main() -> None:
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

    raw = open(args.file).read() if args.file else sys.stdin.read()
    dates = parse_dates(raw)
    if not dates:
        sys.exit("error: no completion dates supplied")

    h_start = args.history_start or min(dates)
    h_end = args.history_end or date.today()
    history = daily_throughput(dates, h_start, h_end)
    rng = random.Random(args.seed)

    summary = {
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
        results = simulate_when(history, args.items, args.runs, rng)
        summary["question"] = f"When will {args.items} items be done (starting {args.start})?"
        summary["percentiles"] = {
            f"p{p}": when_date(args.start, percentile(results, p)) for p in PERCENTILES
        }
        label = "days"
    else:
        days = (args.by - args.start).days
        if days <= 0:
            sys.exit("error: --by must be after --start")
        results = simulate_how_many(history, days, args.runs, rng)
        summary["question"] = f"How many items done between {args.start} and {args.by}?"
        # Higher confidence means *fewer* items for a how-many forecast.
        summary["percentiles"] = {f"p{p}": percentile(results, 100 - p) for p in PERCENTILES}
        label = "items"

    if args.json:
        print(json.dumps(summary, indent=2))
        return

    h = summary["history"]
    print(summary["question"])
    print(f"History: {h['items']} items over {h['days']} days ({h['start']} → {h['end']}), {h['items_per_week']}/week")
    print(f"Runs: {args.runs}\n")
    for k, v in summary["percentiles"].items():
        print(f"  {k[1:]}% confidence: {'not within simulation limit' if v is None else v}")
    print(f"\nDistribution ({label}):\n{histogram(results)}")


if __name__ == "__main__":
    main()
