"""Forecasts, epic plans, calibration, stats and ageing, computed from the store.

Each `*_data` function takes the parsed CLI arguments and returns the JSON-ready answer
(private keys, starting with `_`, carry raw simulation samples for the HTML report).
Problems the user can fix raise UserError; assertions guard internal invariants.
"""

from __future__ import annotations

import json
import math
import random
from argparse import Namespace
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import store
from forecast import (
    PERCENTILES,
    Plan,
    Sim,
    daily_throughput,
    percentile,
    simulate_how_many,
    simulate_priority,
    simulate_when,
    when_date,
)
from store import Connection, SavedForecast, Selection, Window

MIN_TYPE_SAMPLE = 10  # fewer completions of a type than this: compare its open items with all types
LOW_HISTORY = 20  # fewer completions than this in the window: warn that the forecast is shaky


class UserError(Exception):
    """Something the user can fix; the message says how."""


def window_for(con: Connection, scope: str, days: int, end: date | None = None) -> Window:
    """The history window, or a UserError saying to sync first."""
    try:
        window = store.last_days(con, scope, days, end)
    except store.NoData:
        raise UserError(f"no data for scope {scope!r}; run `ingest {scope} <file>` first") from None
    # Don't count days before the data starts as zero-throughput days: they're unknown.
    first = store.first_completion(con, scope)
    if first is not None and window.start < first <= window.end:
        window = Window(first, window.end)
    assert window.days <= days, f"{window.days}-day window for --window {days}"
    assert end is None or window.end == end, f"window ends {window.end}, asked for {end}"
    return window


def chance(results: Sequence[float], ok: Callable[[float], bool]) -> float:
    """Share of simulated runs for which `ok` holds, to 3 decimals."""
    assert results, "no simulated runs to take a chance from"
    share = round(sum(map(ok, results)) / len(results), 3)
    assert 0 <= share <= 1, f"chance {share} outside 0..1"
    return share


def target_days(start: date, target: date | None) -> int | None:
    """Days from start to a target date, or None without a target."""
    if target is None:
        return None
    if target <= start:
        raise UserError(f"--target-date {target} must be after the forecast start {start}")
    days = (target - start).days
    assert days > 0, f"target {target} is {days} days from start {start}"
    assert start + timedelta(days=days) == target, f"{days} days from {start} isn't {target}"
    return days


def percentile_dates(start: date, results: Sequence[float]) -> dict[str, str | None]:
    """The pN finish dates of simulated runs (None where runs never finished)."""
    dates = {f"p{p}": when_date(start, percentile(results, p)) for p in PERCENTILES}
    known = [d for d in dates.values() if d]
    assert known == sorted(known), f"percentile dates out of order: {dates}"
    assert len(dates) == len(PERCENTILES), f"every reported percentile {PERCENTILES} needs a date, got {dates}"
    return dates


# --- backlog forecast -----------------------------------------------------------------


@dataclass(frozen=True)
class History:
    """Daily completions and arrivals over a window, for one selection of issues."""

    selection: Selection
    window: Window
    throughput: list[int]
    arrivals: list[int]
    completed: int  # items finished in the window
    created: int  # items created in the window (new work arriving)

    def __post_init__(self) -> None:
        assert len(self.throughput) == len(self.arrivals) == self.window.days, (
            f"{len(self.throughput)} days of throughput, {len(self.arrivals)} of arrivals for {self.window}"
        )
        assert (self.completed, self.created) == (sum(self.throughput), sum(self.arrivals)), (
            f"totals {self.completed}/{self.created} don't match the daily series"
        )


def window_total(daily: Sequence[int], what: str) -> int:
    """Sum of a daily series, checked for sanity."""
    total = sum(daily)
    assert total >= max(daily, default=0), f"{total} {what} but one day alone had {max(daily)}"
    assert total <= 10**7, f"{total} {what} in {len(daily)} days is not a real team"
    return total


def load_history(con: Connection, sel: Selection, window: Window) -> History:
    """Daily completions and arrivals in the window."""
    throughput = daily_throughput(store.event_dates(con, sel, "resolved"), window.start, window.end)
    arrivals = daily_throughput(store.event_dates(con, sel, "created"), window.start, window.end)
    history = History(sel, window, throughput, arrivals,
                      window_total(throughput, "completions"), window_total(arrivals, "arrivals"))
    assert history.completed >= 0, f"negative completions {history.completed}"
    assert history.created >= 0, f"negative arrivals {history.created}"
    return history


def history_summary(h: History) -> dict:
    """The window and its weekly rates, as reported with every forecast."""
    days, completed, created = h.window.days, h.completed, h.created
    done_wk, new_wk = round(completed / days * 7, 2), round(created / days * 7, 2)
    assert done_wk <= completed * 7, f"weekly rate {done_wk} from {completed} completions in {days} days"
    assert new_wk <= created * 7, f"weekly rate {new_wk} from {created} arrivals in {days} days"
    return h.window.as_json() | {"days": days, "completed": completed, "created": created,
                                 "completed_per_week": done_wk, "created_per_week": new_wk}


def forecast_warnings(h: History, when: bool) -> list[str]:
    """Plain-language caveats about the history behind a forecast."""
    warnings = []
    if h.completed < LOW_HISTORY:
        warnings.append(f"only {h.completed} completions in history; low confidence")
    if when and h.created >= h.completed:
        warnings.append(
            "items are being created at least as fast as they are completed, so the backlog isn't shrinking; "
            "this date covers today's backlog only, so forecast again as new work arrives"
        )
    assert len(warnings) <= 2, f"unexpected warnings {warnings}"
    assert all(w and w[0].islower() for w in warnings), f"warnings are lower-case sentence fragments, got {warnings}"
    return warnings


@dataclass(frozen=True)
class Run:
    """What every model in one forecast shares: its history, start date, and random source."""

    history: History
    start: date
    rng: random.Random

    def __post_init__(self) -> None:
        assert self.start >= self.history.window.start, f"forecast starts {self.start}, before its history"
        assert self.history.completed >= 0, f"{self.history.completed} completions in the history"


def how_many_forecast(con: Connection, args: Namespace, run: Run) -> dict:
    """How many items will be done by args.by."""
    horizon = (args.by - run.start).days
    if horizon <= 0:
        raise UserError(f"--by {args.by} must be after the forecast start {run.start}")
    results = simulate_how_many(run.history.throughput, horizon, Sim(args.runs, run.rng))
    # Higher confidence means *fewer* items for a how-many forecast.
    pct = {f"p{p}": percentile(results, 100 - p) for p in PERCENTILES}
    out: dict = {"kind": "how_many", "target_date": args.by.isoformat(), "percentiles": pct, "_samples": {"items": results}}
    if args.at_least is not None:
        out |= {"at_least": args.at_least, "chance": chance(results, lambda r: r >= args.at_least)}
    if not args.no_record:
        rec = SavedForecast(run.history.selection, "how_many", run.history.window, run.start, pct, target_date=args.by)
        out["forecast_id"] = store.save_forecast(con, rec, datetime.now())
    assert pct["p95"] <= pct["p50"], f"95% confidence must promise no more than 50%: {pct}"
    assert len(results) == args.runs, f"{len(results)} results for {args.runs} runs"
    return out


def when_forecast(con: Connection, args: Namespace, run: Run) -> dict:
    """When today's open backlog (or args.items) will be done. New work isn't modelled: forecast again when it arrives."""
    keys = store.open_keys(con, run.history.selection)
    items = args.items if args.items is not None else len(keys)
    if items <= 0:
        raise UserError("nothing is open in this scope, so there is nothing to forecast")
    deadline = target_days(run.start, args.target_date)
    results = simulate_when(run.history.throughput, items, Sim(args.runs, run.rng, args.max_days))
    pct = percentile_dates(run.start, results)
    finished = round(sum(r != math.inf for r in results) / len(results), 4)
    out: dict = {"kind": "when", "items": items, "percentiles": pct, "finished_within_limit": finished,
                 "max_days": args.max_days, "_samples": {"days": results}}
    if deadline is not None:
        out |= {"target_date": args.target_date.isoformat(), "chance": chance(results, lambda r: r <= deadline)}
    if not args.no_record:
        rec = SavedForecast(run.history.selection, "when", run.history.window, run.start, pct,
                            items=items, keys=keys if args.items is None else ())
        out["forecast_id"] = store.save_forecast(con, rec, datetime.now())
    assert 0 <= finished <= 1, f"share of runs finishing is {finished}"
    assert len(results) == args.runs, f"{len(results)} results for {args.runs} runs"
    return out


def forecast_data(con: Connection, args: Namespace) -> dict:
    """A backlog forecast: when (default) or how many by a date (args.by)."""
    window = window_for(con, args.scope, args.window, args.history_end)
    sel = Selection(args.scope, tuple(args.type) if args.type else None)
    history = load_history(con, sel, window)
    if args.start and args.start < window.start:
        raise UserError(f"--start {args.start} is before the history window ({window.start} to {window.end})")
    run = Run(history, args.start or window.end, random.Random(args.seed))
    out: dict = {"scope": args.scope, "types": list(sel.types) if sel.types else None, "history": history_summary(history),
                 "start": run.start.isoformat(), "runs": args.runs}
    if warnings := forecast_warnings(history, when=not args.by):
        out["warnings"] = warnings
    out |= how_many_forecast(con, args, run) if args.by else when_forecast(con, args, run)
    assert out["kind"] in ("when", "how_many"), f"unknown forecast kind {out['kind']}"
    assert "_samples" in out, "the report needs the raw samples"
    return out


# --- epics ------------------------------------------------------------------------------


@dataclass(frozen=True)
class EpicRun:
    """What every epic's forecast shares."""

    window: Window
    start: date
    rng: random.Random
    team: list[int]  # whole-team daily completions
    deadline: int | None

    def __post_init__(self) -> None:
        assert len(self.team) == self.window.days, f"{len(self.team)} days of team history for {self.window}"
        assert self.deadline is None or self.deadline > 0, f"deadline {self.deadline} days from start"


def epic_forecast(con: Connection, args: Namespace, run: EpicRun, group: tuple[str, list[str], str]) -> dict:
    """One epic: at its own current pace, and if the whole team worked only on it."""
    epic, keys, status = group
    own = daily_throughput(store.epic_completion_dates(con, args.scope, epic), run.window.start, run.window.end)
    e: dict = {"epic": epic, "status": status, "open": len(keys), "completed_in_window": sum(own),
               "current_pace": None, "sole_focus": None}
    remaining, deadline = len(keys), run.deadline
    for model, history in (("current_pace", own), ("sole_focus", run.team)):
        if not remaining or not any(history):
            continue
        results = simulate_when(history, remaining, Sim(args.runs, run.rng, args.max_days))
        e[model] = percentile_dates(run.start, results)
        if deadline is not None:
            e.setdefault("chance", {})[model] = chance(results, lambda r: r <= deadline)
    if e["current_pace"] and not args.no_record:
        rec = SavedForecast(Selection(args.scope), "when", run.window, run.start, e["current_pace"],
                            items=remaining, keys=keys, epic=epic)
        e["forecast_id"] = store.save_forecast(con, rec, datetime.now())
    assert e["current_pace"] is None or keys, f"{epic} has a pace forecast but nothing open"
    assert e["completed_in_window"] <= sum(run.team), f"{epic} finished more than the whole team"
    return e


def plan_share(con: Connection, args: Namespace, window: Window) -> float:
    """Share of team throughput the plan gets: --epic-share, else what epics got recently."""
    share = args.epic_share if args.epic_share is not None else store.epic_share(con, args.scope, window)
    if not 0 <= share <= 1:
        raise UserError(f"--epic-share must be between 0 and 1 (e.g. 0.5), got {share}")
    if args.wip < 1:
        raise UserError(f"--wip must be at least 1, got {args.wip}")
    assert 0 <= share <= 1, f"share {share} escaped validation"
    assert args.wip >= 1, f"wip {args.wip} escaped validation"
    return share


def plan_forecast(con: Connection, args: Namespace, run: EpicRun, epics: list[dict]) -> dict:
    """Epics worked in args.order, args.wip at a time; adds each listed epic's 'priority' dates."""
    order = [k.strip() for k in ",".join(args.order).split(",") if k.strip()]
    by_key = {e["epic"]: e for e in epics}
    if unknown := [k for k in order if k not in by_key]:
        raise UserError(f"not open epics in scope: {', '.join(unknown)}")
    share = plan_share(con, args, run.window)
    priority: dict = {"order": order, "wip": args.wip, "epic_share": round(share, 3)}
    if share == 0:
        priority["note"] = (
            "no epic work was completed in the history window, so there is no capacity to plan with; "
            "pass --epic-share to say how much of the team's time epics will get"
        )
        return priority
    sim = Sim(args.runs, run.rng, args.max_days)
    finish = simulate_priority(run.team, [by_key[k]["open"] for k in order], sim, Plan(share, args.wip))
    deadline = run.deadline
    for key, results in zip(order, finish):
        by_key[key]["priority"] = percentile_dates(run.start, results)
        if deadline is not None:
            by_key[key].setdefault("chance", {})["priority"] = chance(results, lambda d: d <= deadline)
    undated = [k for k in order if "priority" not in by_key[k]]
    assert not undated, f"planned epics without plan dates: {undated}"
    assert len(order) == len(set(order)), f"an epic is listed twice in the order: {order}"
    return priority


def epics_data(con: Connection, args: Namespace) -> dict:
    """Per-epic forecasts, plus a priority plan when args.order is given."""
    window = window_for(con, args.scope, args.window, args.history_end)
    start = args.start or window.end
    team = daily_throughput(store.event_dates(con, Selection(args.scope), "resolved"), window.start, window.end)
    run = EpicRun(window, start, random.Random(args.seed), team, target_days(start, args.target_date))
    statuses = store.epic_statuses(con, args.scope)
    groups = [(epic, keys, statuses.get(epic, "not synced")) for epic, keys in store.epic_groups(con, args.scope, args.epic)]
    epics = [epic_forecast(con, args, run, g) for g in groups]
    out: dict = {
        "scope": args.scope,
        "history": window.as_json() | {"team_completed": sum(team)},
        "start": start.isoformat(),
        "epics": epics,
        "open_without_epic": store.open_without_epic(con, args.scope),
    }
    if args.order:
        out["priority"] = plan_forecast(con, args, run, epics)
    if run.deadline is not None:
        out["target_date"] = args.target_date.isoformat()
    assert [e["epic"] for e in epics] == [g[0] for g in groups], "epics out of order"
    assert out["open_without_epic"] >= 0, f"negative unparented count {out['open_without_epic']}"
    return out


# --- calibration --------------------------------------------------------------------------


def hits_for_date(actual: date | None, pct: dict, synced_to: date) -> dict[str, bool | None]:
    """Per percentile date: held (actual on or before it), missed, or pending (not known yet)."""
    hits: dict[str, bool | None] = {}
    for k, v in pct.items():
        promised = date.fromisoformat(v) if v else None
        if actual is not None:
            hits[k] = promised is not None and actual <= promised
        elif promised is not None and synced_to > promised:
            hits[k] = False  # the date has passed and the work isn't done
        else:
            hits[k] = None
    assert set(hits) == set(pct), f"hits {sorted(hits)} for percentiles {sorted(pct)}"
    assert actual is None or None not in hits.values(), f"actual {actual} known but some hits pending: {hits}"
    return hits


def evaluate_how_many(con: Connection, f: dict, pct: dict, synced_to: date) -> dict:
    """A how-many forecast holds at pN if at least the pN count was actually done by its target date."""
    if synced_to < f["target_date"]:
        return {"actual": None, "hits": dict.fromkeys(pct)}
    actual = store.resolved_between(con, f)
    hits = {k: actual >= v for k, v in pct.items()}
    assert actual >= 0, f"negative actual {actual}"
    assert set(hits) == set(pct), f"hits {sorted(hits)} for percentiles {sorted(pct)}"
    return {"actual": actual, "hits": hits}


def evaluate_when(con: Connection, f: dict, pct: dict, synced_to: date) -> dict:
    """A when forecast holds at pN if its backlog was cleared by the pN date."""
    if f["scope_growth"]:
        return {"actual": None, "hits": dict.fromkeys(pct), "note": "retired scope-growth model; not scored"}
    total, still_open, last = store.tracked_items(con, f)
    if not total:
        return {"actual": None, "hits": dict.fromkeys(pct), "note": "explicit --items; not trackable"}
    actual = last if still_open == 0 else None
    hits = hits_for_date(actual, pct, synced_to)
    assert actual is None or actual >= f["start"], f"cleared {actual}, before the forecast started {f['start']}"
    assert set(hits) == set(pct), f"hits {sorted(hits)} for percentiles {sorted(pct)}"
    return {"actual": actual.isoformat() if actual else None, "hits": hits}


def model_name(f: dict) -> str:
    """How a stored forecast is grouped in the calibration summary."""
    if f["epic"]:
        name = "epic/current_pace"
    elif f["kind"] == "how_many":
        name = "how_many"
    else:
        name = "when (retired scope-growth model)" if f["scope_growth"] else "when"
    assert f["kind"] in ("when", "how_many"), f"unknown forecast kind {f['kind']!r}"
    assert not f["epic"] or f["kind"] == "when", f"epic forecast {f['id']} isn't a when forecast"
    return name


def calibrate_data(con: Connection, args: Namespace) -> dict:
    """Every recorded forecast scored against what happened, and the hit rate per model and percentile."""
    detail, summary = [], {}
    for f in store.recorded_forecasts(con, args.scope):
        pct = json.loads(f["percentiles"])
        synced_to = f["last_sync"].date()
        result = (evaluate_how_many if f["kind"] == "how_many" else evaluate_when)(con, f, pct, synced_to)
        model = model_name(f)
        detail.append({"id": f["id"], "scope": f["scope"], "epic": f["epic"], "made_at": f["made_at"].isoformat(),
                       "model": model} | result)
        for k, hit in result["hits"].items():
            tally = summary.setdefault(model, {}).setdefault(k, {"held": 0, "missed": 0, "pending": 0})
            tally["held" if hit else "pending" if hit is None else "missed"] += 1
    counted = sum(t["held"] + t["missed"] + t["pending"] for by_p in summary.values() for t in by_p.values())
    assert counted == sum(len(d["hits"]) for d in detail), f"tallied {counted} hits for {len(detail)} forecasts"
    unsummarised = [d["id"] for d in detail if d["hits"] and d["model"] not in summary]
    assert not unsummarised, f"forecasts missing from the summary: {unsummarised}"
    return {"summary": summary, "forecasts": detail}


# --- stats --------------------------------------------------------------------------------


def stats_data(con: Connection, args: Namespace) -> dict:
    """Weekly completions and arrivals, lead time by type, and recent backlog snapshots."""
    window = window_for(con, args.scope, args.window)
    quantiles = store.lead_time_quantiles(con, args.scope, window)
    open_counts = store.open_by_type(con, args.scope)
    finished_types = {t for t, *_ in quantiles}
    weekly = [
        # `days`: how many of the week's 7 days fall inside the window (edge weeks are partial).
        {"week": w.isoformat(), "completed": c, "created": n,
         "days": (min(w + timedelta(days=6), window.end) - max(w, window.start)).days + 1}
        for w, c, n in store.weekly_counts(con, args.scope, window)
    ]
    by_type = [
        {"type": t, "completed": c, "open": open_counts.get(t, 0), "lead_time_days": {"p50": a, "p85": b, "p95": d}}
        for t, c, a, b, d in quantiles
    ] + [
        {"type": t, "completed": 0, "open": n, "lead_time_days": None} for t, n in open_counts.items() if t not in finished_types
    ]
    snaps = store.snapshots(con, args.scope, args.snapshots)
    assert all(1 <= w["days"] <= 7 for w in weekly), f"a week covers {[w['days'] for w in weekly]} days"
    assert sum(t["open"] for t in by_type) == sum(open_counts.values()), "open counts lost a type"
    return {
        "scope": args.scope,
        "window": list(window.as_json().values()),
        "weekly": weekly,
        "by_type": by_type,
        "snapshots": [{"taken_at": t.isoformat(), "open": o, "done": d} for t, o, d in snaps],
    }


# --- ageing -------------------------------------------------------------------------------


def classify_age(age: int, sample: Sequence[int]) -> dict:
    """Where an open item's age sits against finished items' lead times."""
    assert sample, "need finished items to compare an age against"
    assert sample == sorted(sample), "lead-time sample must be sorted"
    p85, p95 = percentile(sample, 85), percentile(sample, 95)
    assert p85 <= p95, f"85th percentile lead time {p85} above the 95th {p95}"
    risk = "stale" if age > p95 else "at risk" if age > p85 else "ok"
    older_than = round(sum(d < age for d in sample) / len(sample) * 100)
    return {"lead_time_p85": p85, "lead_time_p95": p95, "risk": risk, "older_than_pct_of_completed": older_than}


def aging_data(con: Connection, args: Namespace) -> dict:
    """Open items whose age already exceeds what most completed items of their type took."""
    window = window_for(con, args.scope, args.window)
    by_type = store.lead_times(con, args.scope, window)
    overall = sorted(d for v in by_type.values() for d in v)
    if not overall:
        raise UserError(
            "no items were completed in the history window, so there is nothing to compare open items "
            "against; widen --window or sync more history (a full sync fetches the last 90 days)"
        )
    rows = []
    for key, itype, epic, category, age in store.open_item_ages(con, args.scope, window.end):
        own = by_type.get(itype) or []
        basis = itype if len(own) >= MIN_TYPE_SAMPLE else "all types"
        rows.append({"key": key, "type": itype, "epic": epic, "status": category, "age_days": age, "basis": basis}
                    | classify_age(age, own if basis == itype else overall))
    flagged = [r for r in rows if r["risk"] != "ok"]
    risks = [r["risk"] for r in rows]
    out = {
        "scope": args.scope,
        "as_of": window.end.isoformat(),
        "window": list(window.as_json().values()),
        "open": len(rows),
        "at_risk": risks.count("at risk"),
        "stale": risks.count("stale"),
        "items": rows if args.all else flagged,
    }
    assert out["at_risk"] + out["stale"] == len(flagged), f"{len(flagged)} flagged but counts say {out['at_risk']}+{out['stale']}"
    assert len(out["items"]) <= out["open"], f"{len(out['items'])} items listed of {out['open']} open"
    return out
