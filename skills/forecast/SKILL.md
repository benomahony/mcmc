---
name: forecast
description: Monte Carlo delivery forecast from Jira history. Use when the user asks "when will this epic/project/backlog be done?", "when will each epic land?", "what work is stuck or ageing?", "how many items can we finish by <date>?", wants a probabilistic/throughput-based forecast, throughput or lead-time stats, or wants to check how accurate past forecasts were, for a Jira project, epic, board, or JQL filter.
---

# Jira Monte Carlo forecast

Forecast from **historical throughput** (completed items per day), not estimates. History is
kept in a local DuckDB, so repeat runs only fetch what changed, the backlog is snapshotted on
every sync, and every forecast is recorded for later calibration.

The CLI (needs `uv`; `--help` on any subcommand lists flags):

```bash
M="${CLAUDE_PLUGIN_ROOT}/skills/forecast/scripts/mcmc.py"
```

## 1. Pin down the question

Get (ask only for what is missing):

- **Scope** – a project key, epic, board, or raw JQL. Give it a short stable **scope name**
  (e.g. `PAY`, `PAY-123-epic`) — the same name must be reused on later runs.
- **Question** – *when* will the remaining items be done, *how many* by a date, stats, or
  calibration ("how good were our forecasts?").
- **Issue types** – default: everything except sub-tasks. Use `--type` to forecast e.g. only Stories.
- **History window** – default 90 days. Shorter (30–60d) if the team changed recently.

## 2. Sync from Jira

```bash
uv run "$M" sync-info <scope>
```

returns `mode` (`full` or `incremental`), `updated_since`, and the stored `jql`. Then query
with whichever Jira MCP is connected (Atlassian MCP: `getAccessibleAtlassianResources` →
`cloudId`, then `searchJiraIssuesUsingJql`):

- **full** (first run, or weekly): `<scope JQL> AND issuetype not in subTaskIssueTypes() AND (statusCategory != Done OR resolved >= -<window>d)`
- **incremental**: `<scope JQL> AND issuetype not in subTaskIssueTypes() AND updated >= "<updated_since>"`

Request only the fields `issuetype,created,resolutiondate,status` plus the **epic link**:
`parent` on Jira Cloud, or the "Epic Link" custom field on Server/Data Center (find its id,
e.g. `customfield_10008`, via the MCP's field search). Page through **all**
results (`nextPageToken` / `startAt` / `start_at`) — a truncated pull silently understates
throughput or backlog. Write what you fetched to a scratch file as CSV — one row per issue,
dates as `YYYY-MM-DD`, `resolved` empty when unresolved, `status_category` as Jira reports
it (`Done`, `In Progress`, `To Do`):

```csv
key,type,created,resolved,status_category,epic
PAY-105,Story,2026-06-15,2026-09-14,Done,PAY-98
PAY-10,Story,2026-08-14,,In Progress,
```

`epic` is the parent epic's key (empty if none).

(Raw Jira JSON issues also work; pass `--epic-field customfield_NNNNN` for a DC Epic Link.)
Sub-tasks are dropped automatically. Epic links only arrive with a **full** sync for issues
that haven't changed, so if `epics` reports none, do a full sync. Then:

```bash
uv run "$M" ingest <scope> issues.csv --jql '<scope JQL>' [--full]
```

Pass `--full` only for a full pull: open items missing from it are treated as having left the scope.

If `ingest` reports `missing_epics` (epics whose children are synced but the epic itself
wasn't, typically because it was resolved before the window), fetch them with
`key in (<keys>)`, same fields, and `ingest` them too (without `--full`).

## 3. Forecast

```bash
uv run "$M" forecast <scope> [--type Story]...        # when will the open backlog be done?
uv run "$M" forecast <scope> --items 42               # when will N items be done?
uv run "$M" forecast <scope> --by 2026-12-18          # how many by a date?
uv run "$M" epics <scope> [--epic PAY-123]...         # when will each open epic be done?
uv run "$M" epics <scope> --order PAY-98,PAY-123 [--wip 2]   # ...if worked in this priority order
uv run "$M" stats <scope>                             # weekly throughput/arrivals, lead time by type, snapshots
uv run "$M" aging <scope> [--all]                     # open items older than their type's p85/p95 lead time
uv run "$M" calibrate [<scope>]                       # how past forecasts held up
```

`forecast` defaults the remaining count to open items in the DB, and for *when* questions
runs two models: **no_growth** (fixed backlog) and **scope_growth** (each simulated day also
adds that historical day's created items). Epic issues are containers and never count as
items. `epics` gives two answers per epic: **current pace** (resampling that epic's own
completions — realistic, and recorded for calibration) and **sole focus** (whole-team
throughput on only that epic — the best case). With `--order`, it adds a **priority**
forecast: epics worked in that order, `--wip` at a time, using the share of team throughput
that historically went to epic work (`--epic-share` to override, e.g. "if we spent 60% on
epics"); epics not listed are treated as paused. Take the order from the user, else from Jira
(`issuetype = Epic AND statusCategory != Done ORDER BY Rank`), and say which you used.
Add `--json` to post-process.

## 4. Report

- Lead with the **85% confidence** answer; show 50/70/85/95 in a small table, both models
  side by side for *when* questions. Explain that scope_growth is the realistic one when work
  keeps being added (projects, living epics), no_growth when the scope is frozen.
- State the inputs: scope JQL, history window, completed vs created per week, remaining count.
- Relay any `warnings` (low history, backlog not shrinking). If scope_growth doesn't finish,
  say plainly that at current rates the backlog never empties — that is the finding.
- For epics, show one row per epic: open items, completions in the window, current-pace
  p50/p85, sole-focus p85. "No progress" means nothing finished in the window, so there is no
  pace to project — say so rather than guessing. A wide gap between current pace and sole
  focus is the finding: the team's attention is spread thin; prioritising shortens it.
  Call out epics marked done that still have open children — the epic status is wrong or
  the children belong elsewhere.
- For *how many*, higher confidence means a **lower** number ("85% likely to finish at least N").
- For aging, lead with counts (stale = older than p95 lead time, at risk = older than p85),
  then the oldest in-progress items first — those are most likely stuck; old to-do items are
  usually deprioritised, not stuck. Lead time runs from creation, and only finished items
  set the baseline, so frame flags as "older than nearly everything we finish", not "late".
  Suggest closing or re-scoping stale to-do items: they inflate every backlog forecast.
- If `calibrate` has resolved forecasts for this scope, add one line on track record
  (e.g. "past p85 answers held 7/9 times").
