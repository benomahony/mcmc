---
type: llm
weight: 1
---

A good response:
- gives numbers produced by the plugin's forecasting tool (the Monte Carlo simulation). If the response says
  it couldn't run the tool and estimated by hand instead, it FAILS, however reasonable the estimate;
- answers from the provided CSV, without asking for a Jira connection;
- gives "at least N" counts at 50/70/85/95% confidence for the month to 2026-11-06, with higher confidence giving a lower number, and leads with the 85% figure (about 24 at roughly 6.4 items a week);
- puts the number in context: there are only 12 open items today, so finishing more than 12 needs new work to arrive;
- doesn't invent figures that aren't derived from the data.
