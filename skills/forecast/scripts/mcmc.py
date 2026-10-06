#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["duckdb>=1.1"]
# ///
"""Jira history store + Monte Carlo forecasts with scope growth and calibration.

Subcommands:
  sync-info SCOPE        what to fetch next (full or incremental, and since when)
  ingest SCOPE FILE...   upsert issues fetched from Jira, snapshot the backlog
  forecast SCOPE         simulate from stored history and record the forecast
  epics SCOPE            per-epic forecasts: at current pace, and if it had the team's sole focus
  calibrate [SCOPE]      score past forecasts against what actually happened
  stats SCOPE            weekly throughput/arrivals, lead time, backlog snapshots
  aging SCOPE            open items older than their type's usual lead time
  report SCOPE           all of the above as one self-contained HTML page, kept in reports/
  reports [SCOPE]        list saved reports (newest first) and the index page

DB: --db, else $MCMC_DB, else $CLAUDE_PLUGIN_DATA/mcmc.duckdb, else
~/.local/share/mcmc/mcmc.duckdb.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import random
import sys
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).parent))
from forecast import (  # noqa: E402
    PERCENTILES,
    daily_throughput,
    histogram,
    percentile,
    simulate_how_many,
    simulate_priority,
    simulate_when,
    when_date,
)

FULL_SYNC_EVERY = timedelta(days=7)

SCHEMA = """
CREATE TABLE IF NOT EXISTS scopes (
    name TEXT PRIMARY KEY,
    jql TEXT,
    created_at TIMESTAMP,
    last_sync TIMESTAMP,
    last_full_sync TIMESTAMP
);
CREATE TABLE IF NOT EXISTS issues (
    scope TEXT,
    key TEXT,
    issue_type TEXT,
    created DATE,
    resolved DATE,
    done BOOLEAN,
    synced_at TIMESTAMP,
    epic TEXT,
    status_category TEXT,   -- 'to_do' | 'in_progress' | 'done'
    PRIMARY KEY (scope, key)
);
CREATE TABLE IF NOT EXISTS snapshots (
    scope TEXT,
    taken_at TIMESTAMP,
    open_items INTEGER,
    done_items INTEGER
);
CREATE SEQUENCE IF NOT EXISTS forecast_id;
CREATE TABLE IF NOT EXISTS forecasts (
    id INTEGER PRIMARY KEY DEFAULT nextval('forecast_id'),
    scope TEXT,
    made_at TIMESTAMP,
    kind TEXT,              -- 'when' | 'how_many'
    scope_growth BOOLEAN,
    start DATE,
    target_date DATE,       -- how_many only
    items INTEGER,          -- when only
    types TEXT[],
    history_start DATE,
    history_end DATE,
    percentiles JSON,       -- {"p50": "2026-11-02" | null | 12, ...}
    epic TEXT               -- per-epic forecasts only
);
CREATE TABLE IF NOT EXISTS forecast_items (forecast_id INTEGER, key TEXT);
CREATE TABLE IF NOT EXISTS reports (
    scope TEXT,
    created_at TIMESTAMP,
    path TEXT,
    as_of DATE,
    open_items INTEGER,
    p85 DATE,
    shrinking BOOLEAN,
    target_date DATE,
    chance DOUBLE
);
-- Columns added after the first release; no-ops on fresh DBs.
ALTER TABLE issues ADD COLUMN IF NOT EXISTS epic TEXT;
ALTER TABLE issues ADD COLUMN IF NOT EXISTS status_category TEXT;
ALTER TABLE forecasts ADD COLUMN IF NOT EXISTS epic TEXT;
"""


def public(out: dict) -> dict:
    """Drop private keys (raw simulation samples etc.) before printing JSON."""
    return {k: v for k, v in out.items() if not k.startswith("_")}


@contextmanager
def transaction(con):
    """All the writes inside happen, or none do."""
    con.begin()
    try:
        yield
    except BaseException:
        con.rollback()
        raise
    con.commit()


def default_db() -> Path:
    if env := os.environ.get("MCMC_DB"):
        return Path(env)
    if data := os.environ.get("CLAUDE_PLUGIN_DATA"):
        return Path(data) / "mcmc.duckdb"
    return Path.home() / ".local/share/mcmc/mcmc.duckdb"


def connect(path: Path) -> duckdb.DuckDBPyConnection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    con.execute(SCHEMA)
    return con


# --- ingest -----------------------------------------------------------------


def _field(issue: dict, *names: str):
    for source in (issue, issue.get("fields") or {}):
        for name in names:
            if source.get(name) is not None:
                return source[name]
    return None


def _day(value) -> date | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value).strip().replace("Z", "+00:00")[:10]).date()


# Jira status category keys (new/indeterminate/done) and display names.
_CATEGORIES = {
    "new": "to_do", "to do": "to_do", "todo": "to_do",
    "indeterminate": "in_progress", "in progress": "in_progress",
    "done": "done", "complete": "done",
}


def _epic(issue: dict, epic_field: str | None) -> str | None:
    """Epic key: flat `epic` column, Data Center Epic Link custom field, or Cloud `parent`."""
    value = _field(issue, *([epic_field] if epic_field else []), "epic", "epic_link", "epic_key", "parent")
    if isinstance(value, dict):
        value = value.get("key")
    return str(value) if value else None


def normalise(issue: dict, epic_field: str | None = None) -> dict:
    """Accept raw Jira REST issues ({key, fields: {...}}) or flat records."""
    issue_type = _field(issue, "issue_type", "issuetype", "type")
    subtask = False
    if isinstance(issue_type, dict):
        subtask = bool(issue_type.get("subtask"))
        issue_type = issue_type.get("name")
    subtask = subtask or str(issue_type).lower().replace("-", "") == "subtask"
    status = _field(issue, "status")
    category = _field(issue, "status_category", "statusCategory")
    if category is None and isinstance(status, dict):
        category = status.get("statusCategory") or status.get("category")
    if isinstance(category, dict):
        category = category.get("key") or category.get("name")
    category = _CATEGORIES.get(str(category).strip().lower().replace("_", " ")) if category else None
    resolved = _day(_field(issue, "resolved", "resolutiondate", "resolution_date"))
    done = _field(issue, "done")
    if done is None:
        done = resolved is not None or category == "done"
    return {
        "key": issue["key"],
        "issue_type": issue_type,
        "created": _day(_field(issue, "created")),
        "resolved": resolved,
        "done": str(done).lower() not in ("false", "0", "") if isinstance(done, str) else bool(done),
        "subtask": subtask,
        "epic": _epic(issue, epic_field),
        "status_category": "done" if done else category,
    }


def load_issues(raw: str, epic_field: str | None = None) -> list[dict]:
    """JSON list, {'issues': [...]}, JSON lines, or CSV with a header row
    (key,type,created,resolved,status_category)."""
    raw = raw.strip()
    if not raw:
        return []
    if raw[0] in "[{":
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = [json.loads(line) for line in raw.splitlines() if line.strip()]
    else:
        data = [{k.strip(): (v.strip() or None) for k, v in row.items()} for row in csv.DictReader(io.StringIO(raw))]
    if isinstance(data, dict):
        data = data.get("issues") or data.get("values") or [data]
    return [normalise(i, epic_field) for i in data]


def missing_epics(con, scope: str) -> list[str]:
    """Epics referenced by synced issues whose own issue isn't synced (e.g. resolved before the window)."""
    return [
        r[0]
        for r in con.execute(
            """SELECT DISTINCT epic FROM issues WHERE scope = ? AND epic IS NOT NULL
               AND epic NOT IN (SELECT key FROM issues WHERE scope = ?) ORDER BY epic""",
            [scope, scope],
        ).fetchall()
    ]


def cmd_ingest(con, args) -> None:
    issues = [
        issue
        for f in args.files
        for issue in load_issues(sys.stdin.read() if f == "-" else Path(f).read_text(), args.epic_field)
    ]
    subtasks = sum(i["subtask"] for i in issues)
    if not args.include_subtasks:
        issues = [i for i in issues if not i["subtask"]]
    with transaction(con):
        result = ingest(con, args.scope, issues, jql=args.jql, full=args.full, now=args.as_of or datetime.now())
    print(json.dumps({"scope": args.scope, "ingested": len(issues),
                      "skipped_subtasks": 0 if args.include_subtasks else subtasks} | result))


def ingest(con, scope: str, issues: list[dict], *, jql: str | None, full: bool, now: datetime) -> dict:
    """Upsert one sync's issues, record the sync, and snapshot the backlog. Call inside a transaction."""
    con.execute("INSERT INTO scopes VALUES (?, ?, ?, NULL, NULL) ON CONFLICT DO NOTHING", [scope, jql, now])
    if jql:
        con.execute("UPDATE scopes SET jql = ? WHERE name = ?", [jql, scope])
    if issues:
        con.executemany(
            "INSERT OR REPLACE INTO issues "
            "(scope, key, issue_type, created, resolved, done, synced_at, epic, status_category) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                [scope, i["key"], i["issue_type"], i["created"], i["resolved"], i["done"], now, i["epic"],
                 i["status_category"]]
                for i in issues
            ],
        )
    removed = 0
    if full:
        # A full sync returns every open item, so open items we didn't see have left the scope.
        seen = [i["key"] for i in issues]
        removed = con.execute(
            "SELECT count(*) FROM issues WHERE scope = ? AND NOT done AND NOT list_contains(?, key)", [scope, seen]
        ).fetchone()[0]
        con.execute("DELETE FROM issues WHERE scope = ? AND NOT done AND NOT list_contains(?, key)", [scope, seen])
        con.execute("UPDATE scopes SET last_full_sync = ? WHERE name = ?", [now, scope])
    con.execute("UPDATE scopes SET last_sync = ? WHERE name = ?", [now, scope])
    open_items, done_items = con.execute(
        "SELECT count(*) FILTER (NOT done), count(*) FILTER (done) FROM issues WHERE scope = ?", [scope]
    ).fetchone()
    con.execute("INSERT INTO snapshots VALUES (?, ?, ?, ?)", [scope, now, open_items, done_items])
    return {"removed": removed, "open": open_items, "done": done_items, "missing_epics": missing_epics(con, scope)}


def cmd_sync_info(con, args) -> None:
    row = con.execute("SELECT jql, last_sync, last_full_sync FROM scopes WHERE name = ?", [args.scope]).fetchone()
    now = datetime.now()
    if row is None or row[2] is None or now - row[2] > FULL_SYNC_EVERY or row[1] is None:
        mode, since = "full", None
    else:
        mode = "incremental"
        # One day of overlap absorbs Jira/user timezone differences; upserts are idempotent.
        since = (row[1].date() - timedelta(days=1)).isoformat()
    print(
        json.dumps(
            {
                "scope": args.scope,
                "known": row is not None,
                "jql": row[0] if row else None,
                "last_sync": row[1].isoformat() if row and row[1] else None,
                "last_full_sync": row[2].isoformat() if row and row[2] else None,
                "mode": mode,
                "updated_since": since,
                "missing_epics": missing_epics(con, args.scope),
            }
        )
    )


# --- forecast ---------------------------------------------------------------


# Every query below is fixed text with bound parameters. Optional filters bind NULL to switch
# themselves off, e.g. the type filter: with no types given, epics (containers, not deliverable
# items) are excluded; otherwise only the listed types count. Bind it with `types_param`.
#   (CASE WHEN ?::TEXT[] IS NULL THEN coalesce(issue_type, '') <> 'Epic'
#         ELSE list_contains(?::TEXT[], issue_type) END)


def types_param(types: list[str] | None) -> list:
    """The two bindings for the type filter above."""
    return [types or None, types or None]


def history_window(con, scope: str, window: int, end: date | None) -> tuple[date, date]:
    if end is None:
        last = con.execute("SELECT last_sync FROM scopes WHERE name = ?", [scope]).fetchone()
        if not last or last[0] is None:
            sys.exit(f"error: no data for scope {scope!r}; ingest first")
        end = last[0].date()
    return end - timedelta(days=window - 1), end


DAILY_SERIES = {
    "resolved": """SELECT resolved FROM issues WHERE scope = ? AND resolved IS NOT NULL
                   AND (CASE WHEN ?::TEXT[] IS NULL THEN coalesce(issue_type, '') <> 'Epic'
                             ELSE list_contains(?::TEXT[], issue_type) END)""",
    "created": """SELECT created FROM issues WHERE scope = ? AND created IS NOT NULL
                  AND (CASE WHEN ?::TEXT[] IS NULL THEN coalesce(issue_type, '') <> 'Epic'
                            ELSE list_contains(?::TEXT[], issue_type) END)""",
}


def daily_series(con, scope: str, column: str, start: date, end: date, types) -> list[int]:
    """Per-day counts of issues `resolved` (throughput) or `created` (arrivals) in the window."""
    dates = [r[0] for r in con.execute(DAILY_SERIES[column], [scope, *types_param(types)]).fetchall()]
    return daily_throughput(dates, start, end)


def open_keys(con, scope: str, types) -> list[str]:
    rows = con.execute(
        """SELECT key FROM issues WHERE scope = ? AND NOT done
           AND (CASE WHEN ?::TEXT[] IS NULL THEN coalesce(issue_type, '') <> 'Epic'
                     ELSE list_contains(?::TEXT[], issue_type) END)""",
        [scope, *types_param(types)],
    ).fetchall()
    return [r[0] for r in rows]


def record(con, scope, kind, growth, start, target, items, types, h_start, h_end, pct, keys=(), epic=None) -> int:
    with transaction(con):
        return _record(con, scope, kind, growth, start, target, items, types, h_start, h_end, pct, keys, epic)


def _record(con, scope, kind, growth, start, target, items, types, h_start, h_end, pct, keys, epic) -> int:
    fid = con.execute(
        """INSERT INTO forecasts (scope, made_at, kind, scope_growth, start, target_date, items, types,
                                  history_start, history_end, percentiles, epic)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
        [scope, datetime.now(), kind, growth, start, target, items, types, h_start, h_end, json.dumps(pct), epic],
    ).fetchone()[0]
    if keys:
        con.executemany("INSERT INTO forecast_items VALUES (?, ?)", [[fid, k] for k in keys])
    return fid


def chance(results: list[float], ok) -> float:
    return round(sum(map(ok, results)) / len(results), 3)


def target_days(start: date, target: date | None) -> int | None:
    if target is None:
        return None
    if target <= start:
        sys.exit("error: --target-date must be after the forecast start")
    return (target - start).days


def forecast_data(con, args) -> dict:
    h_start, h_end = history_window(con, args.scope, args.window, args.history_end)
    types = args.type or None
    throughput = daily_series(con, args.scope, "resolved", h_start, h_end, types)
    arrivals = daily_series(con, args.scope, "created", h_start, h_end, types)
    rng = random.Random(args.seed)
    start = args.start or h_end
    days = len(throughput)
    out: dict = {
        "scope": args.scope,
        "types": types,
        "history": {
            "start": h_start.isoformat(),
            "end": h_end.isoformat(),
            "days": days,
            "completed": sum(throughput),
            "created": sum(arrivals),
            "completed_per_week": round(sum(throughput) / days * 7, 2),
            "created_per_week": round(sum(arrivals) / days * 7, 2),
        },
        "start": start.isoformat(),
        "runs": args.runs,
    }
    warnings = []
    if sum(throughput) < 20:
        warnings.append(f"only {sum(throughput)} completions in history; low confidence")
    if not args.by and not args.no_scope_growth and sum(arrivals) >= sum(throughput):
        warnings.append("items are being created at least as fast as they are completed; the backlog is not shrinking")
    if warnings:
        out["warnings"] = warnings

    distributions = out["_samples"] = {}
    if args.by:
        horizon = (args.by - start).days
        if horizon <= 0:
            sys.exit("error: --by must be after the forecast start")
        results = simulate_how_many(throughput, horizon, args.runs, rng)
        pct = {f"p{p}": percentile(results, 100 - p) for p in PERCENTILES}
        out.update(kind="how_many", target_date=args.by.isoformat(), percentiles=pct)
        distributions["items"] = results
        if args.at_least is not None:
            out["at_least"] = args.at_least
            out["chance"] = chance(results, lambda r: r >= args.at_least)
        if not args.no_record:
            out["forecast_id"] = record(
                con, args.scope, "how_many", False, start, args.by, None, types, h_start, h_end, pct
            )
    else:
        keys = open_keys(con, args.scope, types)
        items = args.items if args.items is not None else len(keys)
        if items <= 0:
            sys.exit("error: nothing remaining in scope")
        out.update(kind="when", items=items, percentiles={}, finished_within_limit={})
        if (deadline := target_days(start, args.target_date)) is not None:
            out.update(target_date=args.target_date.isoformat(), chance={})
        models = [("no_growth", None)] + ([] if args.no_scope_growth else [("scope_growth", arrivals)])
        for name, arr in models:
            results = simulate_when(throughput, items, args.runs, rng, arrivals=arr, max_days=args.max_days)
            pct = {f"p{p}": when_date(start, percentile(results, p)) for p in PERCENTILES}
            out["percentiles"][name] = pct
            out["finished_within_limit"][name] = round(sum(r != math.inf for r in results) / len(results), 4)
            distributions[f"days ({name})"] = results
            if deadline is not None:
                out["chance"][name] = chance(results, lambda r: r <= deadline)
            if not args.no_record:
                out.setdefault("forecast_ids", {})[name] = record(
                    con, args.scope, "when", arr is not None, start, None, items, types, h_start, h_end, pct,
                    keys if args.items is None else (),
                )
        out["max_days"] = args.max_days

    return out


def cmd_forecast(con, args) -> None:
    out = forecast_data(con, args)
    if args.json:
        print(json.dumps(public(out), indent=2, default=str))
        return
    types, start, distributions = out["types"], out["start"], out["_samples"]
    h = out["history"]
    print(f"Scope {args.scope}" + (f" (types: {', '.join(types)})" if types else ""))
    print(
        f"History {h['start']} → {h['end']}: {h['completed']} completed ({h['completed_per_week']}/wk), "
        f"{h['created']} created ({h['created_per_week']}/wk)"
    )
    for w in out.get("warnings", []):
        print(f"WARNING: {w}")
    if out["kind"] == "how_many":
        print(f"\nItems completed between {start} and {args.by}:")
        for k, v in out["percentiles"].items():
            print(f"  {k[1:]}% confidence: at least {v}")
        if "chance" in out:
            print(f"  Chance of at least {args.at_least}: {out['chance']:.0%}")
    else:
        print(f"\nWhen will {out['items']} items be done (from {start})?")
        names = list(out["percentiles"])
        print("  conf  " + "".join(f"{n:>14}" for n in names))
        for p in PERCENTILES:
            row = [out["percentiles"][n][f"p{p}"] or f">{args.max_days}d" for n in names]
            print(f"  {p:>3}%  " + "".join(f"{v:>14}" for v in row))
        if "chance" in out:
            print("  chance by " + str(args.target_date))
            for n in names:
                print(f"    {n}: {out['chance'][n]:.0%}")
        for n in names:
            if out["finished_within_limit"][n] < 1:
                print(f"  {n}: only {out['finished_within_limit'][n]:.0%} of runs finished within {args.max_days} days")
    for label, values in distributions.items():
        print(f"\nDistribution, {label}:\n{histogram(values)}")


# --- epics ------------------------------------------------------------------


def epics_data(con, args) -> dict:
    h_start, h_end = history_window(con, args.scope, args.window, args.history_end)
    start = args.start or h_end
    rng = random.Random(args.seed)
    rows = con.execute(
        """
        SELECT epic, list(key ORDER BY key) FILTER (NOT done) AS open_keys
        FROM issues
        WHERE scope = ? AND epic IS NOT NULL AND coalesce(issue_type, '') <> 'Epic'
          AND (?::TEXT[] IS NULL OR list_contains(?::TEXT[], epic))
        GROUP BY epic
        HAVING count(*) FILTER (NOT done) > 0
            OR epic IN (SELECT key FROM issues WHERE scope = ? AND issue_type = 'Epic' AND NOT done)
        ORDER BY epic""",
        [args.scope, args.epic or None, args.epic or None, args.scope],
    ).fetchall()
    team = daily_series(con, args.scope, "resolved", h_start, h_end, None)
    unparented_open = con.execute(
        "SELECT count(*) FROM issues WHERE scope = ? AND NOT done AND epic IS NULL AND coalesce(issue_type, '') <> 'Epic'",
        [args.scope],
    ).fetchone()[0]

    epic_status = {
        k: ("done" if d else "open")
        for k, d in con.execute("SELECT key, done FROM issues WHERE scope = ? AND issue_type = 'Epic'", [args.scope]).fetchall()
    }
    deadline = target_days(start, args.target_date)
    epics = []
    for epic, keys in rows:
        keys = keys or []
        dates = [
            r[0]
            for r in con.execute(
                "SELECT resolved FROM issues WHERE scope = ? AND epic = ? AND resolved IS NOT NULL", [args.scope, epic]
            ).fetchall()
        ]
        own = daily_throughput(dates, h_start, h_end)
        e = {"epic": epic, "status": epic_status.get(epic, "not synced"), "open": len(keys), "completed_in_window": sum(own), "current_pace": None, "sole_focus": None}
        if keys:
            if any(own):
                results = simulate_when(own, len(keys), args.runs, rng, max_days=args.max_days)
                e["current_pace"] = {f"p{p}": when_date(start, percentile(results, p)) for p in PERCENTILES}
                if deadline is not None:
                    e.setdefault("chance", {})["current_pace"] = chance(results, lambda r: r <= deadline)
                if not args.no_record:
                    e["forecast_id"] = record(
                        con, args.scope, "when", False, start, None, len(keys), None, h_start, h_end,
                        e["current_pace"], keys, epic=epic,
                    )
            if any(team):
                results = simulate_when(team, len(keys), args.runs, rng, max_days=args.max_days)
                e["sole_focus"] = {f"p{p}": when_date(start, percentile(results, p)) for p in PERCENTILES}
                if deadline is not None:
                    e.setdefault("chance", {})["sole_focus"] = chance(results, lambda r: r <= deadline)
        epics.append(e)

    if args.order:
        order = [k.strip() for k in ",".join(args.order).split(",") if k.strip()]
        by_key = {e["epic"]: e for e in epics}
        if unknown := [k for k in order if k not in by_key]:
            sys.exit(f"error: not open epics in scope: {', '.join(unknown)}")
        share = args.epic_share
        if share is None:
            share = con.execute(
                """SELECT count(*) FILTER (epic IS NOT NULL) / greatest(count(*), 1) FROM issues
                   WHERE scope = ? AND resolved BETWEEN ? AND ? AND coalesce(issue_type, '') <> 'Epic'""",
                [args.scope, h_start, h_end],
            ).fetchone()[0]
        results = simulate_priority(
            team, [by_key[k]["open"] for k in order], args.runs, rng, share=share, wip=args.wip, max_days=args.max_days
        )
        for k, r in zip(order, results):
            by_key[k]["priority"] = {f"p{p}": when_date(start, percentile(r, p)) for p in PERCENTILES}
            if deadline is not None:
                by_key[k].setdefault("chance", {})["priority"] = chance(r, lambda d: d <= deadline)
        priority = {"order": order, "wip": args.wip, "epic_share": round(share, 3)}

    out = {
        "scope": args.scope,
        "history": {"start": h_start.isoformat(), "end": h_end.isoformat(), "team_completed": sum(team)},
        "start": start.isoformat(),
        "epics": epics,
        "open_without_epic": unparented_open,
    }
    if args.order:
        out["priority"] = priority
    if deadline is not None:
        out["target_date"] = args.target_date.isoformat()
    return out


def cmd_epics(con, args) -> None:
    out = epics_data(con, args)
    if args.json:
        print(json.dumps(public(out), indent=2, default=str))
        return
    epics, priority, deadline = out["epics"], out.get("priority"), out.get("target_date")
    if not epics:
        print(f"No open epics in scope {args.scope} (were epic links ingested?)")
        return
    h = out["history"]
    print(
        f"Scope {args.scope}: epics from {out['start']}, history {h['start']} → {h['end']} "
        f"({h['team_completed']} team completions)\n"
    )
    head = f"{'epic':<12} {'status':<10} {'open':>4} {'done/window':>11}   {'current pace p50':>16} {'p85':>11}   {'sole focus p85':>14}"
    models_shown = ["current_pace", "sole_focus"] + (["priority"] if args.order else [])
    if args.order:
        epics.sort(key=lambda e: priority["order"].index(e["epic"]) if e["epic"] in priority["order"] else 10**6)
        head += f"   {'priority p85':>12}"
    if deadline is not None:
        head += f"   chance by {args.target_date} ({'/'.join(m.split('_')[0] for m in models_shown)})"
    print(head)
    for e in epics:
        pace, focus = e["current_pace"], e["sole_focus"]
        if not e["open"]:
            p50 = p85 = "done"
        elif pace is None:
            p50, p85 = "no progress", "-"
        else:
            p50, p85 = (pace[k] or f">{args.max_days}d" for k in ("p50", "p85"))
        f85 = (focus["p85"] or f">{args.max_days}d") if focus else "-"
        line = f"{e['epic']:<12} {e['status']:<10} {e['open']:>4} {e['completed_in_window']:>11}   {p50:>16} {p85:>11}   {f85:>14}"
        if args.order:
            prio = e.get("priority")
            line += f"   {(prio['p85'] or f'>{args.max_days}d') if prio else 'paused':>12}"
        if deadline is not None:
            c = e.get("chance", {})
            line += "   " + " ".join(f"{c[m]:>4.0%}" if m in c else "   -" for m in models_shown)
        print(line)
    print(f"\n{out['open_without_epic']} open items have no epic.")
    if stale := [e["epic"] for e in epics if e["status"] == "done" and e["open"]]:
        print(f"Epics marked done but with open children: {', '.join(stale)}")
    print("current pace = resampling the epic's own completions; sole focus = whole team on this epic only.")
    if args.order:
        print(
            f"priority = epics worked in the order above, {args.wip} at a time, with {priority['epic_share']:.0%} "
            "of team throughput going to epic work; unlisted epics paused."
        )


# --- calibrate --------------------------------------------------------------


def actual_when(con, f: dict) -> date | None:
    """Date the forecast's backlog was cleared, or None if not (yet) known."""
    if f["scope_growth"]:
        # Backlog zero: a day on/after start where every in-scope item created by then is resolved by then.
        row = con.execute(
            """
            WITH s AS (
                SELECT * FROM issues WHERE scope = ?
                AND (CASE WHEN ?::TEXT[] IS NULL THEN coalesce(issue_type, '') <> 'Epic'
                          ELSE list_contains(?::TEXT[], issue_type) END)
            ),
            candidates AS (SELECT DISTINCT resolved AS d FROM s WHERE resolved > ?)
            SELECT min(d) FROM candidates
            WHERE NOT EXISTS (
                SELECT 1 FROM s WHERE created <= d AND (resolved IS NULL OR resolved > d) AND NOT (done AND resolved IS NULL)
            )""",
            [f["scope"], *types_param(f["types"]), f["start"]],
        ).fetchone()
        return row[0]
    row = con.execute(
        """SELECT count(*) FILTER (i.key IS NOT NULL AND NOT i.done), max(i.resolved)
           FROM forecast_items fi LEFT JOIN issues i ON i.scope = ? AND i.key = fi.key
           WHERE fi.forecast_id = ?""",
        [f["scope"], f["id"]],
    ).fetchone()
    still_open, last_resolved = row
    return last_resolved if still_open == 0 and last_resolved is not None else None


def actual_how_many(con, f: dict) -> int:
    return con.execute(
        """SELECT count(*) FROM issues WHERE scope = ? AND resolved > ? AND resolved <= ?
           AND (CASE WHEN ?::TEXT[] IS NULL THEN coalesce(issue_type, '') <> 'Epic'
                     ELSE list_contains(?::TEXT[], issue_type) END)""",
        [f["scope"], f["start"], f["target_date"], *types_param(f["types"])],
    ).fetchone()[0]


def evaluate(con, f: dict, synced_to: date) -> dict:
    """Per percentile: True (held), False (missed), or None (pending)."""
    pct = json.loads(f["percentiles"])
    if f["kind"] == "how_many":
        if synced_to < f["target_date"]:
            return {"actual": None, "hits": {k: None for k in pct}}
        actual = actual_how_many(con, f)
        return {"actual": actual, "hits": {k: actual >= v for k, v in pct.items()}}
    if f["kind"] == "when" and not f["scope_growth"]:
        has_items = con.execute("SELECT count(*) FROM forecast_items WHERE forecast_id = ?", [f["id"]]).fetchone()[0]
        if not has_items:
            return {"actual": None, "hits": {k: None for k in pct}, "note": "explicit --items; not trackable"}
    actual = actual_when(con, f)
    hits = {}
    for k, v in pct.items():
        target = date.fromisoformat(v) if v else None
        if actual is not None:
            hits[k] = target is not None and actual <= target
        elif target is not None and synced_to > target:
            hits[k] = False
        else:
            hits[k] = None
    return {"actual": actual.isoformat() if actual else None, "hits": hits}


def calibrate_data(con, args) -> dict:
    rows = con.execute(
        """SELECT f.*, s.last_sync FROM forecasts f JOIN scopes s ON s.name = f.scope
           WHERE ?::TEXT IS NULL OR f.scope = ? ORDER BY f.id""",
        [args.scope, args.scope],
    )
    cols = [c[0] for c in rows.description]
    forecasts = [dict(zip(cols, r)) for r in rows.fetchall()]
    detail, summary = [], {}
    for f in forecasts:
        result = evaluate(con, f, f["last_sync"].date())
        if f["epic"]:
            model = "epic/current_pace"
        elif f["kind"] == "how_many":
            model = "how_many"
        else:
            model = f"when/{'scope_growth' if f['scope_growth'] else 'no_growth'}"
        detail.append(
            {"id": f["id"], "scope": f["scope"], "epic": f["epic"], "made_at": f["made_at"].isoformat(), "model": model}
            | result
        )
        for k, hit in result["hits"].items():
            s = summary.setdefault(model, {}).setdefault(k, {"held": 0, "missed": 0, "pending": 0})
            s["held" if hit else "pending" if hit is None else "missed"] += 1
    out = {"summary": summary, "forecasts": detail}
    return out


def cmd_calibrate(con, args) -> None:
    out = calibrate_data(con, args)
    if args.json:
        print(json.dumps(public(out), indent=2, default=str))
        return
    if not out["forecasts"]:
        print("No recorded forecasts.")
        return
    print("Calibration: share of resolved forecasts where the pN answer held (well calibrated ≈ N%)\n")
    for model, by_p in out["summary"].items():
        print(model)
        for k, s in by_p.items():
            resolved = s["held"] + s["missed"]
            rate = f"{s['held'] / resolved:.0%}" if resolved else "  -"
            print(f"  {k}: {rate:>4} held  ({s['held']}/{resolved} resolved, {s['pending']} pending)")
    print(f"\n{len(out['forecasts'])} forecasts; --json for per-forecast detail")


# --- stats ------------------------------------------------------------------


def stats_data(con, args) -> dict:
    h_start, h_end = history_window(con, args.scope, args.window, None)
    weekly = con.execute(
        """
        WITH weeks AS (SELECT unnest(generate_series(date_trunc('week', ?::DATE), ?::DATE, INTERVAL 7 DAY))::DATE AS week)
        SELECT week,
               (SELECT count(*) FROM issues WHERE scope = ? AND date_trunc('week', resolved) = week) AS completed,
               (SELECT count(*) FROM issues WHERE scope = ? AND date_trunc('week', created) = week) AS created
        FROM weeks ORDER BY week""",
        [h_start, h_end, args.scope, args.scope],
    ).fetchall()
    by_type = con.execute(
        """
        SELECT coalesce(issue_type, '?') AS type, count(*) AS completed,
               quantile_cont(resolved - created, 0.5) AS lead_p50,
               quantile_cont(resolved - created, 0.85) AS lead_p85,
               quantile_cont(resolved - created, 0.95) AS lead_p95
        FROM issues WHERE scope = ? AND resolved BETWEEN ? AND ? AND created IS NOT NULL
        GROUP BY ALL ORDER BY completed DESC""",
        [args.scope, h_start, h_end],
    ).fetchall()
    open_by_type = dict(
        con.execute(
            "SELECT coalesce(issue_type, '?'), count(*) FROM issues WHERE scope = ? AND NOT done GROUP BY ALL",
            [args.scope],
        ).fetchall()
    )
    snaps = con.execute(
        "SELECT taken_at, open_items, done_items FROM snapshots WHERE scope = ? ORDER BY taken_at DESC LIMIT ?",
        [args.scope, args.snapshots],
    ).fetchall()
    out = {
        "scope": args.scope,
        "window": [h_start.isoformat(), h_end.isoformat()],
        # `days`: how many of the week's 7 days fall inside the window (edge weeks are partial).
        "weekly": [
            {"week": w.isoformat(), "completed": c, "created": n,
             "days": (min(w + timedelta(days=6), h_end) - max(w, h_start)).days + 1}
            for w, c, n in weekly
        ],
        "by_type": [
            {"type": t, "completed": c, "open": open_by_type.get(t, 0), "lead_time_days": {"p50": a, "p85": b, "p95": d}}
            for t, c, a, b, d in by_type
        ]
        + [
            {"type": t, "completed": 0, "open": n, "lead_time_days": None}
            for t, n in open_by_type.items()
            if t not in {r[0] for r in by_type}
        ],
        "snapshots": [{"taken_at": t.isoformat(), "open": o, "done": d} for t, o, d in reversed(snaps)],
    }
    return out


def cmd_stats(con, args) -> None:
    out = stats_data(con, args)
    if args.json:
        print(json.dumps(public(out), indent=2, default=str))
        return
    print(f"Scope {args.scope}, {out['window'][0]} → {out['window'][1]}\n\nweek         completed  created")
    for w in out["weekly"]:
        partial = f"  (partial: {w['days']} of 7 days)" if w["days"] < 7 else ""
        print(f"{w['week']}  {w['completed']:>9}  {w['created']:>7}{partial}")
    print("\ntype                 completed  open  lead time p50/p85/p95 (days)")
    for t in out["by_type"]:
        lt = t["lead_time_days"]
        lead = "-" if not lt else "/".join(f"{lt[k]:.0f}" for k in ("p50", "p85", "p95"))
        print(f"{t['type'][:20]:<20} {t['completed']:>9}  {t['open']:>4}  {lead}")
    if out["snapshots"]:
        print("\nbacklog snapshots (open / done)")
        for s in out["snapshots"]:
            print(f"  {s['taken_at'][:16]}  {s['open']:>5} / {s['done']}")


# --- aging ------------------------------------------------------------------

MIN_TYPE_SAMPLE = 10


def aging_data(con, args) -> dict:
    """Open items whose age already exceeds what most completed items of their type took."""
    h_start, h_end = history_window(con, args.scope, args.window, None)
    lead = con.execute(
        """SELECT issue_type, list(resolved - created ORDER BY resolved - created) FROM issues
           WHERE scope = ? AND resolved BETWEEN ? AND ? AND created IS NOT NULL
             AND coalesce(issue_type, '') <> 'Epic'
           GROUP BY ALL""",
        [args.scope, h_start, h_end],
    ).fetchall()
    by_type = {t: v for t, v in lead}
    overall = sorted(d for v in by_type.values() for d in v)
    if not overall:
        sys.exit(
            "error: no items were completed in the history window, so there is nothing to compare open items "
            "against; widen --window or sync more history (a full sync fetches the last 90 days)"
        )
    items = con.execute(
        """SELECT key, issue_type, epic, coalesce(status_category, 'unknown'), ? - created AS age FROM issues
           WHERE scope = ? AND NOT done AND created IS NOT NULL AND coalesce(issue_type, '') <> 'Epic'
           ORDER BY age DESC, key""",
        [h_end, args.scope],
    ).fetchall()
    rows = []
    for key, itype, epic, category, age in items:
        sample = by_type.get(itype) or []
        basis = itype if len(sample) >= MIN_TYPE_SAMPLE else "all types"
        sample = sample if basis == itype else overall
        older_than = sum(d < age for d in sample) / len(sample)
        p85, p95 = percentile(sample, 85), percentile(sample, 95)
        risk = "stale" if age > p95 else "at risk" if age > p85 else "ok"
        rows.append(
            {"key": key, "type": itype, "epic": epic, "status": category, "age_days": age,
             "older_than_pct_of_completed": round(older_than * 100), "lead_time_p85": p85, "lead_time_p95": p95,
             "basis": basis, "risk": risk}
        )
    flagged = [r for r in rows if r["risk"] != "ok"]
    out = {
        "scope": args.scope,
        "as_of": h_end.isoformat(),
        "window": [h_start.isoformat(), h_end.isoformat()],
        "open": len(rows),
        "at_risk": sum(r["risk"] == "at risk" for r in rows),
        "stale": sum(r["risk"] == "stale" for r in rows),
        "items": rows if args.all else flagged,
    }
    return out


def cmd_aging(con, args) -> None:
    out = aging_data(con, args)
    if args.json:
        print(json.dumps(public(out), indent=2, default=str))
        return
    print(
        f"Scope {args.scope}, as of {out['as_of']}: {out['open']} open, {out['stale']} stale (older than p95 lead time), "
        f"{out['at_risk']} at risk (older than p85)\n"
    )
    if not out["items"]:
        print("Nothing older than its type's p85 lead time.")
        return
    print(f"{'key':<10} {'type':<12} {'status':<11} {'epic':<9} {'age':>4}  {'p85/p95':>8}  older than   risk")
    for r in out["items"]:
        basis = "" if r["basis"] == r["type"] else "*"
        print(
            f"{r['key']:<10} {(r['type'] or '?')[:12]:<12} {r['status']:<11} {r['epic'] or '-':<9} {r['age_days']:>4}  "
            f"{r['lead_time_p85']:>3}/{r['lead_time_p95']:<4}{basis:1}  {r['older_than_pct_of_completed']:>5}%      {r['risk']}"
        )
    if any(r["basis"] == "all types" for r in out["items"]):
        print(f"\n* fewer than {MIN_TYPE_SAMPLE} completions of this type in the window; compared against all types")
    print("age and lead time in days since creation; 'older than' = share of recently completed items it has outlived")


# --- report -----------------------------------------------------------------


def cmd_report(con, args) -> None:
    import report

    common = dict(
        scope=args.scope, window=args.window, history_end=None, start=None, seed=args.seed, runs=args.runs,
        max_days=1095, no_record=True, target_date=args.target_date,
    )
    jql, last_sync = con.execute("SELECT jql, last_sync FROM scopes WHERE name = ?", [args.scope]).fetchone() or (None, None)
    data = {
        "scope": args.scope,
        "jql": jql,
        "last_sync": last_sync.isoformat() if last_sync else None,
        "forecast": forecast_data(
            con, argparse.Namespace(**common, type=None, by=None, items=None, at_least=None, no_scope_growth=True)
        ),
        "epics": epics_data(
            con, argparse.Namespace(**common, epic=None, order=args.order, wip=args.wip, epic_share=args.epic_share)
        ),
        "aging": aging_data(con, argparse.Namespace(scope=args.scope, window=args.window, all=True)),
        "stats": stats_data(con, argparse.Namespace(scope=args.scope, window=args.window, snapshots=10)),
        "calibrate": calibrate_data(con, argparse.Namespace(scope=args.scope)),
    }
    now = datetime.now()
    root = reports_dir(args)
    # One file per run, never overwritten: reports/<scope>/<scope>-<date>-<time>.html
    html = report.render(data)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(html)
        out = args.out
    else:
        out = write_new(root / args.scope / f"{args.scope}-{now:%Y-%m-%d-%H%M%S}.html", html)
    fc = data["forecast"]
    h = fc["history"]
    try:
        con.execute(
            "INSERT INTO reports VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [args.scope, now, str(out.resolve()), h["end"], fc["items"], fc["percentiles"]["no_growth"]["p85"],
             h["completed_per_week"] - h["created_per_week"] > 0.1 * h["completed_per_week"],
             fc.get("target_date"), (fc.get("chance") or {}).get("no_growth")],
        )
    except BaseException:
        if not args.out:
            out.unlink(missing_ok=True)  # an unrecorded report would never appear in the index
        raise
    write_index(con, root)
    print(out)


def write_new(path: Path, text: str) -> Path:
    """Write `text` to a file that didn't exist before: `path`, else `name-2`, `name-3`...

    Creating with mode "x" claims the name atomically, so two reports saved in the same
    second (even by two processes) never overwrite each other.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    for n in range(1, 1000):
        candidate = path if n == 1 else path.with_stem(f"{path.stem}-{n}")
        try:
            with candidate.open("x") as fh:
                fh.write(text)
            return candidate
        except FileExistsError:
            continue
    raise SystemExit(f"error: 999 reports named {path.stem} already exist; clear out {path.parent}")


def reports_dir(args) -> Path:
    return (args.db or default_db()).parent / "reports"


def saved_reports(con, scope: str | None = None) -> list[dict]:
    rows = con.execute(
        "SELECT * FROM reports WHERE ?::TEXT IS NULL OR scope = ? ORDER BY created_at DESC", [scope, scope]
    )
    cols = [c[0] for c in rows.description]
    return [dict(zip(cols, r)) for r in rows.fetchall() if Path(r[2]).exists()]


def write_index(con, root: Path) -> Path:
    import report

    root.mkdir(parents=True, exist_ok=True)
    index = root / "index.html"
    index.write_text(report.render_index(saved_reports(con), root))
    return index


def cmd_reports(con, args) -> None:
    rows = saved_reports(con, args.scope)
    index = write_index(con, reports_dir(args))
    if args.json:
        print(json.dumps({"index": str(index), "reports": rows}, indent=2, default=str))
        return
    if not rows:
        print("No saved reports yet; run `report <scope>`.")
        return
    for r in rows:
        print(f"{r['created_at']:%Y-%m-%d %H:%M}  {r['scope']:<12} 85% by {r['p85'] or '—'}  {r['path']}")
    print(f"\nIndex: {index}")


# --- cli --------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=None, help="DuckDB file (see above for default)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("sync-info", help="what to fetch next for a scope")
    p.add_argument("scope")

    p = sub.add_parser("ingest", help="upsert issues (JSON list/lines, or CSV: key,type,created,resolved,status_category)")
    p.add_argument("scope")
    p.add_argument("files", nargs="+", metavar="file", help="CSV/JSON files (e.g. one per page), or - for stdin")
    p.add_argument("--jql", help="JQL defining the scope (stored for later syncs)")
    p.add_argument("--full", action="store_true", help="payload has every open item; drop open items not in it")
    p.add_argument("--include-subtasks", action="store_true", help="keep sub-tasks (dropped by default)")
    p.add_argument("--as-of", type=datetime.fromisoformat,
                   help="when this data was fetched from Jira (default: now); e.g. to load a historical snapshot")
    p.add_argument("--epic-field", help="JSON field holding the epic key, e.g. customfield_10008 (Data Center Epic Link)")

    p = sub.add_parser("forecast", help="Monte Carlo forecast from stored history")
    p.add_argument("scope")
    goal = p.add_mutually_exclusive_group()
    goal.add_argument("--items", type=int, help="override remaining count (default: open items in DB)")
    goal.add_argument("--by", type=date.fromisoformat, help="how many items by this date")
    p.add_argument("--target-date", type=date.fromisoformat, help="when-forecasts: also give the chance of finishing by this date")
    p.add_argument("--at-least", type=int, help="with --by: also give the chance of finishing at least this many")
    p.add_argument("--type", action="append", help="restrict to issue type (repeatable)")
    p.add_argument("--window", type=int, default=90, help="history window in days (default 90)")
    p.add_argument("--history-end", type=date.fromisoformat, help="default: date of last sync")
    p.add_argument("--start", type=date.fromisoformat, help="forecast start (default: history end)")
    p.add_argument("--no-scope-growth", action="store_true", help="skip the scope-growth model")
    p.add_argument("--max-days", type=int, default=1095, help="simulation cap per run (default 1095)")
    p.add_argument("--runs", type=int, default=10_000)
    p.add_argument("--seed", type=int)
    p.add_argument("--no-record", action="store_true", help="don't store this forecast for calibration")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("epics", help="per-epic forecasts (current pace vs sole focus)")
    p.add_argument("scope")
    p.add_argument("--epic", action="append", help="only these epic keys (repeatable)")
    p.add_argument("--order", action="append", help="priority order of epic keys, comma-separated (adds a priority forecast)")
    p.add_argument("--wip", type=int, default=1, help="epics worked at once in priority order (default 1)")
    p.add_argument("--target-date", type=date.fromisoformat, help="also give each epic's chance of finishing by this date")
    p.add_argument(
        "--epic-share", type=float, help="share of team throughput spent on epic work (default: historical share)"
    )
    p.add_argument("--window", type=int, default=90)
    p.add_argument("--history-end", type=date.fromisoformat, help="default: date of last sync")
    p.add_argument("--start", type=date.fromisoformat, help="forecast start (default: history end)")
    p.add_argument("--max-days", type=int, default=1095)
    p.add_argument("--runs", type=int, default=10_000)
    p.add_argument("--seed", type=int)
    p.add_argument("--no-record", action="store_true")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("calibrate", help="score recorded forecasts against actuals")
    p.add_argument("scope", nargs="?")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("aging", help="open items older than their type's usual lead time")
    p.add_argument("scope")
    p.add_argument("--window", type=int, default=90)
    p.add_argument("--all", action="store_true", help="list every open item, not just flagged ones")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("report", help="write a self-contained HTML report and print its path")
    p.add_argument("scope")
    p.add_argument("--out", type=Path, help="output file (default: reports/<scope>/<scope>-<timestamp>.html next to the DB)")
    p.add_argument("--window", type=int, default=90)
    p.add_argument("--target-date", type=date.fromisoformat, help="add the chance of finishing by this date")
    p.add_argument("--order", action="append", help="epic priority order, comma-separated")
    p.add_argument("--wip", type=int, default=1)
    p.add_argument("--epic-share", type=float)
    p.add_argument("--runs", type=int, default=10_000)
    p.add_argument("--seed", type=int)

    p = sub.add_parser("reports", help="list saved HTML reports, newest first, and refresh the index page")
    p.add_argument("scope", nargs="?")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("stats", help="weekly throughput, lead time by type, backlog snapshots")
    p.add_argument("scope")
    p.add_argument("--window", type=int, default=90)
    p.add_argument("--snapshots", type=int, default=10, help="recent snapshots to show")
    p.add_argument("--json", action="store_true")

    args = ap.parse_args(argv)
    con = connect(args.db or default_db())
    try:
        {
            "sync-info": cmd_sync_info,
            "ingest": cmd_ingest,
            "forecast": cmd_forecast,
            "epics": cmd_epics,
            "calibrate": cmd_calibrate,
            "stats": cmd_stats,
            "aging": cmd_aging,
            "report": cmd_report,
            "reports": cmd_reports,
        }[args.cmd](con, args)
    finally:
        con.close()


if __name__ == "__main__":
    main()
