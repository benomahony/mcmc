"""The local SQLite store: schema, syncs from Jira, and every query the analysis needs.

Every query is fixed text with bound parameters. Optional filters bind NULL to switch
themselves off. Lists bind as JSON. The issue-type filter, written out in each query that needs it, is
    (CASE WHEN ? IS NULL THEN coalesce(issue_type, '') <> 'Epic'
          ELSE issue_type IN (SELECT value FROM json_each(?)) END)
With no types given,
epics are excluded (they are containers, not deliverable items); otherwise only the listed
types count. Bind it with `Selection.type_params`.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
from collections import defaultdict
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

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
    created DATE CHECK (created IS NULL OR date(created) IS created),
    resolved DATE CHECK (resolved IS NULL OR date(resolved) IS resolved),
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
CREATE TABLE IF NOT EXISTS forecasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT,
    made_at TIMESTAMP,
    kind TEXT,              -- 'when' | 'how_many'
    scope_growth BOOLEAN,   -- retired model; always false for new forecasts
    start DATE,
    target_date DATE,       -- how_many only
    items INTEGER,          -- when only
    types LIST,
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
    chance REAL
);
"""

FULL_SYNC_EVERY = timedelta(days=7)

Connection = sqlite3.Connection

sqlite3.register_adapter(date, date.isoformat)
sqlite3.register_adapter(datetime, datetime.isoformat)
sqlite3.register_adapter(list, json.dumps)
sqlite3.register_converter("DATE", lambda b: date.fromisoformat(b.decode()))
sqlite3.register_converter("TIMESTAMP", lambda b: datetime.fromisoformat(b.decode()))
sqlite3.register_converter("BOOLEAN", lambda b: b != b"0")
sqlite3.register_converter("LIST", lambda b: json.loads(b))


class NoData(Exception):
    """The scope has never been synced, so there is nothing to forecast from."""


@dataclass(frozen=True)
class Window:
    """A history window of whole days, both ends included."""

    start: date
    end: date

    def __post_init__(self) -> None:
        assert self.start <= self.end, f"window starts {self.start} after it ends {self.end}"
        assert (self.end - self.start).days < 3650, f"a {self.days}-day window is beyond any sensible history"

    @property
    def days(self) -> int:
        days = (self.end - self.start).days + 1
        assert days >= 1, f"window {self.start}..{self.end} covers {days} days"
        assert self.start + timedelta(days=days - 1) == self.end, f"{days} days from {self.start} don't end on {self.end}"
        return days

    def as_json(self) -> dict[str, str]:
        """The window's ends as ISO dates."""
        bounds = {"start": self.start.isoformat(), "end": self.end.isoformat()}
        assert bounds["start"] <= bounds["end"], f"window bounds out of order: {bounds}"
        assert len(bounds["start"]) == len(bounds["end"]) == 10, f"ISO dates expected, got {bounds}"
        return bounds


@dataclass(frozen=True)
class Selection:
    """Which issues count: a scope, optionally narrowed to some issue types."""

    scope: str
    types: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        assert self.scope.strip(), "a scope needs a name"
        assert self.types is None or all(t.strip() for t in self.types), f"blank issue type in {self.types}"

    @property
    def type_params(self) -> list[list[str] | None]:
        """The two bindings for the issue-type filter."""
        types = list(self.types) if self.types else None
        assert types is None or len(types) == len(set(types)), f"issue types listed twice: {types}"
        assert types is None or all(isinstance(t, str) for t in types), f"issue types must be names, got {types}"
        return [types, types]


@dataclass(frozen=True)
class Sync:
    """One fetch from Jira: which scope, when, and whether it returned every open item."""

    scope: str
    at: datetime
    full: bool = False
    jql: str | None = None

    def __post_init__(self) -> None:
        assert self.scope.strip(), "a sync needs a scope name"
        assert self.jql is None or self.jql.strip(), f"JQL, when given, can't be blank: {self.jql!r}"


@dataclass(frozen=True)
class SavedForecast:
    """A forecast as stored for later calibration."""

    selection: Selection
    kind: str  # 'when' | 'how_many'
    window: Window
    start: date
    percentiles: dict
    target_date: date | None = None
    items: int | None = None
    keys: Sequence[str] = field(default_factory=tuple)
    epic: str | None = None

    def __post_init__(self) -> None:
        assert self.kind in ("when", "how_many"), f"unknown forecast kind {self.kind!r}"
        assert (self.kind == "how_many") == (self.target_date is not None), (
            f"a {self.kind} forecast {'needs' if self.kind == 'how_many' else 'has no'} target date, got {self.target_date}"
        )


# --- connection -------------------------------------------------------------------------


def db_path(explicit: Path | None) -> Path:
    """--db, else $MCMC_DB, else $CLAUDE_PLUGIN_DATA/mcmc.sqlite, else ~/.local/share/mcmc/mcmc.sqlite."""
    if explicit:
        path = explicit
    elif env := os.environ.get("MCMC_DB"):
        path = Path(env)
    elif data := os.environ.get("CLAUDE_PLUGIN_DATA"):
        path = Path(data) / "mcmc.sqlite"
    else:
        path = Path.home() / ".local/share/mcmc/mcmc.sqlite"
    assert path.name, f"the database path must name a file, got {path!r}"
    assert not path.is_dir(), f"{path} is a directory; point --db or $MCMC_DB at a file"
    return path


def connect(path: Path) -> Connection:
    """Open (creating if needed) the store and bring its schema up to date."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES, isolation_level=None)
    con.executescript(SCHEMA)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
    missing = {"scopes", "issues", "snapshots", "forecasts", "forecast_items", "reports"} - tables
    assert not missing, f"schema setup left tables missing: {sorted(missing)}"
    assert path.exists(), f"SQLite didn't create {path}"
    return con


@contextmanager
def transaction(con: Connection) -> Iterator[None]:
    """All the writes inside happen, or none do."""
    assert isinstance(con, sqlite3.Connection), f"transactions need a SQLite connection, got {type(con).__name__}"
    con.execute("BEGIN")
    committed = False
    try:
        yield
        assert con.execute("SELECT 1").fetchone() == (1,), "the connection was closed inside a transaction"
        con.commit()
        committed = True
    finally:
        if not committed:
            con.rollback()


def rows_as_dicts(cursor: sqlite3.Cursor) -> list[dict]:
    """Rows from the last query as dicts keyed by column name."""
    assert cursor.description is not None, "the last statement returned no result set"
    cols = [c[0] for c in cursor.description]
    rows = [dict(zip(cols, r)) for r in cursor.fetchall()]
    assert len(set(cols)) == len(cols), f"duplicate column names would lose values: {cols}"
    return rows


def one(con: Connection, sql: str, params: Sequence[object]) -> tuple:
    """The single row a query must return."""
    row = con.execute(sql, params).fetchone()
    assert row is not None, f"expected one row from: {sql.split()[0:6]}"
    assert len(row) >= 1, f"empty row from: {sql.split()[0:6]}"
    return row


# --- syncs --------------------------------------------------------------------------------


def ingest(con: Connection, sync: Sync, issues: Sequence[dict]) -> dict:
    """Upsert one sync's issues, record the sync, and snapshot the backlog. Call inside a transaction."""
    con.execute("INSERT INTO scopes VALUES (?, ?, ?, NULL, NULL) ON CONFLICT DO NOTHING", [sync.scope, sync.jql, sync.at])
    if sync.jql:
        con.execute("UPDATE scopes SET jql = ? WHERE name = ?", [sync.jql, sync.scope])
    keys = [i["key"] for i in issues]
    if issues:
        con.executemany(
            "INSERT OR REPLACE INTO issues "
            "(scope, key, issue_type, created, resolved, done, synced_at, epic, status_category) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                [sync.scope, i["key"], i["issue_type"], i["created"], i["resolved"], i["done"], sync.at, i["epic"],
                 i["status_category"]]
                for i in issues
            ],
        )
    removed = drop_departed(con, sync, keys) if sync.full else 0
    con.execute("UPDATE scopes SET last_sync = ? WHERE name = ?", [sync.at, sync.scope])
    open_items, done_items = one(
        con, "SELECT count(*) FILTER (WHERE NOT done), count(*) FILTER (WHERE done) FROM issues WHERE scope = ?", [sync.scope]
    )
    con.execute("INSERT INTO snapshots VALUES (?, ?, ?, ?)", [sync.scope, sync.at, open_items, done_items])
    synced = len(set(keys))
    assert open_items + done_items >= synced, f"{synced} issues synced but only {open_items + done_items} stored"
    done_in_sync = sum(bool(i["done"]) for i in issues)
    assert done_items >= done_in_sync, f"{done_in_sync} done issues synced but only {done_items} stored as done"
    return {"removed": removed, "open": open_items, "done": done_items, "missing_epics": missing_epics(con, sync.scope)}


def drop_departed(con: Connection, sync: Sync, seen: list[str]) -> int:
    """A full sync returns every open item, so open items it didn't see have left the scope."""
    assert sync.full, "only a full sync can tell which open items have left the scope"
    (before,) = one(con, "SELECT count(*) FROM issues WHERE scope = ? AND NOT done", [sync.scope])
    con.execute("DELETE FROM issues WHERE scope = ? AND NOT done AND key NOT IN (SELECT value FROM json_each(?))", [sync.scope, seen])
    con.execute("UPDATE scopes SET last_full_sync = ? WHERE name = ?", [sync.at, sync.scope])
    (after,) = one(con, "SELECT count(*) FROM issues WHERE scope = ? AND NOT done", [sync.scope])
    assert after <= before, f"dropping departed items grew the backlog from {before} to {after}"
    return before - after


def missing_epics(con: Connection, scope: str) -> list[str]:
    """Epics referenced by synced issues whose own issue isn't synced (e.g. resolved before the window)."""
    keys = [
        r[0]
        for r in con.execute(
            """SELECT DISTINCT epic FROM issues WHERE scope = ? AND epic IS NOT NULL
               AND epic NOT IN (SELECT key FROM issues WHERE scope = ?) ORDER BY epic""",
            [scope, scope],
        ).fetchall()
    ]
    assert keys == sorted(keys), "missing epics must come back in key order"
    assert len(keys) == len(set(keys)), f"duplicate epics: {keys}"
    return keys


def sync_state(con: Connection, scope: str, now: datetime) -> dict:
    """What to fetch next: a full sync weekly (or first time), otherwise what changed since the last one."""
    row = con.execute("SELECT jql, last_sync, last_full_sync FROM scopes WHERE name = ?", [scope]).fetchone()
    jql, last, last_full = row or (None, None, None)
    if last is None or last_full is None or now - last_full > FULL_SYNC_EVERY:
        mode, since = "full", None
    else:
        mode = "incremental"
        # One day of overlap absorbs Jira/user timezone differences; upserts are idempotent.
        since = (last.date() - timedelta(days=1)).isoformat()
    assert (mode == "incremental") == (since is not None), f"{mode} sync with since={since}"
    assert row is not None or mode == "full", "a scope never synced needs a full sync"
    return {
        "scope": scope, "known": row is not None, "jql": jql,
        "last_sync": last.isoformat() if last else None,
        "last_full_sync": last_full.isoformat() if last_full else None,
        "mode": mode, "updated_since": since, "missing_epics": missing_epics(con, scope),
    }


def scope_info(con: Connection, scope: str) -> tuple[str | None, datetime | None]:
    """The scope's JQL and last sync time (None, None if never synced)."""
    row = con.execute("SELECT jql, last_sync FROM scopes WHERE name = ?", [scope]).fetchone()
    jql, last = row or (None, None)
    assert jql is None or isinstance(jql, str), f"stored JQL must be text, got {jql!r}"
    assert last is None or isinstance(last, datetime), f"last sync must be a timestamp, got {last!r}"
    return jql, last


def first_completion(con: Connection, scope: str) -> date | None:
    """The earliest resolution date held for the scope: history before it is unknown, not empty."""
    (first,) = one(con, 'SELECT min(resolved) AS "first [DATE]" FROM issues WHERE scope = ?', [scope])
    assert first is None or isinstance(first, date), f"first resolution {first!r} is not a date"
    assert first is None or first <= date.today(), f"first resolution {first} is in the future"
    return first


def last_days(con: Connection, scope: str, days: int, end: date | None = None) -> Window:
    """The last `days` days of history, ending at `end` or the last sync."""
    assert days >= 1, f"a history window needs at least one day, got {days}"
    if end is None:
        _, last = scope_info(con, scope)
        if last is None:
            raise NoData(scope)
        end = last.date()
    window = Window(end - timedelta(days=days - 1), end)
    assert window.days == days, f"{window.days}-day window for {days} days"
    return window


# --- history ------------------------------------------------------------------------------


DATED = {
    "resolved": """SELECT resolved FROM issues WHERE scope = ? AND resolved IS NOT NULL
                   AND (CASE WHEN ? IS NULL THEN coalesce(issue_type, '') <> 'Epic'
                             ELSE issue_type IN (SELECT value FROM json_each(?)) END)""",
    "created": """SELECT created FROM issues WHERE scope = ? AND created IS NOT NULL
                  AND (CASE WHEN ? IS NULL THEN coalesce(issue_type, '') <> 'Epic'
                            ELSE issue_type IN (SELECT value FROM json_each(?)) END)""",
}


def event_dates(con: Connection, sel: Selection, event: str) -> list[date]:
    """When each issue in the selection was `resolved` (throughput) or `created` (new work arriving)."""
    dates = [r[0] for r in con.execute(DATED[event], [sel.scope, *sel.type_params]).fetchall()]
    bad = [d for d in dates if not isinstance(d, date)]
    assert not bad, f"{event} values must be dates, got {bad[:3]}"
    (issues,) = one(con, "SELECT count(*) FROM issues WHERE scope = ?", [sel.scope])
    assert len(dates) <= issues, f"{len(dates)} {event} dates from only {issues} issues in {sel.scope}"
    return dates


def open_keys(con: Connection, sel: Selection) -> list[str]:
    """Keys of the selection's open items."""
    keys = [
        r[0]
        for r in con.execute(
            """SELECT key FROM issues WHERE scope = ? AND NOT done
               AND (CASE WHEN ? IS NULL THEN coalesce(issue_type, '') <> 'Epic'
                     ELSE issue_type IN (SELECT value FROM json_each(?)) END) ORDER BY key""",
            [sel.scope, *sel.type_params],
        ).fetchall()
    ]
    assert len(keys) == len(set(keys)), f"duplicate open keys in {sel.scope}"
    assert keys == sorted(keys), "open keys must come back in key order"
    return keys


# --- epics --------------------------------------------------------------------------------


def epic_groups(con: Connection, scope: str, only: Sequence[str] | None = None) -> list[tuple[str, list[str]]]:
    """(epic, open child keys) for every epic with open children or still open itself."""
    rows = con.execute(
        """
        SELECT epic, json_group_array(key) FILTER (WHERE NOT done) AS open_keys
        FROM issues
        WHERE scope = ? AND epic IS NOT NULL AND coalesce(issue_type, '') <> 'Epic'
          AND (? IS NULL OR epic IN (SELECT value FROM json_each(?)))
        GROUP BY epic
        HAVING count(*) FILTER (WHERE NOT done) > 0
            OR epic IN (SELECT key FROM issues WHERE scope = ? AND issue_type = 'Epic' AND NOT done)
        ORDER BY epic""",
        [scope, list(only) if only else None, list(only) if only else None, scope],
    ).fetchall()
    groups = [(epic, sorted(json.loads(keys))) for epic, keys in rows]
    epics = [g[0] for g in groups]
    assert epics == sorted(epics), f"epics out of key order: {epics}"
    unordered = [e for e, k in groups if k != sorted(k)]
    assert not unordered, f"child keys out of order for {unordered}"
    return groups


def epic_statuses(con: Connection, scope: str) -> dict[str, str]:
    """'done' or 'open' for every epic issue synced in the scope."""
    statuses = {
        key: "done" if done else "open"
        for key, done in con.execute("SELECT key, done FROM issues WHERE scope = ? AND issue_type = 'Epic'", [scope]).fetchall()
    }
    assert set(statuses.values()) <= {"done", "open"}, f"unexpected epic status in {set(statuses.values())}"
    assert all(statuses), f"blank epic key among {sorted(statuses)}"
    return statuses


def open_without_epic(con: Connection, scope: str) -> int:
    """Open items (not epics themselves) that belong to no epic."""
    (count,) = one(
        con,
        "SELECT count(*) FROM issues WHERE scope = ? AND NOT done AND epic IS NULL AND coalesce(issue_type, '') <> 'Epic'",
        [scope],
    )
    (open_total,) = one(con, "SELECT count(*) FROM issues WHERE scope = ? AND NOT done", [scope])
    assert 0 <= count <= open_total, f"{count} unparented of {open_total} open items"
    assert isinstance(count, int), f"count {count!r} isn't a whole number"
    return count


def epic_completion_dates(con: Connection, scope: str, epic: str) -> list[date]:
    """When each of the epic's children was resolved."""
    dates = [
        r[0]
        for r in con.execute(
            "SELECT resolved FROM issues WHERE scope = ? AND epic = ? AND resolved IS NOT NULL ORDER BY resolved",
            [scope, epic],
        ).fetchall()
    ]
    assert dates == sorted(dates), f"{epic} completion dates out of order"
    assert all(isinstance(d, date) for d in dates), f"{epic} has a non-date resolution"
    return dates


def epic_share(con: Connection, scope: str, window: Window) -> float:
    """Share of the window's completed items (epics excluded) that belonged to an epic."""
    (share,) = one(
        con,
        """SELECT count(*) FILTER (WHERE epic IS NOT NULL) * 1.0 / max(count(*), 1) FROM issues
           WHERE scope = ? AND resolved BETWEEN ? AND ? AND coalesce(issue_type, '') <> 'Epic'""",
        [scope, window.start, window.end],
    )
    share = float(share)
    assert not math.isnan(share), f"epic share for {scope} {window.as_json()} is NaN"
    assert 0 <= share <= 1, f"epic share {share} outside 0..1"
    return share


# --- forecasts and calibration -----------------------------------------------------------


def save_forecast(con: Connection, rec: SavedForecast, made_at: datetime) -> int:
    """Store a forecast (and the open items it covers) for later calibration."""
    with transaction(con):
        (fid,) = one(
            con,
            """INSERT INTO forecasts (scope, made_at, kind, scope_growth, start, target_date, items, types,
                                      history_start, history_end, percentiles, epic)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
            [rec.selection.scope, made_at, rec.kind, False, rec.start, rec.target_date, rec.items,
             rec.selection.type_params[0], rec.window.start, rec.window.end, json.dumps(rec.percentiles), rec.epic],
        )
        if rec.keys:
            con.executemany("INSERT INTO forecast_items VALUES (?, ?)", [[fid, k] for k in rec.keys])
    (tracked,) = one(con, "SELECT count(*) FROM forecast_items WHERE forecast_id = ?", [fid])
    assert tracked == len(rec.keys), f"forecast {fid} tracks {tracked} items, expected {len(rec.keys)}"
    assert isinstance(fid, int) and fid > 0, f"bad forecast id {fid!r}"
    return fid


def recorded_forecasts(con: Connection, scope: str | None) -> list[dict]:
    """Every stored forecast (for one scope, or all), oldest first, with its scope's last sync."""
    rows = rows_as_dicts(
        con.execute(
            """SELECT f.*, s.last_sync FROM forecasts f JOIN scopes s ON s.name = f.scope
               WHERE ? IS NULL OR f.scope = ? ORDER BY f.id""",
            [scope, scope],
        )
    )
    assert [r["id"] for r in rows] == sorted(r["id"] for r in rows), "forecasts must come back oldest first"
    assert scope is None or all(r["scope"] == scope for r in rows), f"forecast from another scope than {scope}"
    return rows


def tracked_items(con: Connection, f: dict) -> tuple[int, int, date | None]:
    """For a forecast's tracked items: how many there are, how many are still open, and the last resolution."""
    total, still_open, last = one(
        con,
        """SELECT count(*), count(*) FILTER (WHERE i.key IS NOT NULL AND NOT i.done), max(i.resolved) AS "last [DATE]"
           FROM forecast_items fi LEFT JOIN issues i ON i.scope = ? AND i.key = fi.key
           WHERE fi.forecast_id = ?""",
        [f["scope"], f["id"]],
    )
    assert 0 <= still_open <= total, f"{still_open} open of {total} tracked items"
    assert last is None or isinstance(last, date), f"last resolution {last!r} is not a date"
    return total, still_open, last


def resolved_between(con: Connection, f: dict) -> int:
    """Items resolved after the forecast's start, up to and including its target date."""
    sel = Selection(f["scope"], tuple(f["types"]) if f["types"] else None)
    (count,) = one(
        con,
        """SELECT count(*) FROM issues WHERE scope = ? AND resolved > ? AND resolved <= ?
           AND (CASE WHEN ? IS NULL THEN coalesce(issue_type, '') <> 'Epic'
                     ELSE issue_type IN (SELECT value FROM json_each(?)) END)""",
        [sel.scope, f["start"], f["target_date"], *sel.type_params],
    )
    assert count >= 0, f"negative count {count}"
    assert f["target_date"] >= f["start"], f"target {f['target_date']} before start {f['start']}"
    return count


# --- stats and aging ----------------------------------------------------------------------


def weekly_counts(con: Connection, scope: str, window: Window) -> list[tuple[date, int, int]]:
    """(week start, completed, created) for every week touching the window."""
    first = window.start - timedelta(days=window.start.weekday())
    weeks = [
        (week, *one(
            con,
            """SELECT count(*) FILTER (WHERE resolved BETWEEN ? AND ?), count(*) FILTER (WHERE created BETWEEN ? AND ?)
               FROM issues WHERE scope = ?""",
            [week, week + timedelta(days=6), week, week + timedelta(days=6), scope],
        ))
        for week in (first + timedelta(weeks=n) for n in range((window.end - first).days // 7 + 1))
    ]
    off = [w for w, _, _ in weeks if w.weekday() != 0]
    assert not off, f"weeks must start on Mondays, got {off}"
    assert len(weeks) >= window.days // 7, f"{len(weeks)} weeks for a {window.days}-day window"
    return weeks


def quantile(sorted_values: list[int], q: float) -> float:
    """Linearly interpolated quantile of already sorted values."""
    assert sorted_values, "no values to take a quantile of"
    assert 0 <= q <= 1, f"quantile {q} outside 0..1"
    pos = q * (len(sorted_values) - 1)
    low = math.floor(pos)
    high = min(low + 1, len(sorted_values) - 1)
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (pos - low)


def lead_time_quantiles(con: Connection, scope: str, window: Window) -> list[tuple]:
    """(type, completed, p50, p85, p95 lead time in days) for items resolved in the window."""
    samples: dict[str, list[int]] = defaultdict(list)
    for kind, lead in con.execute(
        """SELECT coalesce(issue_type, '?'), CAST(julianday(resolved) - julianday(created) AS INTEGER) AS lead FROM issues
           WHERE scope = ? AND resolved BETWEEN ? AND ? AND created IS NOT NULL ORDER BY lead""",
        [scope, window.start, window.end],
    ).fetchall():
        samples[kind].append(lead)
    rows = sorted(
        ((kind, len(v), quantile(v, 0.5), quantile(v, 0.85), quantile(v, 0.95)) for kind, v in samples.items()),
        key=lambda r: (-r[1], r[0]),
    )
    disordered = [r for r in rows if not r[2] <= r[3] <= r[4]]
    assert not disordered, f"lead-time quantiles out of order: {disordered}"
    empty = [r[0] for r in rows if r[1] <= 0]
    assert not empty, f"types listed with no completions: {empty}"
    return rows


def open_by_type(con: Connection, scope: str) -> dict[str, int]:
    """Open item count per issue type."""
    counts = dict(
        con.execute(
            "SELECT coalesce(issue_type, '?'), count(*) FROM issues WHERE scope = ? AND NOT done GROUP BY 1", [scope]
        ).fetchall()
    )
    (total,) = one(con, "SELECT count(*) FROM issues WHERE scope = ? AND NOT done", [scope])
    assert sum(counts.values()) == total, f"per-type counts {sum(counts.values())} don't add up to {total} open"
    assert all(n > 0 for n in counts.values()), "a listed type must have open items"
    return counts


def snapshots(con: Connection, scope: str, limit: int) -> list[tuple[datetime, int, int]]:
    """The most recent backlog snapshots, oldest first."""
    assert limit >= 0, f"can't show {limit} snapshots"
    rows = con.execute(
        "SELECT taken_at, open_items, done_items FROM snapshots WHERE scope = ? ORDER BY taken_at DESC LIMIT ?",
        [scope, limit],
    ).fetchall()[::-1]
    assert [r[0] for r in rows] == sorted(r[0] for r in rows), "snapshots must come back oldest first"
    return rows


def lead_times(con: Connection, scope: str, window: Window) -> dict[str | None, list[int]]:
    """Sorted lead times (days from creation to resolution) per type, for items resolved in the window."""
    grouped: dict[str | None, list[int]] = defaultdict(list)
    for kind, lead in con.execute(
        """SELECT issue_type, CAST(julianday(resolved) - julianday(created) AS INTEGER) AS lead FROM issues
           WHERE scope = ? AND resolved BETWEEN ? AND ? AND created IS NOT NULL
             AND coalesce(issue_type, '') <> 'Epic'
           ORDER BY lead""",
        [scope, window.start, window.end],
    ).fetchall():
        grouped[kind].append(lead)
    samples = dict(grouped)
    assert all(v == sorted(v) for v in samples.values()), "lead times must come back sorted"
    assert all(d >= 0 for v in samples.values() for d in v), "an item was resolved before it was created"
    return samples


def open_item_ages(con: Connection, scope: str, as_of: date) -> list[tuple[str, str | None, str | None, str, int]]:
    """(key, type, epic, status category, age in days) for open items (not epics), oldest first."""
    rows = con.execute(
        """SELECT key, issue_type, epic, coalesce(status_category, 'unknown'), CAST(julianday(?) - julianday(created) AS INTEGER) AS age FROM issues
           WHERE scope = ? AND NOT done AND created IS NOT NULL AND coalesce(issue_type, '') <> 'Epic'
           ORDER BY age DESC, key""",
        [as_of, scope],
    ).fetchall()
    ages = [r[4] for r in rows]
    assert ages == sorted(ages, reverse=True), f"open items must come back oldest first, got ages {ages[:5]}..."
    odd = [a for a in ages if not isinstance(a, int)]
    assert not odd, f"ages must be whole days, got {odd[:3]}"
    return rows


# --- reports ------------------------------------------------------------------------------


def record_report(con: Connection, row: dict) -> None:
    """Remember a saved report and its headline numbers."""
    assert set(row) == {"scope", "created_at", "path", "as_of", "open_items", "p85", "shrinking", "target_date", "chance"}, (
        f"report row fields drifted: {sorted(row)}"
    )
    assert Path(row["path"]).is_absolute(), f"report paths are stored absolute, got {row['path']}"
    con.execute(
        "INSERT INTO reports VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [row["scope"], row["created_at"], row["path"], row["as_of"], row["open_items"], row["p85"],
         row["shrinking"], row["target_date"], row["chance"]],
    )


def saved_reports(con: Connection, scope: str | None = None) -> list[dict]:
    """Recorded reports whose files still exist, newest first."""
    rows = rows_as_dicts(
        con.execute("SELECT * FROM reports WHERE ? IS NULL OR scope = ? ORDER BY created_at DESC", [scope, scope])
    )
    existing = [r for r in rows if Path(r["path"]).exists()]
    times = [r["created_at"] for r in existing]
    assert times == sorted(times, reverse=True), f"reports must come back newest first: {times}"
    assert scope is None or all(r["scope"] == scope for r in existing), f"report from another scope than {scope}"
    return existing
