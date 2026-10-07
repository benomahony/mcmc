---
type: llm
weight: 1
---

A good response:
- answers from the provided CSV, without asking for a Jira connection;
- gives "at least N" counts at 50/70/85/95% confidence for the month to 2026-11-06, with higher confidence giving a lower number, and leads with the 85% figure;
- puts the number in context: there are only 12 open items today, so finishing more than 12 needs new work to arrive;
- doesn't invent figures that aren't derived from the data.
