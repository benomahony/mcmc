"""Read issues fetched from Jira into flat records.

Accepts raw Jira REST/MCP issues (`{key, fields: {...}}`), flat records, a JSON list, a
`{"issues": [...]}` page, JSON lines, or CSV with a header row
(key,type,created,resolved,status_category[,epic]).
"""

from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime
from typing import Any

# Jira status category keys (new/indeterminate/done) and display names, to one vocabulary.
CATEGORIES = {
    "new": "to_do", "to do": "to_do", "todo": "to_do",
    "indeterminate": "in_progress", "in progress": "in_progress",
    "done": "done", "complete": "done",
}
RECORD_KEYS = {"key", "issue_type", "created", "resolved", "done", "subtask", "epic", "status_category"}


def field(issue: dict, *names: str) -> Any:
    """The first of `names` set on the issue itself or under its Jira `fields`."""
    assert names, "name at least one field to look up"
    sources = (issue, issue.get("fields") or {})
    assert all(isinstance(s, dict) for s in sources), f"issue fields must be a mapping, got {type(sources[1]).__name__}"
    for source in sources:
        for name in names:
            if source.get(name) is not None:
                return source[name]
    return None


def day(value: object) -> date | None:
    """The calendar day of an ISO date or timestamp (`2026-08-14`, `...T09:00:00.000+0000`, `... 15:30:00 BST`)."""
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    assert len(text) >= 10, f"too short to hold a date: {value!r}"
    parsed = datetime.fromisoformat(text[:10]).date()
    assert parsed.isoformat() == text[:10], f"{value!r} read as {parsed}"
    return parsed


def epic_key(issue: dict, epic_field: str | None) -> str | None:
    """Epic key: flat `epic` column, Data Center Epic Link custom field, or Cloud `parent`."""
    names = ([epic_field] if epic_field else []) + ["epic", "epic_link", "epic_key", "parent"]
    raw = field(issue, *names)
    value = raw.get("key") if isinstance(raw, dict) else raw
    key = str(value).strip() if value else None
    assert key is None or key, f"epic key is blank for issue {issue.get('key')!r}"
    assert not isinstance(raw, dict) or value is not None, f"parent of {issue.get('key')!r} has no key: {raw!r}"
    return key or None


def issue_type(issue: dict) -> tuple[str | None, bool]:
    """The issue type's name, and whether it is a sub-task."""
    raw = field(issue, "issue_type", "issuetype", "type")
    flagged = isinstance(raw, dict) and bool(raw.get("subtask"))
    name = raw.get("name") if isinstance(raw, dict) else raw
    assert name is None or isinstance(name, str), f"issue type name must be text, got {name!r}"
    subtask = flagged or str(name).lower().replace("-", "") == "subtask"
    assert not flagged or subtask, "a type flagged as a sub-task must be treated as one"
    return name, subtask


def status_category(issue: dict) -> str | None:
    """'to_do', 'in_progress', 'done', or None if the issue doesn't say."""
    category = field(issue, "status_category", "statusCategory")
    status = field(issue, "status")
    if category is None and isinstance(status, dict):
        category = status.get("statusCategory") or status.get("category")
    if isinstance(category, dict):
        category = category.get("key") or category.get("name")
    result = CATEGORIES.get(str(category).strip().lower().replace("_", " ")) if category else None
    assert result in (None, "to_do", "in_progress", "done"), f"unknown category {result!r}"
    assert category is None or result is not None or str(category).strip(), f"blank status category on {issue.get('key')!r}"
    return result


def as_bool(value: object) -> bool:
    """Booleans from JSON (true/false) or CSV text ("true", "false", "1", "0", "")."""
    if isinstance(value, str):
        text = value.strip().lower()
        assert text in ("true", "false", "1", "0", "", "yes", "no"), f"not a yes/no value: {value!r}"
        return text in ("true", "1", "yes")
    assert value is None or isinstance(value, bool | int), f"not a yes/no value: {value!r}"
    return bool(value)


def normalise(issue: dict, epic_field: str | None = None) -> dict:
    """One flat record per issue: key, issue_type, created, resolved, done, subtask, epic, status_category."""
    assert "key" in issue, f"every issue needs a key, got fields {sorted(issue)}"
    name, subtask = issue_type(issue)
    category = status_category(issue)
    resolved = day(field(issue, "resolved", "resolutiondate", "resolution_date"))
    explicit = field(issue, "done")
    done = resolved is not None or category == "done" if explicit is None else as_bool(explicit)
    record = {
        "key": str(issue["key"]),
        "issue_type": name,
        "created": day(field(issue, "created")),
        "resolved": resolved,
        "done": done,
        "subtask": subtask,
        "epic": epic_key(issue, epic_field),
        "status_category": "done" if done else category,
    }
    assert set(record) == RECORD_KEYS, f"record fields drifted: {sorted(record)}"
    assert record["resolved"] is None or record["done"], (
        f"{record['key']} has resolution date {record['resolved']} but done={explicit!r}; a resolved issue is always done"
    )
    return record


def parse_csv(raw: str) -> list[dict]:
    """Rows of a CSV with a header row; blank cells become None."""
    rows = csv.DictReader(io.StringIO(raw))
    columns = [f.strip() for f in rows.fieldnames or []]
    assert "key" in columns, f"CSV needs a key column, got {columns}"
    records = [{k.strip(): (v.strip() or None) for k, v in row.items() if k is not None} for row in rows]
    assert all(set(r) <= set(columns) for r in records), f"rows with cells outside the header {columns}"
    return records


def parse_payload(raw: str) -> list[dict]:
    """Raw issue dicts from JSON (list, page or lines) or CSV text."""
    raw = raw.strip()
    if not raw:
        return []
    if raw[0] in "[{":
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = [json.loads(line) for line in raw.splitlines() if line.strip()]
    else:
        data = parse_csv(raw)
    if isinstance(data, dict):
        data = data.get("issues") or data.get("values") or [data]
    assert isinstance(data, list), f"expected a list of issues, got {type(data).__name__}"
    odd = [i for i in data if not isinstance(i, dict)]
    assert not odd, f"every issue must be a JSON object, got {odd[:2]}"
    return data


def load_issues(raw: str, epic_field: str | None = None) -> list[dict]:
    """Flat records for every issue in a payload."""
    issues = [normalise(i, epic_field) for i in parse_payload(raw)]
    blank = [i for i in issues if not i["key"].strip()]
    assert not blank, f"{len(blank)} issues have a blank key"
    undated = [i["key"] for i in issues if not isinstance(i["created"], date | None)]
    assert not undated, f"created must be a date or missing: {undated}"
    return issues
