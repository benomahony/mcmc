#!/usr/bin/env python3
"""Jira history store + Monte Carlo forecasts with calibration.

Subcommands:
  sync-info SCOPE        what to fetch next (full or incremental, and since when)
  ingest SCOPE FILE...   upsert issues fetched from Jira, snapshot the backlog
  forecast SCOPE         simulate from stored history and record the forecast
  epics SCOPE            per-epic forecasts: at current pace, and if it had the team's sole focus
  calibrate [SCOPE]      score past forecasts against what actually happened
  stats SCOPE            weekly finished/created, lead time, backlog snapshots
  aging SCOPE            open items older than their type's usual lead time
  report SCOPE           all of the above as one self-contained HTML page, kept in reports/
  reports [SCOPE]        list saved reports (newest first) and the index page

DB: --db, else $MCMC_DB, else $CLAUDE_PLUGIN_DATA/mcmc.sqlite, else
~/.local/share/mcmc/mcmc.sqlite.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import analysis  # noqa: E402
import jira_input  # noqa: E402
import report  # noqa: E402
import store  # noqa: E402
from analysis import MIN_TYPE_SAMPLE, UserError  # noqa: E402
from forecast import PERCENTILES, histogram  # noqa: E402
from store import Connection  # noqa: E402

def public(out: dict) -> dict:
    """Drop private keys (raw simulation samples etc.) before printing JSON."""
    shown = {k: v for k, v in out.items() if not k.startswith("_")}
    hidden = sorted(set(out) - set(shown))
    assert all(k.startswith("_") for k in hidden), f"dropped public keys: {hidden}"
    assert len(shown) + len(hidden) == len(out), f"{len(shown)} shown + {len(hidden)} hidden != {len(out)} keys"
    return shown


def emit_json(out: dict) -> None:
    text = json.dumps(public(out), indent=2, default=str)
    assert text.startswith("{"), "JSON output must be an object"
    assert "_samples" not in text, "raw samples must not be printed"
    print(text)


# --- sync ---------------------------------------------------------------------------------


def cmd_sync_info(con: Connection, args: argparse.Namespace) -> None:
    state = store.sync_state(con, args.scope, datetime.now())
    assert state["mode"] in ("full", "incremental"), f"unknown sync mode {state['mode']}"
    assert state["scope"] == args.scope, f"state for {state['scope']}, asked for {args.scope}"
    print(json.dumps(state))


def read_files(files: list[str], epic_field: str | None) -> list[dict]:
    """Issues from every file given (or stdin for '-'), in order."""
    assert files, "ingest needs at least one file"
    issues = [i for f in files for i in jira_input.load_issues(sys.stdin.read() if f == "-" else Path(f).read_text(), epic_field)]
    unflagged = [i["key"] for i in issues if "subtask" not in i]
    assert not unflagged, f"records must say whether they are sub-tasks: {unflagged[:5]}"
    return issues


def cmd_ingest(con: Connection, args: argparse.Namespace) -> None:
    issues = read_files(args.files, args.epic_field)
    subtasks = sum(i["subtask"] for i in issues)
    kept = issues if args.include_subtasks else [i for i in issues if not i["subtask"]]
    sync = store.Sync(args.scope, args.as_of or datetime.now(), full=args.full, jql=args.jql)
    with store.transaction(con):
        result = store.ingest(con, sync, kept)
    assert len(kept) == len(issues) - (0 if args.include_subtasks else subtasks), "sub-task filtering lost issues"
    assert result["open"] >= 0, f"negative open count {result['open']}"
    print(json.dumps({"scope": args.scope, "ingested": len(kept),
                      "skipped_subtasks": 0 if args.include_subtasks else subtasks} | result))


# --- forecast -----------------------------------------------------------------------------


def print_history(out: dict, args: argparse.Namespace) -> None:
    h = out["history"]
    assert h["days"] >= 1, f"history covers {h['days']} days"
    assert out["scope"] == args.scope, f"output for {out['scope']}, asked for {args.scope}"
    types = out["types"]
    print(f"Scope {args.scope}" + (f" (types: {', '.join(types)})" if types else ""))
    print(f"History {h['start']} → {h['end']}: {h['completed']} completed ({h['completed_per_week']}/wk), "
          f"{h['created']} created ({h['created_per_week']}/wk)")
    for w in out.get("warnings", []):
        print(f"WARNING: {w}")


def print_how_many(out: dict, args: argparse.Namespace) -> None:
    pct = out["percentiles"]
    assert set(pct) == {f"p{p}" for p in PERCENTILES}, f"the table prints every percentile {PERCENTILES}, got {sorted(pct)}"
    assert out["kind"] == "how_many", f"not a how-many forecast: {out['kind']}"
    print(f"\nItems completed between {out['start']} and {args.by}:")
    for k, v in pct.items():
        print(f"  {k[1:]}% confidence: at least {v}")
    if "chance" in out:
        print(f"  Chance of at least {args.at_least}: {out['chance']:.0%}")


def print_when(out: dict, args: argparse.Namespace) -> None:
    pct = out["percentiles"]
    assert set(pct) == {f"p{p}" for p in PERCENTILES}, f"the table prints every percentile {PERCENTILES}, got {sorted(pct)}"
    assert out["kind"] == "when", f"not a when forecast: {out['kind']}"
    print(f"\nWhen will today's {out['items']} items be done (from {out['start']}), if nothing new is added?")
    for p in PERCENTILES:
        print(f"  {p}% confidence: {pct[f'p{p}'] or f'more than {args.max_days} days'}")
    if "chance" in out:
        print(f"  Chance of being done by {args.target_date}: {out['chance']:.0%}")
    if out["finished_within_limit"] < 1:
        print(f"  only {out['finished_within_limit']:.0%} of runs finished within {args.max_days} days")


def cmd_forecast(con: Connection, args: argparse.Namespace) -> None:
    out = analysis.forecast_data(con, args)
    assert out["kind"] in ("when", "how_many"), f"unknown forecast kind {out['kind']}"
    assert out["_samples"], "a forecast always has samples"
    if args.json:
        emit_json(out)
        return
    print_history(out, args)
    (print_how_many if out["kind"] == "how_many" else print_when)(out, args)
    for label, values in out["_samples"].items():
        print(f"\nDistribution, {label}:\n{histogram(values)}")


# --- epics --------------------------------------------------------------------------------


def epic_cells(e: dict, args: argparse.Namespace) -> list[str]:
    """Current pace p50/p85 and sole-focus p85 for one epic's row."""
    assert e["open"] >= 0, f"{e['epic']} has {e['open']} open"
    assert args.max_days > 0, f"max days {args.max_days}"
    pace, focus = e["current_pace"], e["sole_focus"]
    if not e["open"]:
        p50 = p85 = "done"
    elif pace is None:
        p50, p85 = "no progress", "-"
    else:
        p50, p85 = (pace[k] or f">{args.max_days}d" for k in ("p50", "p85"))
    return [p50, p85, (focus["p85"] or f">{args.max_days}d") if focus else "-"]


def print_epic_notes(out: dict, args: argparse.Namespace) -> None:
    priority = out.get("priority")
    assert out["open_without_epic"] >= 0, f"negative unparented count {out['open_without_epic']}"
    assert priority is None or args.order, "a plan without an order"
    print(f"\n{out['open_without_epic']} open items have no epic.")
    if stale := [e["epic"] for e in out["epics"] if e["status"] == "done" and e["open"]]:
        print(f"Epics marked done but with open children: {', '.join(stale)}")
    print("current pace = resampling the epic's own completions; sole focus = whole team on this epic only.")
    if priority and priority.get("note"):
        print(f"priority: can't forecast the plan: {priority['note']}")
    elif priority:
        print(f"priority = epics worked in the order above, {args.wip} at a time, with {priority['epic_share']:.0%} "
              "of team throughput going to epic work; unlisted epics paused.")


def cmd_epics(con: Connection, args: argparse.Namespace) -> None:
    out = analysis.epics_data(con, args)
    epics, priority = out["epics"], out.get("priority")
    keyless = [e for e in epics if "epic" not in e]
    assert not keyless, f"every row needs an epic key: {keyless[:2]}"
    assert priority is None or priority["order"], "a plan needs an order"
    if args.json:
        emit_json(out)
        return
    if not epics:
        print(f"No open epics in scope {args.scope} (were epic links ingested?)")
        return
    h = out["history"]
    print(f"Scope {args.scope}: epics from {out['start']}, history {h['start']} → {h['end']} ({h['team_completed']} team completions)\n")
    models = ["current_pace", "sole_focus"] + (["priority"] if priority else [])
    head = f"{'epic':<12} {'status':<10} {'open':>4} {'done/window':>11}   {'current pace p50':>16} {'p85':>11}   {'sole focus p85':>14}"
    if priority:
        order = priority["order"]
        epics = sorted(epics, key=lambda e: order.index(e["epic"]) if e["epic"] in order else 10**6)
        head += f"   {'priority p85':>12}"
    if "target_date" in out:
        head += f"   chance by {args.target_date} ({'/'.join(m.split('_')[0] for m in models)})"
    print(head)
    for e in epics:
        print(epic_line(e, args, models, with_chance="target_date" in out))
    print_epic_notes(out, args)


def epic_line(e: dict, args: argparse.Namespace, models: list[str], *, with_chance: bool) -> str:
    """One epic's row in the epics table."""
    p50, p85, f85 = epic_cells(e, args)
    line = f"{e['epic']:<12} {e['status']:<10} {e['open']:>4} {e['completed_in_window']:>11}   {p50:>16} {p85:>11}   {f85:>14}"
    if "priority" in models:
        plan = e.get("priority")
        line += f"   {(plan['p85'] or f'>{args.max_days}d') if plan else 'paused':>12}"
    if with_chance:
        c = e.get("chance", {})
        line += "   " + " ".join(f"{c[m]:>4.0%}" if m in c else "   -" for m in models)
    assert line.startswith(e["epic"]), f"row for {e['epic']} starts {line[:12]!r}"
    assert "\n" not in line, f"row for {e['epic']} spans lines"
    return line


# --- calibrate, stats, aging --------------------------------------------------------------


def cmd_calibrate(con: Connection, args: argparse.Namespace) -> None:
    out = analysis.calibrate_data(con, args)
    assert set(out) == {"summary", "forecasts"}, f"calibration output fields {sorted(out)}"
    unscored = [f.get("id") for f in out["forecasts"] if "hits" not in f]
    assert not unscored, f"forecasts without hits: {unscored}"
    if args.json:
        emit_json(out)
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


def cmd_stats(con: Connection, args: argparse.Namespace) -> None:
    out = analysis.stats_data(con, args)
    assert out["scope"] == args.scope, f"stats for {out['scope']}, asked for {args.scope}"
    assert len(out["window"]) == 2, f"the header prints a start and end date, got window {out['window']}"
    if args.json:
        emit_json(out)
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


def cmd_aging(con: Connection, args: argparse.Namespace) -> None:
    out = analysis.aging_data(con, args)
    items = out["items"]
    assert len(items) <= out["open"], f"{len(items)} items listed of {out['open']} open"
    risks = {i["risk"] for i in items}
    assert risks <= {"ok", "at risk", "stale"}, f"unknown risk level in {risks}"
    if args.json:
        emit_json(out)
        return
    print(f"Scope {args.scope}, as of {out['as_of']}: {out['open']} open, {out['stale']} stale (older than p95 lead time), "
          f"{out['at_risk']} at risk (older than p85)\n")
    if not items:
        print("Nothing older than its type's p85 lead time.")
        return
    print(f"{'key':<10} {'type':<12} {'status':<11} {'epic':<9} {'age':>4}  {'p85/p95':>8}  older than   risk")
    for r in items:
        basis = "" if r["basis"] == r["type"] else "*"
        print(f"{r['key']:<10} {(r['type'] or '?')[:12]:<12} {r['status']:<11} {r['epic'] or '-':<9} {r['age_days']:>4}  "
              f"{r['lead_time_p85']:>3}/{r['lead_time_p95']:<4}{basis:1}  {r['older_than_pct_of_completed']:>5}%      {r['risk']}")
    if any(r["basis"] == "all types" for r in items):
        print(f"\n* fewer than {MIN_TYPE_SAMPLE} completions of this type in the window; compared against all types")
    print("age and lead time in days since creation; 'older than' = share of recently completed items it has outlived")


# --- reports ------------------------------------------------------------------------------


def report_data(con: Connection, args: argparse.Namespace) -> dict:
    """Everything the HTML report shows, computed without recording any forecasts."""
    common = {"scope": args.scope, "window": args.window, "history_end": None, "start": None, "seed": args.seed,
              "runs": args.runs, "max_days": 1095, "no_record": True, "target_date": args.target_date}
    jql, last_sync = store.scope_info(con, args.scope)
    data = {
        "scope": args.scope,
        "jql": jql,
        "last_sync": last_sync.isoformat() if last_sync else None,
        "forecast": analysis.forecast_data(con, argparse.Namespace(
            **common, type=None, by=None, items=None, at_least=None)),
        "epics": analysis.epics_data(con, argparse.Namespace(
            **common, epic=None, order=args.order, wip=args.wip, epic_share=args.epic_share)),
        "aging": analysis.aging_data(con, argparse.Namespace(scope=args.scope, window=args.window, all=True)),
        "stats": analysis.stats_data(con, argparse.Namespace(scope=args.scope, window=args.window, snapshots=10)),
        "calibrate": analysis.calibrate_data(con, argparse.Namespace(scope=args.scope)),
    }
    assert data["forecast"]["kind"] == "when", "the report forecasts when the backlog will be done"
    assert "p85" in data["forecast"]["percentiles"], "the report leads with the 85% date"
    return data


def write_new(path: Path, text: str) -> Path:
    """Write `text` to a file that didn't exist before: `path`, else `name-2`, `name-3`...

    Creating with mode "x" claims the name atomically, so two reports saved in the same
    second (even by two processes) never overwrite each other.
    """
    assert path.suffix == ".html", f"reports are HTML files, got {path.name}"
    assert text, "refusing to write an empty report"
    path.parent.mkdir(parents=True, exist_ok=True)
    for n in range(1, 1000):
        candidate = path if n == 1 else path.with_stem(f"{path.stem}-{n}")
        try:
            with candidate.open("x") as fh:
                fh.write(text)
        except FileExistsError:
            continue
        return candidate
    raise UserError(f"999 reports named {path.stem} already exist; clear out {path.parent}")


def reports_dir(db: Path | None) -> Path:
    root = store.db_path(db).parent / "reports"
    assert root.name == "reports", f"unexpected reports folder {root}"
    assert not root.is_file(), f"{root} is a file; the reports folder can't be created"
    return root


def write_index(con: Connection, root: Path) -> Path:
    """Refresh reports/index.html from the reports recorded in the store."""
    root.mkdir(parents=True, exist_ok=True)
    index = root / "index.html"
    index.write_text(report.render_index(store.saved_reports(con), root))
    assert index.exists(), f"failed to write {index}"
    assert index.parent == root, f"index written outside {root}"
    return index


def headline(scope: str, path: Path, data: dict, now: datetime) -> dict:
    """The row recorded for a saved report."""
    fc = data["forecast"]
    h = fc["history"]
    row = {
        "scope": scope, "created_at": now, "path": str(path.resolve()), "as_of": h["end"], "open_items": fc["items"],
        "p85": fc["percentiles"]["p85"],
        "shrinking": h["completed_per_week"] - h["created_per_week"] > 0.1 * h["completed_per_week"],
        "target_date": fc.get("target_date"), "chance": fc.get("chance"),
    }
    assert row["open_items"] > 0, f"a report forecasts {row['open_items']} open items"
    assert row["chance"] is None or 0 <= row["chance"] <= 1, f"chance {row['chance']} outside 0..1"
    return row


def cmd_report(con: Connection, args: argparse.Namespace) -> None:
    data = report_data(con, args)
    html = report.render(data)
    now = datetime.now()
    root = reports_dir(args.db)
    out: Path = args.out or write_new(root / args.scope / f"{args.scope}-{now:%Y-%m-%d-%H%M%S}.html", html)
    if args.out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html)
    recorded = False
    try:
        store.record_report(con, headline(args.scope, out, data, now))
        recorded = True
    finally:
        if not recorded and not args.out:
            out.unlink(missing_ok=True)  # an unrecorded report would never appear in the index
    assert out.exists(), f"report {out} vanished"
    assert html.startswith("<!doctype html>"), "the report must be a full HTML document"
    write_index(con, root)
    print(out)


def cmd_reports(con: Connection, args: argparse.Namespace) -> None:
    rows = store.saved_reports(con, args.scope)
    index = write_index(con, reports_dir(args.db))
    gone = [r["path"] for r in rows if not Path(r["path"]).exists()]
    assert not gone, f"listed reports whose files are gone: {gone}"
    assert index.exists(), f"index {index} missing"
    if args.json:
        print(json.dumps({"index": str(index), "reports": rows}, indent=2, default=str))
        return
    if not rows:
        print("No saved reports yet; run `report <scope>`.")
        return
    for r in rows:
        print(f"{r['created_at']:%Y-%m-%d %H:%M}  {r['scope']:<12} 85% by {r['p85'] or '—'}  {r['path']}")
    print(f"\nIndex: {index}")


# --- argument parsing -------------------------------------------------------------------


def add_sync_commands(sub: argparse._SubParsersAction) -> None:
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
    assert "sync-info" in sub.choices, "sync-info subcommand missing"
    assert "ingest" in sub.choices, "ingest subcommand missing"


def add_simulation_options(p: argparse.ArgumentParser) -> None:
    """Options every simulating command shares."""
    p.add_argument("scope")
    p.add_argument("--window", type=int, default=90, help="history window in days (default 90)")
    p.add_argument("--history-end", type=date.fromisoformat, help="default: date of last sync")
    p.add_argument("--start", type=date.fromisoformat, help="forecast start (default: history end)")
    p.add_argument("--max-days", type=int, default=1095, help="simulation cap per run (default 1095)")
    p.add_argument("--runs", type=int, default=10_000)
    p.add_argument("--seed", type=int)
    p.add_argument("--no-record", action="store_true", help="don't store this forecast for calibration")
    p.add_argument("--json", action="store_true")
    actions = {a.dest for a in p._actions}
    assert {"window", "runs", "seed"} <= actions, f"missing shared options in {sorted(actions)}"
    assert p.get_default("window") == 90, "default history window changed"


def add_forecast_commands(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("forecast", help="Monte Carlo forecast from stored history")
    add_simulation_options(p)
    goal = p.add_mutually_exclusive_group()
    goal.add_argument("--items", type=int, help="override remaining count (default: open items in DB)")
    goal.add_argument("--by", type=date.fromisoformat, help="how many items by this date")
    p.add_argument("--target-date", type=date.fromisoformat, help="when-forecasts: also give the chance of finishing by this date")
    p.add_argument("--at-least", type=int, help="with --by: also give the chance of finishing at least this many")
    p.add_argument("--type", action="append", help="restrict to issue type (repeatable)")
    p = sub.add_parser("epics", help="per-epic forecasts (current pace vs sole focus)")
    add_simulation_options(p)
    p.add_argument("--epic", action="append", help="only these epic keys (repeatable)")
    p.add_argument("--order", action="append", help="priority order of epic keys, comma-separated (adds a priority forecast)")
    p.add_argument("--wip", type=int, default=1, help="epics worked at once in priority order (default 1)")
    p.add_argument("--target-date", type=date.fromisoformat, help="also give each epic's chance of finishing by this date")
    p.add_argument("--epic-share", type=float, help="share of team throughput spent on epic work (default: historical share)")
    assert "forecast" in sub.choices, "forecast subcommand missing"
    assert "epics" in sub.choices, "epics subcommand missing"


def add_review_commands(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("calibrate", help="score recorded forecasts against actuals")
    p.add_argument("scope", nargs="?")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("aging", help="open items older than their type's usual lead time")
    p.add_argument("scope")
    p.add_argument("--window", type=int, default=90)
    p.add_argument("--all", action="store_true", help="list every open item, not just flagged ones")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("stats", help="weekly throughput, lead time by type, backlog snapshots")
    p.add_argument("scope")
    p.add_argument("--window", type=int, default=90)
    p.add_argument("--snapshots", type=int, default=10, help="recent snapshots to show")
    p.add_argument("--json", action="store_true")
    assert {"calibrate", "aging", "stats"} <= set(sub.choices), f"review subcommands missing from {sorted(sub.choices)}"
    assert sub.choices["calibrate"].get_default("scope") is None, "calibrate covers every scope by default"


def add_report_commands(sub: argparse._SubParsersAction) -> None:
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
    assert {"report", "reports"} <= set(sub.choices), f"report subcommands missing from {sorted(sub.choices)}"
    assert sub.choices["report"].get_default("runs") == 10_000, "default report runs changed"


COMMANDS = {
    "sync-info": cmd_sync_info, "ingest": cmd_ingest, "forecast": cmd_forecast, "epics": cmd_epics,
    "calibrate": cmd_calibrate, "stats": cmd_stats, "aging": cmd_aging, "report": cmd_report, "reports": cmd_reports,
}


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=None, help="SQLite file (see above for default)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for add in (add_sync_commands, add_forecast_commands, add_review_commands, add_report_commands):
        add(sub)
    assert set(sub.choices) == set(COMMANDS), f"parser and handlers disagree: {set(sub.choices) ^ set(COMMANDS)}"
    assert ap.get_default("db") is None, "--db must default to the environment"
    return ap


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    assert args.cmd in COMMANDS, f"no handler for {args.cmd}"
    path = store.db_path(args.db)
    con = store.connect(path)
    assert path.exists(), f"no database at {path} after connecting"
    try:
        COMMANDS[args.cmd](con, args)
    except UserError as e:
        sys.exit(f"error: {e}")
    finally:
        con.close()


if __name__ == "__main__":
    main()
