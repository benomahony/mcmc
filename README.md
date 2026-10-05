# mcmc

Claude Code plugin for Monte Carlo delivery forecasts from Jira throughput.

Ask things like *"when will the PAY-123 epic be done?"* or *"how many PAY tickets can we close by Christmas?"*.
Claude pulls resolved-issue history and the remaining backlog via the Atlassian MCP, then
resamples daily throughput 10,000× to give 50/70/85/95% answers.

## Install

```bash
claude plugin marketplace add benomahony/mcmc
claude plugin install mcmc@mcmc
```

The plugin bundles the Atlassian remote MCP (`https://mcp.atlassian.com/v1/mcp`); run `/mcp` to authenticate.
Any other Jira MCP works too.

## Use

`/mcmc:forecast <scope and question>` — or just ask; the skill triggers on forecasting questions.

The simulator is stdlib-only and usable standalone:

```bash
python3 skills/forecast/scripts/forecast.py dates.txt --items 40 --history-start 2026-07-01
python3 skills/forecast/scripts/forecast.py dates.txt --by 2026-12-18 --json
```
