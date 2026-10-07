---
type: llm
weight: 1
---

The user supplied the data, so no Jira connection is needed. A good response:
- forecasts from the provided CSV instead of refusing or asking for a Jira connection;
- leads with an 85% confidence finish date for the 12 open items, in late October or early November 2026;
- ideally notices the export covers only about 60 days of history and sizes the history window to match
  (a 90-day window would count a month of empty days and understate the pace);
- shows the 50/70/85/95% answers;
- says the forecast covers today's backlog and should be re-run as new work arrives, and does not present a second "new work keeps arriving" model;
- states its inputs (history window, items finished per week, open items) and doesn't invent figures that aren't derived from the data.
