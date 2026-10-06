import json
from datetime import date, datetime, timedelta

import duckdb
import pytest

import mcmc

START = date(2026, 7, 1)


@pytest.fixture
def db(tmp_path):
    return tmp_path / "t.duckdb"


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


def set_last_sync(db, when: date):
    con = duckdb.connect(str(db))
    con.execute("UPDATE scopes SET last_sync = ?", [datetime.combine(when, datetime.min.time())])
    con.close()


def test_normalise_accepts_jira_and_flat_shapes():
    assert mcmc.normalise(jira_issue("A-1", "2026-07-01", "2026-07-05")) == {
        "key": "A-1", "issue_type": "Story", "created": date(2026, 7, 1), "resolved": date(2026, 7, 5), "done": True,
        "subtask": False, "epic": None,
    }
    flat = mcmc.normalise({"key": "A-2", "type": "Bug", "created": "2026-07-01", "status_category": "In Progress"})
    assert flat["done"] is False and flat["issue_type"] == "Bug"


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
    assert out == {"scope": "PAY", "ingested": 0, "skipped_subtasks": 0, "removed": 1, "open": 0, "done": 1}


def seed_steady(db, capsys, tmp_path, days=60, open_items=20):
    # One Story (created before the window) resolved per day, one Bug created every other
    # day, plus an open backlog of Stories.
    issues = [jira_issue(f"S-{i}", START, START + timedelta(i)) for i in range(1, days + 1)]
    issues += [jira_issue(f"B-{i}", START + timedelta(2 * i), kind="Bug") for i in range(1, days // 2 + 1)]
    issues += [jira_issue(f"O-{i}", START, kind="Story") for i in range(open_items)]
    run(db, capsys, "ingest", "PAY", write(tmp_path, issues), "--full")
    set_last_sync(db, START + timedelta(days))


def test_forecast_when_uses_db_backlog_and_type_filter(db, capsys, tmp_path):
    seed_steady(db, capsys, tmp_path)
    out = json.loads(run(db, capsys, "forecast", "PAY", "--type", "Story", "--window", "60", "--seed", "1", "--json"))
    assert out["items"] == 20
    assert out["history"]["completed"] == 60
    # Stories: throughput is exactly 1/day, no Story arrivals in window except the backlog's creation day.
    end = START + timedelta(60)
    assert out["percentiles"]["no_growth"]["p85"] == (end + timedelta(20)).isoformat()


def test_scope_growth_forecast_is_later(db, capsys, tmp_path):
    seed_steady(db, capsys, tmp_path)
    out = json.loads(run(db, capsys, "forecast", "PAY", "--window", "60", "--seed", "1", "--json"))
    assert out["items"] == 50  # 20 open stories + 30 open bugs
    p = out["percentiles"]
    assert p["scope_growth"]["p85"] > p["no_growth"]["p85"]
    assert set(out["forecast_ids"]) == {"no_growth", "scope_growth"}


def test_how_many_records_and_calibrates(db, capsys, tmp_path):
    seed_steady(db, capsys, tmp_path, days=30)
    end = START + timedelta(30)
    out = json.loads(run(db, capsys, "forecast", "PAY", "--by", str(end + timedelta(10)), "--window", "30", "--json"))
    assert out["percentiles"]["p85"] == 10

    cal = json.loads(run(db, capsys, "calibrate", "--json"))
    assert cal["forecasts"][0]["actual"] is None  # target date not reached by sync yet

    later = [jira_issue(f"L-{i}", end, end + timedelta(i)) for i in range(1, 9)]  # only 8 done in 10 days
    run(db, capsys, "ingest", "PAY", write(tmp_path, later))
    set_last_sync(db, end + timedelta(11))
    cal = json.loads(run(db, capsys, "calibrate", "--json"))
    assert cal["forecasts"][0]["actual"] == 8
    assert cal["summary"]["how_many"]["p85"] == {"held": 0, "missed": 1, "pending": 0}


def test_when_calibration_tracks_original_backlog(db, capsys, tmp_path):
    seed_steady(db, capsys, tmp_path, open_items=5)
    run(db, capsys, "forecast", "PAY", "--type", "Story", "--window", "60", "--no-scope-growth", "--json")
    end = START + timedelta(60)
    done = [jira_issue(f"O-{i}", START, end + timedelta(i + 1)) for i in range(5)]
    run(db, capsys, "ingest", "PAY", write(tmp_path, done))
    set_last_sync(db, end + timedelta(6))
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
    assert mcmc.normalise(cloud)["epic"] == "A-100"
    dc = jira_issue("A-2", "2026-07-01")
    dc["fields"]["customfield_10008"] = "A-200"
    assert mcmc.normalise(dc, "customfield_10008")["epic"] == "A-200"
    assert mcmc.normalise(dc)["epic"] is None


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
    run(db, capsys, "ingest", "PAY", str(path), "--full")
    set_last_sync(db, end)

    out = json.loads(run(db, capsys, "epics", "PAY", "--window", "60", "--seed", "1", "--json"))
    e1, e2 = out["epics"]
    assert (e1["epic"], e1["open"], e1["completed_in_window"]) == ("E-1", 6, 15)
    assert e1["sole_focus"]["p85"] == (end + timedelta(3)).isoformat()  # 2/day team throughput
    assert e1["current_pace"]["p85"] > (end + timedelta(20)).isoformat()  # ~0.25/day own pace
    assert (e2["epic"], e2["current_pace"]) == ("E-2", None)  # no completions → no pace forecast
    assert out["open_without_epic"] == 0

    # Epic issues themselves are not backlog items.
    assert json.loads(run(db, capsys, "forecast", "PAY", "--no-record", "--json"))["items"] == 7

    cal = json.loads(run(db, capsys, "calibrate", "--json"))
    assert "epic/current_pace" in cal["summary"]
