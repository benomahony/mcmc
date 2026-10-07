#!/usr/bin/env bash
# End-to-end: real Claude + this plugin + mcp-atlassian, against the jira820 mock.
# Costs real tokens (~$1 for a first full sync). Usage: tests/e2e/run.sh ["question"]
set -euo pipefail
cd "$(dirname "$0")/../.."
question="${1:-When will the open PAY backlog be done? Use the PAY project.}"
work="$(mktemp -d)"
export MCMC_DB="$work/mcmc.sqlite"

JIRA820_PORT=8820 JIRA820_SEED=42 JIRA820_PROJECT_KEY=PAY JIRA820_LOCALE=en \
  uv run --group dev jira820 >"$work/jira820.log" 2>&1 &
trap 'kill $! 2>/dev/null' EXIT
until curl -sf localhost:8820/rest/api/2/serverInfo >/dev/null; do sleep 0.5; done

claude -p "$question" \
  --plugin-dir . \
  --mcp-config tests/e2e/jira-mock.mcp.json --strict-mcp-config \
  --allowedTools Skill "Bash(python3:*)" Write Read "mcp__jira__*" \
  --output-format stream-json --verbose \
  --max-budget-usd "${MAX_BUDGET_USD:-4}" </dev/null >"$work/run.jsonl"
python3 tests/e2e/summarise.py "$work/run.jsonl"

echo
uv run skills/forecast/scripts/mcmc.py stats PAY
echo "DB: $MCMC_DB"
