"""Integration test against jira820, an off-the-shelf stateful Jira DC 8.20 mock.

Drives the same JQL the skill uses over the REST API: full sync, a change in Jira,
incremental sync, then forecast/stats/calibrate from the stored history.
"""

import json
from datetime import date, timedelta

import pytest

jira820 = pytest.importorskip("jira820.server")
import mcmc
from fastapi.testclient import TestClient
from jira820.config import Config

FIELDS = "issuetype,created,resolutiondate,status,customfield_10008"  # Epic Link on DC


@pytest.fixture
def jira():
    return TestClient(jira820.make_app(config=Config(project_key="PAY", seed="42", locale="en")))


def search(jira, jql, page_size=50):
    """Page through every result, as the skill instructs."""
    issues, start = [], 0
    while True:
        r = jira.get("/rest/api/2/search", params={"jql": jql, "fields": FIELDS, "startAt": start, "maxResults": page_size})
        r.raise_for_status()
        body = r.json()
        issues += body["issues"]
        start += len(body["issues"])
        if not body["issues"] or start >= body["total"]:
            return issues


def cli(db, capsys, *argv):
    mcmc.main(["--db", str(db), *argv])
    return json.loads(capsys.readouterr().out)


def ingest_pages(db, capsys, tmp_path, issues, *flags):
    path = tmp_path / "issues.json"
    path.write_text(json.dumps(issues))
    return cli(db, capsys, "ingest", "PAY", str(path), "--jql", "project = PAY", "--epic-field", "customfield_10008", *flags)


def done_transition(jira, key):
    transitions = jira.get(f"/rest/api/2/issue/{key}/transitions").json()["transitions"]
    return next(t["id"] for t in transitions if t["to"]["statusCategory"]["key"] == "done")


def test_full_then_incremental_sync_and_forecast(jira, tmp_path, capsys):
    db = tmp_path / "mcmc.sqlite"
    assert cli(db, capsys, "sync-info", "PAY")["mode"] == "full"

    full = search(jira, "project = PAY AND (statusCategory != Done OR resolved >= -90d)", page_size=37)
    out = ingest_pages(db, capsys, tmp_path, full, "--full")
    # jira820 ignores `subTaskIssueTypes()`, so ingest has to drop sub-tasks itself.
    subtasks = sum(i["fields"]["issuetype"]["subtask"] for i in full)
    assert out["skipped_subtasks"] == subtasks > 0
    assert out["open"] + out["done"] == len(full) - subtasks
    assert out["done"] == sum(
        not i["fields"]["issuetype"]["subtask"]
        for i in search(jira, "project = PAY AND statusCategory = Done AND resolved >= -90d")
    )

    forecast = cli(db, capsys, "forecast", "PAY", "--seed", "1", "--runs", "500", "--json")
    open_epics = sum(
        i["fields"]["issuetype"]["name"] == "Epic" and i["fields"]["status"]["statusCategory"]["key"] != "done"
        for i in full
    )
    assert forecast["items"] == out["open"] - open_epics

    # Epics resolved before the window aren't in the full pull; fetch them by key.
    assert out["missing_epics"]
    epic_issues = search(jira, f"key in ({', '.join(out['missing_epics'])})")
    out = ingest_pages(db, capsys, tmp_path, epic_issues)
    assert out["missing_epics"] == []

    epics = cli(db, capsys, "epics", "PAY", "--seed", "1", "--runs", "500", "--json")["epics"]
    assert all(e["status"] != "not synced" for e in epics)
    linked = {i["fields"].get("customfield_10008") for i in full} - {None}
    assert {e["epic"] for e in epics} <= linked and epics
    for e in epics:
        children = search(jira, f'"Epic Link" = {e["epic"]} AND statusCategory != Done')
        assert e["open"] == sum(not c["fields"]["issuetype"]["subtask"] for c in children)
    assert forecast["history"]["completed"] > 20
    assert forecast["percentiles"]["p85"] > date.today().isoformat()

    # Close three open items in Jira, then sync only what changed.
    open_keys = [
        i["key"] for i in full
        if i["fields"]["status"]["statusCategory"]["key"] != "done" and not i["fields"]["issuetype"]["subtask"]
    ][:3]
    for key in open_keys:
        r = jira.post(f"/rest/api/2/issue/{key}/transitions", json={"transition": {"id": done_transition(jira, key)}})
        assert r.status_code == 204

    info = cli(db, capsys, "sync-info", "PAY")
    assert info["mode"] == "incremental"
    changed = search(jira, f'{info["jql"]} AND updated >= "{info["updated_since"]}"')
    assert set(open_keys) <= {i["key"] for i in changed}
    out2 = ingest_pages(db, capsys, tmp_path, changed)
    assert (out2["open"], out2["done"]) == (out["open"] - 3, out["done"] + 3)

    stats = cli(db, capsys, "stats", "PAY", "--json")
    assert len(stats["snapshots"]) == 3  # full sync, epic backfill, incremental
    assert sum(t["completed"] for t in stats["by_type"]) > 0

    # Closing items can finish an epic outright; its recorded forecast then resolves (and
    # held, since it finished today). Everything else is still pending.
    cal = cli(db, capsys, "calibrate", "PAY", "--json")
    finished = {e["epic"] for e in epics if e["open"] and all(
        c["key"] in open_keys for c in search(jira, f'"Epic Link" = {e["epic"]}')
        if c["fields"]["status"]["statusCategory"]["key"] != "done" or c["key"] in open_keys
    )}
    for f in cal["forecasts"]:
        expect = True if f["epic"] in finished else None
        assert all(v is expect for v in f["hits"].values()), f
    assert date.fromisoformat(stats["window"][1]) - date.fromisoformat(stats["window"][0]) == timedelta(days=89)
