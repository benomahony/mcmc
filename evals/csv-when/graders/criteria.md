---
type: llm
weight: 1
---

The user supplied the data, so no Jira connection is needed. A good response:
- gives numbers produced by the plugin's forecasting tool (the Monte Carlo simulation). If the response says
  it couldn't run the tool and estimated by hand instead, it FAILS, however reasonable the estimate;
- forecasts from the provided CSV instead of refusing or asking for a Jira connection;
- leads with an 85% confidence finish date for the 12 open items of about 20 to 26 October 2026 (the export
  covers about 60 days of history at roughly 6.4 items finished a week);
- shows the 50/70/85/95% answers;
- says the forecast covers today's backlog and should be re-run as new work arrives, and does not present a second "new work keeps arriving" model;
- states its inputs (history window, items finished per week, open items) and doesn't invent figures that aren't derived from the data.
