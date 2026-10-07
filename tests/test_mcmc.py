import json
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import jira_input
import mcmc
import pytest
import store

START = date(2026, 7, 1)


@pytest.fixture
def db(tmp_path):
    return tmp_path / "t.sqlite"


def run(db, capsys, *argv):
    mcmc.main(["--db", str(db), *argv])
    return capsys.readouterr().out


def write(tmp_path, issues, name="issues.json"):
    path = tmp_path / name
    path.write_text(json.dumps(issues, default=str))
    return str(path)


def jira_issue(key, created, resolved=None, kind="Story"):
    """Shape returned by Jira REST / the Atlassian MCP."""
    return {
        "key": key,
        "fields": {
            "issuetype": {"name": kind},
            "created": f"{created}T09:00:00.000+0000",
            "resolutiondate": f"{resolved}T17:00:00.000+0000" if resolved else None,
            "status": {"statusCategory": {"key": "done" if resolved else "indeterminate"}},
        },
    }


def test_normalise_accepts_jira_and_flat_shapes():
    assert jira_input.normalise(jira_issue("A-1", "2026-07-01", "2026-07-05")) == {
        "key": "A-1", "issue_type": "Story", "created": date(2026, 7, 1), "resolved": date(2026, 7, 5), "done": True,
        "subtask": False, "epic": None, "status_category": "done",
    }
    flat = jira_input.normalise({"key": "A-2", "type": "Bug", "created": "2026-07-01", "status_category": "In Progress"})
    assert flat["done"] is False and flat["issue_type"] == "Bug" and flat["status_category"] == "in_progress"


def test_ingest_csv_drops_subtasks(db, capsys, tmp_path):
    path = tmp_path / "issues.csv"
    path.write_text(
        "key,type,created,resolved,status_category\n"
        "PAY-1,Story,2026-07-01,2026-07-05,Done\n"
        "PAY-2,Bug,2026-07-02 15:30:00 BST,,In Progress\n"
        "PAY-3,Sub-task,2026-07-02,,To Do\n"
    )
    out = json.loads(run(db, capsys, "ingest", "PAY", str(path), "--full"))
    assert (out["ingested"], out["skipped_subtasks"], out["open"], out["done"]) == (2, 1, 1, 1)

    # One file per page, each with its own header, ingested together (a full sync spans all pages).
    page2 = tmp_path / "page2.csv"
    page2.write_text("key,type,created,resolved,status_category\nPAY-4,Task,2026-07-03,,To Do\n")
    out = json.loads(run(db, capsys, "ingest", "PAY", str(path), str(page2), "--full"))
    assert (out["ingested"], out["removed"], out["open"]) == (3, 0, 2)


def test_sync_info_full_then_incremental(db, capsys, tmp_path):
    assert json.loads(run(db, capsys, "sync-info", "PAY"))["mode"] == "full"
    run(db, capsys, "ingest", "PAY", write(tmp_path, [jira_issue("PAY-1", "2026-07-01")]), "--full", "--jql", "project = PAY")
    info = json.loads(run(db, capsys, "sync-info", "PAY"))
    assert info["mode"] == "incremental"
    assert info["jql"] == "project = PAY"
    assert info["updated_since"] == (date.today() - timedelta(days=1)).isoformat()


def test_incremental_upsert_and_full_sync_removes_departed(db, capsys, tmp_path):
    run(db, capsys, "ingest", "PAY", write(tmp_path, [jira_issue("PAY-1", START), jira_issue("PAY-2", START)]), "--full")
    out = json.loads(run(db, capsys, "ingest", "PAY", write(tmp_path, [jira_issue("PAY-1", START, START + timedelta(3))])))
    assert (out["open"], out["done"]) == (1, 1)
    out = json.loads(run(db, capsys, "ingest", "PAY", write(tmp_path, []), "--full"))
    assert out == {
        "scope": "PAY", "ingested": 0, "skipped_subtasks": 0, "removed": 1, "open": 0, "done": 1, "missing_epics": [],
    }


def seed_steady(db, capsys, tmp_path, days=60, open_items=20):
    # One Story (created before the window) resolved per day, one Bug created every other
    # day, plus an open backlog of Stories.
    issues = [jira_issue(f"S-{i}", START, START + timedelta(i)) for i in range(1, days + 1)]
    issues += [jira_issue(f"B-{i}", START + timedelta(2 * i), kind="Bug") for i in range(1, days // 2 + 1)]
    issues += [jira_issue(f"O-{i}", START, kind="Story") for i in range(open_items)]
    run(db, capsys, "ingest", "PAY", write(tmp_path, issues), "--full", "--as-of", str(START + timedelta(days)))


def test_forecast_when_uses_db_backlog_and_type_filter(db, capsys, tmp_path):
    seed_steady(db, capsys, tmp_path)
    out = json.loads(run(db, capsys, "forecast", "PAY", "--type", "Story", "--window", "60", "--seed", "1", "--json"))
    assert out["items"] == 20
    assert out["history"]["completed"] == 60
    # Stories: throughput is exactly 1/day, no Story arrivals in window except the backlog's creation day.
    end = START + timedelta(60)
    assert out["percentiles"]["p85"] == (end + timedelta(20)).isoformat()


def test_when_forecast_covers_todays_backlog_and_says_to_reforecast(db, capsys, tmp_path):
    seed_steady(db, capsys, tmp_path)
    # Bugs arrive at 0.5/day while 1/day is finished: no warning. Add a flood of new work and it warns.
    out = json.loads(run(db, capsys, "forecast", "PAY", "--window", "60", "--seed", "1", "--json"))
    assert out["items"] == 50, f"20 open stories + 30 open bugs, got {out['items']}"
    assert set(out["percentiles"]) == {"p50", "p70", "p85", "p95"}, f"one answer per percentile, got {out['percentiles']}"
    assert "warnings" not in out, f"no intake warning while the backlog shrinks: {out.get('warnings')}"
    flood = [jira_issue(f"N-{i}", START + timedelta(1 + i % 60)) for i in range(120)]
    run(db, capsys, "ingest", "PAY", write(tmp_path, flood), "--as-of", str(START + timedelta(60)))
    out = json.loads(run(db, capsys, "forecast", "PAY", "--window", "60", "--seed", "1", "--no-record", "--json"))
    assert any("forecast again" in w for w in out["warnings"]), f"expected a re-forecast warning, got {out['warnings']}"


def test_how_many_records_and_calibrates(db, capsys, tmp_path):
    seed_steady(db, capsys, tmp_path, days=30)
    end = START + timedelta(30)
    out = json.loads(run(db, capsys, "forecast", "PAY", "--by", str(end + timedelta(10)), "--window", "30", "--json"))
    assert out["percentiles"]["p85"] == 10

    cal = json.loads(run(db, capsys, "calibrate", "--json"))
    assert cal["forecasts"][0]["actual"] is None  # target date not reached by sync yet

    later = [jira_issue(f"L-{i}", end, end + timedelta(i)) for i in range(1, 9)]  # only 8 done in 10 days
    run(db, capsys, "ingest", "PAY", write(tmp_path, later), "--as-of", str(end + timedelta(11)))
    cal = json.loads(run(db, capsys, "calibrate", "--json"))
    assert cal["forecasts"][0]["actual"] == 8
    assert cal["summary"]["how_many"]["p85"] == {"held": 0, "missed": 1, "pending": 0}


def test_when_calibration_tracks_original_backlog(db, capsys, tmp_path):
    seed_steady(db, capsys, tmp_path, open_items=5)
    run(db, capsys, "forecast", "PAY", "--type", "Story", "--window", "60", "--json")
    end = START + timedelta(60)
    done = [jira_issue(f"O-{i}", START, end + timedelta(i + 1)) for i in range(5)]
    run(db, capsys, "ingest", "PAY", write(tmp_path, done), "--as-of", str(end + timedelta(6)))
    f = json.loads(run(db, capsys, "calibrate", "PAY", "--json"))["forecasts"][0]
    assert f["actual"] == (end + timedelta(5)).isoformat()
    assert all(f["hits"].values())


def test_stats_reports_lead_time_by_type(db, capsys, tmp_path):
    seed_steady(db, capsys, tmp_path)
    out = json.loads(run(db, capsys, "stats", "PAY", "--window", "60", "--json"))
    types = {t["type"]: t for t in out["by_type"]}
    assert types["Story"]["completed"] == 60 and types["Story"]["lead_time_days"]["p50"] == 30.5
    assert types["Bug"]["open"] == 30 and types["Bug"]["lead_time_days"] is None
    assert len(out["snapshots"]) == 1


def test_epic_link_from_cloud_parent_or_custom_field():
    cloud = jira_issue("A-1", "2026-07-01")
    cloud["fields"]["parent"] = {"key": "A-100", "fields": {"issuetype": {"name": "Epic"}}}
    assert jira_input.normalise(cloud)["epic"] == "A-100"
    dc = jira_issue("A-2", "2026-07-01")
    dc["fields"]["customfield_10008"] = "A-200"
    assert jira_input.normalise(dc, "customfield_10008")["epic"] == "A-200"
    assert jira_input.normalise(dc)["epic"] is None


def test_epics_forecast_current_pace_vs_sole_focus(db, capsys, tmp_path):
    end = START + timedelta(59)
    rows = ["key,type,created,resolved,status_category,epic", f"E-1,Epic,{START},,In Progress,"]
    # Team finishes 2/day; one of those per 4 days is in epic E-1, which has 6 open children.
    for d in range(60):
        day = START + timedelta(d)
        rows.append(f"T-{d},Story,{START},{day},Done,")
        rows.append(f"U-{d},Story,{START},{day},Done," + ("E-1" if d % 4 == 0 else ""))
    rows += [f"C-{i},Story,{START},,To Do,E-1" for i in range(6)]
    rows.append(f"Z-1,Story,{START},,To Do,E-2")  # epic issue not synced, but has open children
    path = tmp_path / "e.csv"
    path.write_text("\n".join(rows))
    run(db, capsys, "ingest", "PAY", str(path), "--full", "--as-of", str(end))

    assert json.loads(run(db, capsys, "sync-info", "PAY"))["missing_epics"] == ["E-2"]

    out = json.loads(run(db, capsys, "epics", "PAY", "--window", "60", "--seed", "1", "--json"))
    e1, e2 = out["epics"]
    assert (e1["status"], e2["status"]) == ("open", "not synced")
    assert (e1["epic"], e1["open"], e1["completed_in_window"]) == ("E-1", 6, 15)
    assert e1["sole_focus"]["p85"] == (end + timedelta(3)).isoformat()  # 2/day team throughput
    assert e1["current_pace"]["p85"] > (end + timedelta(20)).isoformat()  # ~0.25/day own pace
    assert (e2["epic"], e2["current_pace"]) == ("E-2", None)  # no completions → no pace forecast
    assert out["open_without_epic"] == 0

    prio = json.loads(run(db, capsys, "epics", "PAY", "--order", "E-1", "--window", "60", "--no-record", "--json"))
    assert prio["priority"] == {"order": ["E-1"], "wip": 1, "epic_share": 0.125}  # 15 of 120 completions
    # Prioritising E-1 with half the team's throughput lands it between its current pace and sole focus.
    prio = json.loads(
        run(db, capsys, "epics", "PAY", "--order", "E-1", "--epic-share", "0.5", "--window", "60", "--no-record", "--json")
    )
    e1 = next(e for e in prio["epics"] if e["epic"] == "E-1")
    assert e1["sole_focus"]["p85"] < e1["priority"]["p85"] < e1["current_pace"]["p85"]
    with pytest.raises(SystemExit):
        run(db, capsys, "epics", "PAY", "--order", "NOPE-1")

    # Epic issues themselves are not backlog items.
    assert json.loads(run(db, capsys, "forecast", "PAY", "--no-record", "--json"))["items"] == 7

    cal = json.loads(run(db, capsys, "calibrate", "--json"))
    assert "epic/current_pace" in cal["summary"]


def test_aging_flags_items_older_than_their_type_lead_time(db, capsys, tmp_path):
    end = START + timedelta(89)
    rows = ["key,type,created,resolved,status_category"]
    # 20 Stories that each took 1..20 days; only 3 Bugs (too few, so Bugs compare against all types).
    rows += [f"S-{i},Story,{end - timedelta(i + 1)},{end - timedelta(1)},Done" for i in range(1, 21)]
    rows += [f"B-{i},Bug,{end - timedelta(3)},{end - timedelta(2)},Done" for i in range(3)]
    rows += [
        f"O-1,Story,{end - timedelta(5)},,In Progress",   # young: ok
        f"O-2,Story,{end - timedelta(19)},,In Progress",  # > p85 (18) but not > p95 (20): at risk
        f"O-3,Story,{end - timedelta(60)},,To Do",        # way past p95: stale
        f"O-4,Bug,{end - timedelta(40)},,In Progress",    # all-types basis
    ]
    path = tmp_path / "a.csv"
    path.write_text("\n".join(rows))
    run(db, capsys, "ingest", "PAY", str(path), "--full", "--as-of", str(end))

    out = json.loads(run(db, capsys, "aging", "PAY", "--json"))
    assert (out["open"], out["stale"], out["at_risk"]) == (4, 2, 1)
    risk = {r["key"]: r for r in out["items"]}
    assert set(risk) == {"O-2", "O-3", "O-4"}
    assert (risk["O-3"]["risk"], risk["O-3"]["status"], risk["O-3"]["older_than_pct_of_completed"]) == ("stale", "to_do", 100)
    assert risk["O-2"]["risk"] == "at risk"
    assert risk["O-4"]["basis"] == "all types"
    assert len(json.loads(run(db, capsys, "aging", "PAY", "--all", "--json"))["items"]) == 4


def test_chance_of_hitting_target_date_and_count(db, capsys, tmp_path):
    seed_steady(db, capsys, tmp_path)  # Stories: exactly 1/day; 20 open Stories
    end = START + timedelta(60)
    def f(*a):
        return json.loads(run(db, capsys, "forecast", "PAY", "--type", "Story", "--window", "60", "--no-record", "--json", *a))
    assert f("--target-date", str(end + timedelta(20)))["chance"] == 1.0
    assert f("--target-date", str(end + timedelta(19)))["chance"] == 0.0
    assert f("--by", str(end + timedelta(10)), "--at-least", "10")["chance"] == 1.0
    assert f("--by", str(end + timedelta(10)), "--at-least", "11")["chance"] == 0.0
    with pytest.raises(SystemExit):
        f("--target-date", str(end))


def test_epics_chance_by_target_date(db, capsys, tmp_path):
    rows = ["key,type,created,resolved,status_category,epic"]
    rows += [f"T-{d},Story,{START},{START + timedelta(d)},Done,E-1" for d in range(60)]  # E-1: 1/day
    rows += [f"C-{i},Story,{START},,To Do,E-1" for i in range(5)]
    path = tmp_path / "e.csv"
    path.write_text("\n".join(rows))
    end = START + timedelta(59)
    run(db, capsys, "ingest", "PAY", str(path), "--full", "--as-of", str(end))
    def e(days):
        return json.loads(
            run(db, capsys, "epics", "PAY", "--window", "60", "--order", "E-1", "--epic-share", "1",
                "--target-date", str(end + timedelta(days)), "--no-record", "--json")
        )["epics"][0]["chance"]
    assert e(5) == {"current_pace": 1.0, "sole_focus": 1.0, "priority": 1.0}
    assert e(4) == {"current_pace": 0.0, "sole_focus": 0.0, "priority": 0.0}


def test_every_command_prints_text(db, capsys, tmp_path):
    seed_steady(db, capsys, tmp_path)
    for argv in (
        ["sync-info", "PAY"],
        ["forecast", "PAY", "--runs", "200", "--target-date", "2026-12-01"],
        ["forecast", "PAY", "--runs", "200", "--by", "2026-12-01", "--at-least", "5"],
        ["epics", "PAY", "--runs", "200"],
        ["calibrate"],
        ["stats", "PAY"],
        ["aging", "PAY"],
    ):
        assert run(db, capsys, *argv).strip(), argv


def test_report_is_self_contained_html_and_escapes_jira_text(db, capsys, tmp_path):
    seed_steady(db, capsys, tmp_path)
    hostile = tmp_path / "hostile.csv"
    hostile.write_text(
        "key,type,created,resolved,status_category,epic\n"
        f"X-1,<img src=x onerror=alert(1)>,{START},,In Progress,<script>alert(1)</script>\n"
    )
    run(db, capsys, "ingest", "PAY", str(hostile), "--as-of", str(START + timedelta(60)))
    out = tmp_path / "r.html"
    printed = run(db, capsys, "report", "PAY", "--runs", "300", "--seed", "1", "--target-date", "2026-12-01",
                  "--order", "<script>alert(1)</script>", "--out", str(out))
    assert printed.strip() == str(out)
    html = out.read_text()
    for section in ("The short version", "When will today's backlog be done?", "Is the backlog shrinking?",
                    "When will the planned epics land?", "What looks stuck?"):
        assert section in html
    assert "<script>alert" not in html and "<img src=x" not in html
    assert "&lt;script&gt;" in html
    assert "http://" not in html and "https://" not in html  # no external requests
    assert "None" not in html and "nan" not in html


def test_reports_are_kept_with_history_and_indexed(db, capsys, tmp_path):
    seed_steady(db, capsys, tmp_path)
    first = Path(run(db, capsys, "report", "PAY", "--runs", "200").strip())
    second = Path(run(db, capsys, "report", "PAY", "--runs", "200").strip())
    assert first != second and first.exists() and second.exists()  # back to back, nothing overwritten
    assert first.parent == db.parent / "reports" / "PAY"
    assert re.fullmatch(r"PAY-\d{4}-\d\d-\d\d-\d{6}(-\d+)?\.html", first.name)

    listing = json.loads(run(db, capsys, "reports", "--json"))
    assert [Path(r["path"]) for r in listing["reports"]] == [second, first]  # newest first
    index = Path(listing["index"]).read_text()
    assert f'href="PAY/{second.name}"' in index and "latest" in index

    second.unlink()  # deleted files drop out of the listing
    assert [Path(r["path"]) for r in json.loads(run(db, capsys, "reports", "--json"))["reports"]] == [first]


def test_write_new_never_overwrites(tmp_path):
    path = tmp_path / "PAY-2026-09-01-090000.html"
    first, second, third = (mcmc.write_new(path, text) for text in ("a", "b", "c"))
    assert [p.name for p in (first, second, third)] == [
        "PAY-2026-09-01-090000.html", "PAY-2026-09-01-090000-2.html", "PAY-2026-09-01-090000-3.html",
    ]
    assert [p.read_text() for p in (first, second, third)] == ["a", "b", "c"]


def test_failed_ingest_leaves_nothing_half_written(db, capsys, tmp_path):
    run(db, capsys, "ingest", "PAY", write(tmp_path, [jira_issue("PAY-1", START)]), "--full", "--jql", "project = PAY")
    bad = jira_input.normalise(jira_issue("PAY-2", START)) | {"created": "not a date"}
    con = store.connect(db)
    # The JQL update succeeds, then the issue insert fails: the update must be rolled back with it.
    with pytest.raises(sqlite3.Error), store.transaction(con):
        store.ingest(con, store.Sync("PAY", datetime(2026, 9, 1), full=True, jql="project = OTHER"), [bad])
    assert con.execute("SELECT jql FROM scopes").fetchall() == [("project = PAY",)]
    assert con.execute("SELECT key FROM issues").fetchall() == [("PAY-1",)]
    assert con.execute("SELECT count(*) FROM snapshots").fetchall() == [(1,)]
    con.close()


def test_history_before_the_first_completion_is_not_counted_as_empty_days(db, capsys, tmp_path):
    # An export with 30 days of completions (1 a day) but the default 90-day window.
    end = START + timedelta(29)
    issues = [jira_issue(f"S-{i}", START, START + timedelta(i)) for i in range(30)]
    issues += [jira_issue(f"O-{i}", START) for i in range(10)]
    run(db, capsys, "ingest", "PAY", write(tmp_path, issues), "--full", "--as-of", str(end))
    out = json.loads(run(db, capsys, "forecast", "PAY", "--seed", "1", "--no-record", "--json"))
    assert out["history"]["days"] == 30, f"history should start at the first completion, got {out['history']}"
    assert out["history"]["completed_per_week"] == 7.0, f"1 a day is 7 a week, got {out['history']}"
    assert out["percentiles"]["p85"] == (end + timedelta(10)).isoformat(), f"10 items at 1/day, got {out['percentiles']}"
