#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["duckdb>=1.1"]
# ///
"""Jira history store + Monte Carlo forecasts with scope growth and calibration.

Subcommands:
  sync-info SCOPE        what to fetch next (full or incremental, and since when)
  ingest SCOPE FILE      upsert issues fetched from Jira, snapshot the backlog
  forecast SCOPE         simulate from stored history and record the forecast
  epics SCOPE            per-epic forecasts: at current pace, and if it had the team's sole focus
  calibrate [SCOPE]      score past forecasts against what actually happened
  stats SCOPE            weekly throughput/arrivals, lead time, backlog snapshots

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
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).parent))
from forecast import PERCENTILES, daily_throughput, histogram, percentile, simulate_how_many, simulate_when, when_date  # noqa: E402

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
-- Columns added after the first release; no-ops on fresh DBs.
ALTER TABLE issues ADD COLUMN IF NOT EXISTS epic TEXT;
ALTER TABLE forecasts ADD COLUMN IF NOT EXISTS epic TEXT;
"""


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
    resolved = _day(_field(issue, "resolved", "resolutiondate", "resolution_date"))
    done = _field(issue, "done")
    if done is None:
        done = resolved is not None or str(category).lower() == "done"
    return {
        "key": issue["key"],
        "issue_type": issue_type,
        "created": _day(_field(issue, "created")),
        "resolved": resolved,
        "done": str(done).lower() not in ("false", "0", "") if isinstance(done, str) else bool(done),
        "subtask": subtask,
        "epic": _epic(issue, epic_field),
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


def cmd_ingest(con, args) -> None:
    issues = load_issues(Path(args.file).read_text() if args.file != "-" else sys.stdin.read(), args.epic_field)
    subtasks = sum(i["subtask"] for i in issues)
    if not args.include_subtasks:
        issues = [i for i in issues if not i["subtask"]]
    now = datetime.now()
    con.execute(
        "INSERT INTO scopes VALUES (?, ?, ?, NULL, NULL) ON CONFLICT DO NOTHING", [args.scope, args.jql, now]
    )
    if args.jql:
        con.execute("UPDATE scopes SET jql = ? WHERE name = ?", [args.jql, args.scope])
    if issues:
        con.executemany(
            "INSERT OR REPLACE INTO issues (scope, key, issue_type, created, resolved, done, synced_at, epic) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                [args.scope, i["key"], i["issue_type"], i["created"], i["resolved"], i["done"], now, i["epic"]]
                for i in issues
            ],
        )
    removed = 0
    if args.full:
        # A full sync returns every open item, so open items we didn't see have left the scope.
        seen = [i["key"] for i in issues]
        removed = con.execute(
            "SELECT count(*) FROM issues WHERE scope = ? AND NOT done AND NOT list_contains(?, key)",
            [args.scope, seen],
        ).fetchone()[0]
        con.execute("DELETE FROM issues WHERE scope = ? AND NOT done AND NOT list_contains(?, key)", [args.scope, seen])
        con.execute("UPDATE scopes SET last_full_sync = ? WHERE name = ?", [now, args.scope])
    con.execute("UPDATE scopes SET last_sync = ? WHERE name = ?", [now, args.scope])
    open_items, done_items = con.execute(
        "SELECT count(*) FILTER (NOT done), count(*) FILTER (done) FROM issues WHERE scope = ?", [args.scope]
    ).fetchone()
    con.execute("INSERT INTO snapshots VALUES (?, ?, ?, ?)", [args.scope, now, open_items, done_items])
    print(
        json.dumps(
            {
                "scope": args.scope,
                "ingested": len(issues),
                "skipped_subtasks": 0 if args.include_subtasks else subtasks,
                "removed": removed,
                "open": open_items,
                "done": done_items,
            }
        )
    )


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
            }
        )
    )


# --- forecast ---------------------------------------------------------------


def _type_filter(types: list[str] | None) -> tuple[str, list]:
    # Epics are containers, not deliverable items: excluded unless asked for by type.
    if types:
        return "AND issue_type IN (SELECT unnest(?))", [types]
    return "AND coalesce(issue_type, '') <> 'Epic'", []


def history_window(con, scope: str, window: int, end: date | None) -> tuple[date, date]:
    if end is None:
        last = con.execute("SELECT last_sync FROM scopes WHERE name = ?", [scope]).fetchone()
        if not last or last[0] is None:
            sys.exit(f"error: no data for scope {scope!r}; ingest first")
        end = last[0].date()
    return end - timedelta(days=window - 1), end


def daily_series(con, scope: str, column: str, start: date, end: date, types) -> list[int]:
    sql, params = _type_filter(types)
    dates = [
        r[0]
        for r in con.execute(
            f"SELECT {column} FROM issues WHERE scope = ? AND {column} IS NOT NULL {sql}", [scope, *params]
        ).fetchall()
    ]
    return daily_throughput(dates, start, end)


def open_keys(con, scope: str, types) -> list[str]:
    sql, params = _type_filter(types)
    return [r[0] for r in con.execute(f"SELECT key FROM issues WHERE scope = ? AND NOT done {sql}", [scope, *params]).fetchall()]


def record(con, scope, kind, growth, start, target, items, types, h_start, h_end, pct, keys=(), epic=None) -> int:
    fid = con.execute(
        """INSERT INTO forecasts (scope, made_at, kind, scope_growth, start, target_date, items, types,
                                  history_start, history_end, percentiles, epic)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
        [scope, datetime.now(), kind, growth, start, target, items, types, h_start, h_end, json.dumps(pct), epic],
    ).fetchone()[0]
    if keys:
        con.executemany("INSERT INTO forecast_items VALUES (?, ?)", [[fid, k] for k in keys])
    return fid


def cmd_forecast(con, args) -> None:
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

    distributions = {}
    if args.by:
        horizon = (args.by - start).days
        if horizon <= 0:
            sys.exit("error: --by must be after the forecast start")
        results = simulate_how_many(throughput, horizon, args.runs, rng)
        pct = {f"p{p}": percentile(results, 100 - p) for p in PERCENTILES}
        out.update(kind="how_many", target_date=args.by.isoformat(), percentiles=pct)
        distributions["items"] = results
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
        models = [("no_growth", None)] + ([] if args.no_scope_growth else [("scope_growth", arrivals)])
        for name, arr in models:
            results = simulate_when(throughput, items, args.runs, rng, arrivals=arr, max_days=args.max_days)
            pct = {f"p{p}": when_date(start, percentile(results, p)) for p in PERCENTILES}
            out["percentiles"][name] = pct
            out["finished_within_limit"][name] = round(sum(r != math.inf for r in results) / len(results), 4)
            distributions[f"days ({name})"] = results
            if not args.no_record:
                out.setdefault("forecast_ids", {})[name] = record(
                    con, args.scope, "when", arr is not None, start, None, items, types, h_start, h_end, pct,
                    keys if args.items is None else (),
                )
        out["max_days"] = args.max_days

    if args.json:
        print(json.dumps(out, indent=2, default=str))
        return
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
    else:
        print(f"\nWhen will {out['items']} items be done (from {start})?")
        names = list(out["percentiles"])
        print("  conf  " + "".join(f"{n:>14}" for n in names))
        for p in PERCENTILES:
            row = [out["percentiles"][n][f"p{p}"] or f">{args.max_days}d" for n in names]
            print(f"  {p:>3}%  " + "".join(f"{v:>14}" for v in row))
        for n in names:
            if out["finished_within_limit"][n] < 1:
                print(f"  {n}: only {out['finished_within_limit'][n]:.0%} of runs finished within {args.max_days} days")
    for label, values in distributions.items():
        print(f"\nDistribution, {label}:\n{histogram(values)}")


# --- epics ------------------------------------------------------------------


def cmd_epics(con, args) -> None:
    h_start, h_end = history_window(con, args.scope, args.window, args.history_end)
    start = args.start or h_end
    rng = random.Random(args.seed)
    epic_sql, epic_params = ("AND epic IN (SELECT unnest(?))", [args.epic]) if args.epic else ("", [])
    rows = con.execute(
        f"""
        SELECT epic, list(key ORDER BY key) FILTER (NOT done) AS open_keys
        FROM issues
        WHERE scope = ? AND epic IS NOT NULL AND coalesce(issue_type, '') <> 'Epic' {epic_sql}
        GROUP BY epic
        HAVING count(*) FILTER (NOT done) > 0
            OR epic IN (SELECT key FROM issues WHERE scope = ? AND issue_type = 'Epic' AND NOT done)
        ORDER BY epic""",
        [args.scope, *epic_params, args.scope],
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
                if not args.no_record:
                    e["forecast_id"] = record(
                        con, args.scope, "when", False, start, None, len(keys), None, h_start, h_end,
                        e["current_pace"], keys, epic=epic,
                    )
            if any(team):
                results = simulate_when(team, len(keys), args.runs, rng, max_days=args.max_days)
                e["sole_focus"] = {f"p{p}": when_date(start, percentile(results, p)) for p in PERCENTILES}
        epics.append(e)

    out = {
        "scope": args.scope,
        "history": {"start": h_start.isoformat(), "end": h_end.isoformat(), "team_completed": sum(team)},
        "start": start.isoformat(),
        "epics": epics,
        "open_without_epic": unparented_open,
    }
    if args.json:
        print(json.dumps(out, indent=2, default=str))
        return
    if not epics:
        print(f"No open epics in scope {args.scope} (were epic links ingested?)")
        return
    print(f"Scope {args.scope}: epics from {start}, history {h_start} → {h_end} ({sum(team)} team completions)\n")
    print(f"{'epic':<12} {'status':<10} {'open':>4} {'done/window':>11}   {'current pace p50':>16} {'p85':>11}   {'sole focus p85':>14}")
    for e in epics:
        pace, focus = e["current_pace"], e["sole_focus"]
        if not e["open"]:
            p50 = p85 = "done"
        elif pace is None:
            p50, p85 = "no progress", "-"
        else:
            p50, p85 = (pace[k] or f">{args.max_days}d" for k in ("p50", "p85"))
        f85 = (focus["p85"] or f">{args.max_days}d") if focus else "-"
        print(f"{e['epic']:<12} {e['status']:<10} {e['open']:>4} {e['completed_in_window']:>11}   {p50:>16} {p85:>11}   {f85:>14}")
    print(f"\n{unparented_open} open items have no epic.")
    if stale := [e["epic"] for e in epics if e["status"] == "done" and e["open"]]:
        print(f"Epics marked done but with open children: {', '.join(stale)}")
    print("current pace = resampling the epic's own completions; sole focus = whole team on this epic only.")


# --- calibrate --------------------------------------------------------------


def actual_when(con, f: dict) -> date | None:
    """Date the forecast's backlog was cleared, or None if not (yet) known."""
    if f["scope_growth"]:
        # Backlog zero: a day on/after start where every in-scope item created by then is resolved by then.
        sql, params = _type_filter(f["types"])
        row = con.execute(
            f"""
            WITH s AS (SELECT * FROM issues WHERE scope = ? {sql}),
            candidates AS (SELECT DISTINCT resolved AS d FROM s WHERE resolved > ?)
            SELECT min(d) FROM candidates
            WHERE NOT EXISTS (
                SELECT 1 FROM s WHERE created <= d AND (resolved IS NULL OR resolved > d) AND NOT (done AND resolved IS NULL)
            )""",
            [f["scope"], *params, f["start"]],
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
    sql, params = _type_filter(f["types"])
    return con.execute(
        f"SELECT count(*) FROM issues WHERE scope = ? AND resolved > ? AND resolved <= ? {sql}",
        [f["scope"], f["start"], f["target_date"], *params],
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


def cmd_calibrate(con, args) -> None:
    where, params = ("WHERE f.scope = ?", [args.scope]) if args.scope else ("", [])
    rows = con.execute(
        f"""SELECT f.*, s.last_sync FROM forecasts f JOIN scopes s ON s.name = f.scope {where} ORDER BY f.id""",
        params,
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
    if args.json:
        print(json.dumps({"summary": summary, "forecasts": detail}, indent=2, default=str))
        return
    if not forecasts:
        print("No recorded forecasts.")
        return
    print("Calibration: share of resolved forecasts where the pN answer held (well calibrated ≈ N%)\n")
    for model, by_p in summary.items():
        print(model)
        for k, s in by_p.items():
            resolved = s["held"] + s["missed"]
            rate = f"{s['held'] / resolved:.0%}" if resolved else "  -"
            print(f"  {k}: {rate:>4} held  ({s['held']}/{resolved} resolved, {s['pending']} pending)")
    print(f"\n{len(forecasts)} forecasts; --json for per-forecast detail")


# --- stats ------------------------------------------------------------------


def cmd_stats(con, args) -> None:
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
        "weekly": [{"week": w.isoformat(), "completed": c, "created": n} for w, c, n in weekly],
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
    if args.json:
        print(json.dumps(out, indent=2, default=str))
        return
    print(f"Scope {args.scope}, {h_start} → {h_end}\n\nweek         completed  created")
    for w in out["weekly"]:
        print(f"{w['week']}  {w['completed']:>9}  {w['created']:>7}")
    print("\ntype                 completed  open  lead time p50/p85/p95 (days)")
    for t in out["by_type"]:
        lt = t["lead_time_days"]
        lead = "-" if not lt else "/".join(f"{lt[k]:.0f}" for k in ("p50", "p85", "p95"))
        print(f"{t['type'][:20]:<20} {t['completed']:>9}  {t['open']:>4}  {lead}")
    if out["snapshots"]:
        print("\nbacklog snapshots (open / done)")
        for s in out["snapshots"]:
            print(f"  {s['taken_at'][:16]}  {s['open']:>5} / {s['done']}")


# --- cli --------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=None, help="DuckDB file (see above for default)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("sync-info", help="what to fetch next for a scope")
    p.add_argument("scope")

    p = sub.add_parser("ingest", help="upsert issues (JSON list/lines, or CSV: key,type,created,resolved,status_category)")
    p.add_argument("scope")
    p.add_argument("file", help="JSON file, or - for stdin")
    p.add_argument("--jql", help="JQL defining the scope (stored for later syncs)")
    p.add_argument("--full", action="store_true", help="payload has every open item; drop open items not in it")
    p.add_argument("--include-subtasks", action="store_true", help="keep sub-tasks (dropped by default)")
    p.add_argument("--epic-field", help="JSON field holding the epic key, e.g. customfield_10008 (Data Center Epic Link)")

    p = sub.add_parser("forecast", help="Monte Carlo forecast from stored history")
    p.add_argument("scope")
    goal = p.add_mutually_exclusive_group()
    goal.add_argument("--items", type=int, help="override remaining count (default: open items in DB)")
    goal.add_argument("--by", type=date.fromisoformat, help="how many items by this date")
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
        }[args.cmd](con, args)
    finally:
        con.close()


if __name__ == "__main__":
    main()
