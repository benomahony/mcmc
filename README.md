# mcmc

Claude Code plugin for Monte Carlo delivery forecasts from Jira throughput.

Ask things like *"when will the PAY-123 epic be done?"* or *"how many PAY tickets can we close by Christmas?"*.
Claude syncs resolved-issue history and the open backlog via the Atlassian MCP into a local
DuckDB, then resamples daily throughput 10,000× to give 50/70/85/95% answers, with and
without scope growth. Repeat runs sync incrementally, and every forecast is recorded so
`calibrate` can show how well past p85 answers held up.

## Install

```bash
claude plugin marketplace add benomahony/mcmc
claude plugin install mcmc@mcmc
```

The plugin bundles the Atlassian remote MCP (`https://mcp.atlassian.com/v1/mcp`); run `/mcp` to authenticate.
Any other Jira MCP works too.

## Use

`/mcmc:forecast <scope and question>` — or just ask; the skill triggers on forecasting questions.

The CLI needs [`uv`](https://docs.astral.sh/uv/) (it pulls in DuckDB itself):

```bash
M=skills/forecast/scripts/mcmc.py
uv run $M sync-info PAY                       # full or incremental, and since when
uv run $M ingest PAY issues.json --full --jql 'project = PAY'
uv run $M forecast PAY                        # when will the open backlog be done?
uv run $M forecast PAY --by 2026-12-18        # how many by a date?
uv run $M stats PAY                           # throughput, arrivals, lead time by type
uv run $M calibrate                           # how past forecasts held up
uv run $M epics PAY --order PAY-98,PAY-123    # per-epic: current pace, priority order, sole focus
uv run $M aging PAY                           # open items older than their type usually takes
uv run $M report PAY                          # all of it as one self-contained HTML page
```

The DB lives at `$MCMC_DB`, else `$CLAUDE_PLUGIN_DATA/mcmc.duckdb`, else
`~/.local/share/mcmc/mcmc.duckdb`.

The simulator on its own is stdlib-only and takes plain completion dates:

```bash
python3 skills/forecast/scripts/forecast.py dates.txt --items 40 --history-start 2026-07-01
```

## Develop

```bash
uv run pytest        # unit tests + an integration test against the jira820 Jira mock
tests/e2e/run.sh     # real Claude + plugin + mcp-atlassian against jira820 (costs tokens)
```
