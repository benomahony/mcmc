---
name: forecast
description: Monte Carlo delivery forecast from Jira history. Use when the user asks "when will this epic/project/backlog be done?", "how many items can we finish by <date>?", or wants a probabilistic/throughput-based forecast for a Jira project, epic, board, or JQL filter.
---

# Jira Monte Carlo forecast

Forecast from **historical throughput** (completed items per day), not estimates. The
bundled script resamples past daily throughput 10,000 times to give percentile answers.

## 1. Pin down the question

Get (ask only for what is missing):

- **Scope** – a project key, epic, board, or raw JQL.
- **Question** – either *when* will the remaining items be done, or *how many* by a date.
- **History window** – default the last 90 days. Shorter (30–60d) if the team changed recently.

## 2. Pull data with the Jira MCP

Use whichever Jira MCP is connected. With the bundled Atlassian MCP:

1. `getAccessibleAtlassianResources` → `cloudId`.
2. `searchJiraIssuesUsingJql` for **completed** items, requesting only the `resolutiondate` field:
   ```
   project = KEY AND statusCategory = Done AND resolved >= -90d ORDER BY resolved ASC
   ```
   Page through **all** results (`maxResults` / `nextPageToken` or `startAt`) — a truncated
   history silently understates throughput.
3. For a *when* question, count **remaining** items in scope:
   ```
   <scope JQL> AND statusCategory != Done
   ```

Keep issue types consistent between history and remaining (e.g. exclude sub-tasks from both:
`AND issuetype not in subTaskIssueTypes()`). If an issue lacks `resolutiondate`, skip it.

## 3. Run the simulation

Write the resolution dates, one per line, to a scratch file and run:

```bash
# When will 42 remaining items be done?
python3 "${CLAUDE_PLUGIN_ROOT}/skills/forecast/scripts/forecast.py" dates.txt \
  --items 42 --history-start <90 days ago>

# How many items by a date?
python3 "${CLAUDE_PLUGIN_ROOT}/skills/forecast/scripts/forecast.py" dates.txt \
  --by 2026-12-18 --history-start <90 days ago>
```

Always pass `--history-start` equal to the JQL window start so leading zero-throughput days
are counted. Add `--json` if you need to post-process. `--help` lists all flags.

## 4. Report

- Lead with the **85% confidence** answer; show 50/70/85/95 in a small table.
- State the inputs: scope JQL, history window, items completed, avg/week, remaining count.
- Caveats worth one line each when relevant: scope growth (remaining count will rise —
  suggest re-running weekly or adding a split-rate buffer), fewer than ~20 completions in
  history (low confidence), and that items are assumed roughly similar in size.
- For *how many*, higher confidence means a **lower** number ("85% likely to finish at least N").
