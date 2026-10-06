"""The HTML report: one self-contained page (inline SVG, no network) answering, in order,
when the backlog will be done, whether it is shrinking, when planned epics land, what looks
stuck, and what to tidy in Jira.

`facts()` works out what the page says; each section function renders one part of it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from html import escape
from pathlib import Path

from charts import (
    RISK,
    EpicChart,
    FinishChart,
    Weeks,
    aging_chart,
    as_date,
    cdf,
    cumulative_chart,
    density_chart,
    epic_chart,
    long_date,
    pct,
    per_week,
    responsive,
    table,
    weekly_chart,
)
from report_assets import CSS, JS

PERCENT_POINTS = (50, 70, 85, 95)


@dataclass(frozen=True)
class Trend:
    """Weekly rates over the last 4 full weeks against the weeks before."""

    recent_done: float
    recent_new: float
    earlier_done: float
    earlier_new: float

    def __post_init__(self) -> None:
        rates = (self.recent_done, self.recent_new, self.earlier_done, self.earlier_new)
        assert min(rates) >= 0, f"negative weekly rate in {rates}"
        assert max(rates) < 10**6, f"implausible weekly rate in {rates}"


@dataclass(frozen=True)
class Facts:
    """Everything the page states, worked out once from the report data."""

    scope: str
    forecast: dict
    weeks: list[dict]  # full weeks only (the window's edge weeks are partial)
    shrinking: bool
    trend: Trend | None
    plan: list[dict]  # epics in the given priority order
    open_epics: list[dict]  # other open epics, soonest first
    done_but_open: list[dict]  # epics marked done that still have open items
    stuck: list[dict]  # flagged items in progress, oldest first
    flagged: list[dict]  # every flagged item, in progress first

    def __post_init__(self) -> None:
        assert all(w["days"] == 7 for w in self.weeks), "only full weeks belong in the trend"
        assert all(i["status"] == "in_progress" for i in self.stuck), "stuck items must be in progress"

    @property
    def p85(self) -> str | None:
        """The headline: the 85% finish date if nothing new is added."""
        pcts = self.forecast["percentiles"]["no_growth"]
        assert "p85" in pcts, f"forecast has no 85% answer: {sorted(pcts)}"
        assert set(pcts) >= {f"p{p}" for p in PERCENT_POINTS}, f"forecast percentiles {sorted(pcts)}"
        return pcts["p85"]


# --- working out what the page says ---------------------------------------------------------


def weekly_trend(weeks: list[dict]) -> Trend | None:
    """Last 4 full weeks against the earlier ones (needs at least 8 weeks)."""
    recent, earlier = weeks[-4:], weeks[:-4]
    if len(recent) < 4 or len(earlier) < 4:
        return None
    trend = Trend(
        sum(w["completed"] for w in recent) / 4, sum(w["created"] for w in recent) / 4,
        sum(w["completed"] for w in earlier) / len(earlier), sum(w["created"] for w in earlier) / len(earlier),
    )
    assert len(recent) + len(earlier) == len(weeks), "weeks lost between recent and earlier"
    assert trend.recent_done <= max(w["completed"] for w in recent), "an average above every week it averages"
    return trend


def epic_groups(ep: dict) -> tuple[list[dict], list[dict], list[dict]]:
    """(the plan in order, other open epics soonest first, done epics with open items)."""
    prio = ep.get("priority")
    by_key = {e["epic"]: e for e in ep["epics"]}
    plan = [by_key[k] for k in prio["order"]] if prio else []
    in_plan = {e["epic"] for e in plan}
    others = [e for e in ep["epics"] if e["open"] and e["epic"] not in in_plan]
    open_epics = sorted((e for e in others if e["status"] != "done"),
                        key=lambda e: (e["current_pace"] is None, (e["current_pace"] or {}).get("p85") or "9999"))
    done_but_open = [e for e in others if e["status"] == "done"]
    assert len(plan) == len(in_plan), f"an epic is planned twice: {[e['epic'] for e in plan]}"
    assert not in_plan & {e["epic"] for e in open_epics + done_but_open}, "a planned epic is listed twice"
    return plan, open_epics, done_but_open


def facts(d: dict) -> Facts:
    fc, ag = d["forecast"], d["aging"]
    h = fc["history"]
    weeks = [w for w in d["stats"]["weekly"] if w.get("days", 7) == 7]
    plan, open_epics, done_but_open = epic_groups(d["epics"])
    flagged = sorted((i for i in ag["items"] if i["risk"] != "ok"), key=lambda i: (i["status"] != "in_progress", -i["age_days"]))
    shrinking = h["completed_per_week"] - h["created_per_week"] > 0.1 * h["completed_per_week"]
    f = Facts(d["scope"], fc, weeks, shrinking, weekly_trend(weeks), plan, open_epics, done_but_open,
              [i for i in flagged if i["status"] == "in_progress"], flagged)
    assert fc["kind"] == "when", "the report needs a when-forecast"
    assert len(f.flagged) <= len(ag["items"]), f"{len(f.flagged)} flagged of {len(ag['items'])} items"
    return f


# --- summary --------------------------------------------------------------------------------


def backlog_bullet(f: Facts) -> str:
    h = f.forecast["history"]
    done, new = per_week(h["completed_per_week"]), per_week(h["created_per_week"])
    items, when = f.forecast["items"], long_date(f.p85)
    if f.shrinking:
        net = per_week(h["completed_per_week"] - h["created_per_week"])
        text = (f"<b>The backlog is shrinking</b>, by about {net} items a week ({done} finished, {new} created). "
                f"If nothing new were added, the {items} open items would be done by <b>{when}</b> (85% confidence).")
    else:
        text = (f"<b>The backlog isn't shrinking.</b> Over the last {h['days']} days about as many items were created as "
                f"finished ({new} vs {done} a week), so the {items} open items won't clear on their own. "
                f"Even if nothing new were added, they'd take until <b>{when}</b> (85% confidence).")
    assert when in text, "the headline date must appear"
    assert str(items) in text, "the open item count must appear"
    return text


def trend_bullet(f: Facts) -> str | None:
    t = f.trend
    if t is None or abs(t.recent_new - t.earlier_new) < 0.25 * max(t.earlier_new, 1):
        return None
    dropped = t.recent_new < t.earlier_new
    outlook = ""
    if dropped and t.recent_new < t.recent_done and not f.shrinking:
        outlook = " If that holds, the backlog will start to shrink."
    elif not dropped and t.recent_new > t.recent_done:
        outlook = " At that rate the backlog grows."
    text = (f"New work has {'dropped' if dropped else 'risen'} lately: {per_week(t.recent_new)} a week over the last "
            f"4 weeks, against {per_week(t.earlier_new)} before.{outlook}")
    assert per_week(t.recent_new) in text, "the recent rate must appear"
    assert ("dropped" in text) == dropped, "direction must match the numbers"
    return text


def plan_bullet(f: Facts, prio: dict) -> str:
    if prio.get("note"):
        return f"The epic plan can't be forecast: {escape(prio['note'])}."
    last = f.plan[-1].get("priority") or {}
    paced = [e["current_pace"]["p85"] for e in f.plan if e.get("current_pace") and e["current_pace"]["p85"]]
    no_pace = [e["epic"] for e in f.plan if not e.get("current_pace")]
    text = (f"Worked in order ({escape(' → '.join(prio['order']))}), the planned epics would all land by "
            f"<b>{long_date(last.get('p85'))}</b>")
    if paced:
        text += f"; at today's spread-out pace the last would take until {long_date(max(paced))}"
    if no_pace:
        text += f", and {escape(', '.join(no_pace))} has had no progress at all"
    assert f.plan, "a plan bullet needs planned epics"
    assert prio["order"], "a plan needs an order"
    return text + "."


def stuck_bullet(f: Facts) -> str:
    in_plan = {e["epic"] for e in f.plan}
    stuck_in_plan = [i["key"] for i in f.stuck if i["epic"] in in_plan]
    n_stale = sum(i["risk"] == "stale" for i in f.stuck)
    text = f"<b>{len(f.stuck)} items in progress look stuck</b>: older than 85% of what the team finishes"
    text += f", and {n_stale} older than 95%." if n_stale else "."
    if stuck_in_plan:
        text += f" {len(stuck_in_plan)} of them are in planned epics ({escape(', '.join(stuck_in_plan[:4]))})."
    assert f.stuck, "only stuck work gets a stuck bullet"
    assert n_stale <= len(f.stuck), f"{n_stale} stale of {len(f.stuck)} stuck"
    return text


def summary_section(f: Facts, prio: dict | None) -> str:
    fc = f.forecast
    bullets = [backlog_bullet(f), trend_bullet(f)]
    chance = (fc.get("chance") or {}).get("no_growth")
    if fc.get("target_date") and chance is not None:
        bullets.append(f"There's a <b>{pct(chance)} chance</b> of clearing today's backlog by "
                       f"{long_date(fc['target_date'])}, even with nothing new added.")
    if f.plan and prio:
        bullets.append(plan_bullet(f, prio))
    if f.stuck:
        bullets.append(stuck_bullet(f))
    if f.done_but_open:
        bullets.append(f"{len(f.done_but_open)} epics are marked Done in Jira but still have open work.")
    bullets += [escape(w[0].upper() + w[1:]) + "." for w in fc.get("warnings", []) if "created at least as fast" not in w]
    shown = [b for b in bullets if b]
    assert shown[0] == bullets[0], "the backlog finding always leads"
    assert len(shown) <= 9, f"{len(shown)} bullets is not a short version"
    return ('<section class="summary" aria-labelledby="s-summary"><h2 id="s-summary">The short version</h2><ul>'
            + "".join(f"<li>{b}</li>" for b in shown)
            + f'</ul><p class="basis">Forecasts replay randomly chosen days from the last {fc["history"]["days"]} days '
            f"of history {fc['runs']:,} times. They assume items are roughly similar in size and that the coming months "
            "look like the last few. Lead times count from when an item was created.</p></section>")


def tiles(f: Facts) -> str:
    h, fc = f.forecast["history"], f.forecast
    cells = [("Backlog", "Shrinking" if f.shrinking else "Not shrinking",
              f"{per_week(h['completed_per_week'])} finished vs {per_week(h['created_per_week'])} created a week"),
             ("If nothing new is added", long_date(f.p85), "85% confidence")]
    chance = (fc.get("chance") or {}).get("no_growth")
    if fc.get("target_date") and chance is not None:
        cells.append((f"Chance done by {long_date(fc['target_date'])}", pct(chance), "if nothing new is added"))
    cells.append(("Stuck in progress", str(len(f.stuck)), "older than 85% of finished items"))
    assert 3 <= len(cells) <= 4, f"{len(cells)} tiles"
    assert cells[1][1] == long_date(f.p85), "the date tile shows the headline date"
    return '<div class="tiles">' + "".join(
        f'<div class="tile"><div class="label">{escape(a)}</div><div class="value">{escape(b)}</div><div class="note">{escape(c)}</div></div>'
        for a, b, c in cells) + "</div>"


# --- sections -------------------------------------------------------------------------------


def when_section(f: Facts) -> str:
    fc, h = f.forecast, f.forecast["history"]
    pcts = fc["percentiles"]["no_growth"]
    start = as_date(fc["start"])
    chart = FinishChart(fc["_samples"]["days (no_growth)"], start, pcts, fc.get("target_date"))
    pct_line = "".join(f"<span>{p}% by <b>{long_date(pcts[f'p{p}'])}</b></span>" for p in PERCENT_POINTS)
    weekly = cdf(chart.samples, (as_date(pcts["p95"]) - start).days + 14) if pcts["p95"] else []
    chance_rows = [[long_date(start + timedelta(days=d)), f"{weekly[d]:.0%}"] for d in range(7, len(weekly), 7)]
    caveat = "" if f.shrinking else (
        f'<p class="callout">This assumes nothing new is added. At the current rate ({per_week(h["created_per_week"])} '
        f"created, {per_week(h['completed_per_week'])} finished a week) the backlog won't shrink, so treat these dates "
        "as a best case.</p>")
    toggle = ('<div class="toggle" role="group" aria-label="Chart view">'
              '<button type="button" data-view="pdf" aria-pressed="true">How likely each date is</button>'
              '<button type="button" data-view="cdf" aria-pressed="false">Chance done by date</button></div>')
    assert len(chance_rows) == max(0, (len(weekly) - 1) // 7), "one row per week"
    assert pcts["p85"] is None or long_date(pcts["p85"]) in pct_line, "the percentile line shows the headline date"
    return f"""<section aria-labelledby="s-when"><h2 id="s-when">When will today's backlog be done?</h2>
<p class="answer">85% likely by <b>{long_date(f.p85)}</b> for the {fc['items']} open items, if nothing new is added.</p>
{caveat}
<div class="chart-head"><span class="lede" style="margin:0">Hover the chart for the chance of being done by any date.</span>{toggle}</div>
<div data-pane="pdf">{responsive(lambda w: density_chart(w, chart))}</div>
<div data-pane="cdf" hidden>{responsive(lambda w: cumulative_chart(w, chart))}</div>
<p class="pcts">{pct_line}</p>
<details><summary>Chance of being done, week by week</summary>{table(["Date", "Chance done by then"], chance_rows, frozenset({1}))}</details>
</section>"""


def flow_section(f: Facts) -> str:
    h = f.forecast["history"]
    if len(f.weeks) < 2:
        return ""
    weeks = Weeks([w["week"] for w in f.weeks], [w["completed"] for w in f.weeks], [w["created"] for w in f.weeks])
    t = f.trend
    trend = "" if t is None else (
        f" Over the last 4 weeks: {per_week(t.recent_done)} finished and {per_week(t.recent_new)} created a week "
        f"(before that: {per_week(t.earlier_done)} and {per_week(t.earlier_new)}).")
    legend = ('<div class="legend"><span><i class="key-line" style="background:var(--done)"></i>Finished</span>'
              '<span><i class="key-line" style="background:var(--new)"></i>Created</span>'
              "<span>Lines: 4-week average · dots: each week</span></div>")
    rows = [[long_date(w["week"]), str(w["completed"]), str(w["created"])] for w in f.weeks]
    assert len(rows) == len(f.weeks), "one table row per week"
    assert all(w["days"] == 7 for w in f.weeks), "partial weeks would show a fake dip"
    answer = "Yes" if f.shrinking else "No"
    return f"""<section aria-labelledby="s-flow"><h2 id="s-flow">Is the backlog shrinking?</h2>
<p class="answer">{answer}: the team finished {per_week(h['completed_per_week'])} and took on {per_week(h['created_per_week'])} items a week over the last {h['days']} days.{trend}</p>
{legend}{responsive(lambda w: weekly_chart(w, weeks))}
<details><summary>Weekly numbers</summary>{table(["Week of", "Finished", "Created"], rows, frozenset({1, 2}))}</details>
</section>"""


def pace_cell(e: dict) -> str:
    """An epic's 85% date at current pace, or why there isn't one."""
    cp = e.get("current_pace")
    text = "no recent progress" if not cp else long_date(cp["p85"]) if cp["p85"] else "later"
    assert text, f"empty pace cell for {e['epic']}"
    assert cp or text == "no recent progress", f"{e['epic']} has no pace forecast"
    return text


def plan_section(f: Facts, ep: dict) -> str:
    prio = ep.get("priority")
    if not f.plan or not prio:
        return ""
    target = ep.get("target_date")
    rows = []
    for e in f.plan:
        row = [escape(e["epic"]), str(e["open"]), f"<b>{long_date((e.get('priority') or {}).get('p85'))}</b>", pace_cell(e)]
        rows.append(row + ([pct((e.get("chance") or {}).get("priority"))] if target else []))
    head = ["Epic", "Open items", "In the plan (85%)", "At current pace (85%)"] + ([f"Chance by {long_date(target)}"] if target else [])
    last = long_date((f.plan[-1].get("priority") or {}).get("p85"))
    answer = (f"Can't forecast the plan: {escape(prio['note'])}." if prio.get("note")
              else f"Worked one at a time in this order, the last lands by <b>{last}</b> (85% confidence).")
    legend = ('<div class="legend"><span><i class="key-dot" style="background:var(--plan)"></i>In the plan</span>'
              '<span><i class="key-dot" style="background:var(--pace)"></i>At current pace</span>'
              "<span>Each dot: 85% confidence finish date</span></div>")
    chart = EpicChart(f.plan, as_date(ep["start"]), True, f.forecast["max_days"])
    assert len(rows) == len(prio["order"]), f"{len(rows)} rows for {len(prio['order'])} planned epics"
    assert all(len(r) == len(head) for r in rows), "plan table rows don't match the header"
    return f"""<section aria-labelledby="s-plan"><h2 id="s-plan">When will the planned epics land?</h2>
<p class="answer">{answer}</p>
<p class="lede">"In the plan" assumes the team works these epics in order, {prio['wip']} at a time, spending {prio['epic_share']:.0%} of its
capacity on epic work as it has recently, with other epics paused. "At current pace" assumes effort stays spread as it is today.</p>
{legend}{responsive(lambda w: epic_chart(w, chart))}
{table(head, rows, frozenset({1, 4}) if target else frozenset({1}))}
</section>"""


def other_epics_section(f: Facts, ep: dict) -> str:
    if not f.open_epics:
        return ""
    days = f.forecast["history"]["days"]
    rows = [[escape(e["epic"]), str(e["open"]), str(e["completed_in_window"]), pace_cell(e)] for e in f.open_epics]
    title = "When will the other open epics land?" if f.plan else "When will each open epic land?"
    chart = EpicChart(f.open_epics, as_date(ep["start"]), False, f.forecast["max_days"])
    assert len(rows) == len(f.open_epics), "one row per open epic"
    assert all(e["status"] != "done" for e in f.open_epics), "done epics belong in the clean-up list"
    return f"""<section aria-labelledby="s-epics"><h2 id="s-epics">{title}</h2>
<p class="lede">At current pace: each epic's own recent completions, replayed forward.</p>
{responsive(lambda w: epic_chart(w, chart))}
{table(["Epic", "Open items", f"Finished in last {days} days", "At current pace (85%)"], rows, frozenset({1, 2}))}
</section>"""


def badge(risk: str) -> str:
    color, icon, label = RISK[risk]
    assert label, f"risk {risk!r} needs a label"
    assert risk == "ok" or icon, f"risk {risk!r} needs an icon so colour isn't the only signal"
    return f'<span class="badge"><span style="color:{color}" aria-hidden="true">{icon}</span>{label}</span>'


def stuck_row(i: dict) -> list[str]:
    row = [escape(i["key"]), escape(i["type"] or "?"), escape(i["epic"]) if i["epic"] else "—", str(i["age_days"]),
           str(i["lead_time_p85"]), badge(i["risk"])]
    assert len(row) == 6, f"stuck table rows have 6 cells, got {len(row)}"
    assert i["age_days"] >= 0, f"{i['key']} is {i['age_days']} days old"
    return row


def stuck_section(f: Facts, items: list[dict]) -> str:
    if not items:
        return ""
    head = ["Item", "Type", "Epic", "Age (days)", "Usually done within (days)", "Risk"]
    top = [stuck_row(i) for i in f.stuck[:10]]
    rest = [stuck_row(i) for i in f.flagged if i not in f.stuck[:10]]
    n_stale = sum(i["risk"] == "stale" for i in f.stuck)
    legend = ('<div class="legend">'
              '<span><i class="key-dot" style="background:var(--ink-2)"></i>In progress</span>'
              '<span><i class="key-dot" style="border:1.5px solid var(--ink-2)"></i>To do</span>'
              '<span><i class="key-zone" style="background:color-mix(in srgb, var(--warning) 35%, transparent)"></i>Older than 85% of finished items</span>'
              '<span><i class="key-zone" style="background:color-mix(in srgb, var(--critical) 25%, transparent)"></i>Older than 95%</span></div>')
    starred = any(i["basis"] != i["type"] for i in items)
    star_note = '<p class="note">* Too few recent completions of this type; compared with all types.</p>' if starred else ""
    top_table = '<p class="lede" style="margin:16px 0 0"><b>Oldest items in progress</b></p>' + table(head, top, frozenset({3, 4}), frozenset({1})) if top else ""
    rest_table = f"<details><summary>All {len(f.flagged)} flagged items, including to do</summary>{table(head, rest, frozenset({3, 4}), frozenset({1}))}</details>" if rest else ""
    assert len(top) + len(rest) == len(f.flagged), "every flagged item appears once"
    assert len(top) <= 10, f"{len(top)} rows in the top table"
    stale_note = f" ({n_stale} older than 95%)" if f.stuck else ""
    return f"""<section aria-labelledby="s-stuck"><h2 id="s-stuck">What looks stuck?</h2>
<p class="answer">{len(f.stuck)} items in progress are older than 85% of what the team finishes{stale_note}.</p>
<p class="lede">Each dot is an open item, placed by age. Items in the shaded zones are older than nearly everything the team
finishes of that type. Filled dots (in progress) there are the likeliest to be stuck; hollow ones (to do) are usually just waiting.</p>
{legend}{responsive(lambda w: aging_chart(w, items))}
{star_note}{top_table}{rest_table}
</section>"""


def cleanup_section(f: Facts, open_without_epic: int) -> str:
    stale_todo = [i for i in f.flagged if i["status"] != "in_progress" and i["risk"] == "stale"]
    items = []
    if f.done_but_open:
        listed = escape(", ".join(f"{e['epic']} ({e['open']})" for e in f.done_but_open))
        items.append(f"<b>{len(f.done_but_open)} epics are marked Done but still have open items:</b> {listed}. "
                     "Reopen them or move the items; until then this work sits outside any plan.")
    if stale_todo:
        oldest = escape(", ".join(i["key"] for i in stale_todo[:5]))
        items.append(f"<b>{len(stale_todo)} to-do items are older than 95% of what the team finishes</b> (oldest: {oldest}). "
                     "Close or re-scope them: they inflate every backlog forecast.")
    if open_without_epic:
        items.append(f"{open_without_epic} open items have no epic.")
    assert len(items) <= 3, f"{len(items)} clean-up items"
    assert open_without_epic >= 0, f"negative unparented count {open_without_epic}"
    if not items:
        return ""
    return ('<section aria-labelledby="s-clean"><h2 id="s-clean">What needs tidying in Jira?</h2><ul class="plain">'
            + "".join(f"<li>{c}</li>" for c in items) + "</ul></section>")


MODEL_LABELS = {"when/no_growth": "Backlog finish date", "when/scope_growth": "Backlog finish date (with new work)",
                "how_many": "Items by a date", "epic/current_pace": "Epic finish date"}


def track_section(cal: dict) -> tuple[str, str]:
    """The calibration table (or nothing yet), and the footer note about it."""
    rows = [
        [escape(MODEL_LABELS.get(model, str(model))), p[1:] + "%", f"{s['held'] / (s['held'] + s['missed']):.0%}",
         f"{s['held']} of {s['held'] + s['missed']}"]
        for model, by_p in cal["summary"].items() for p, s in by_p.items() if s["held"] + s["missed"]
    ]
    assert all(r[3].split(" of ")[0].isdigit() for r in rows), "held counts must be whole numbers"
    assert len(rows) <= 4 * len(cal["summary"]), f"{len(rows)} rows for {len(cal['summary'])} models"
    if not rows:
        return "", "No past forecast for this scope has reached its date yet, so there is no track record. "
    return ('<section aria-labelledby="s-cal"><h2 id="s-cal">How accurate have past forecasts been?</h2>'
            '<p class="lede">A well-calibrated 85% answer should hold about 85% of the time.</p>'
            + table(["Forecast", "Confidence", "Held", "Forecasts"], rows, frozenset({2, 3})) + "</section>"), ""


# --- page -----------------------------------------------------------------------------------


def provenance(d: dict) -> str:
    """The line under the title: which JQL, which history, when synced."""
    h = d["forecast"]["history"]
    synced = ""
    if d.get("last_sync"):
        ls = datetime.fromisoformat(d["last_sync"])
        synced = f" · synced {long_date(ls.date())} {ls:%H:%M}"
    jql = f"<code>{escape(d['jql'])}</code> · " if d.get("jql") else ""
    line = f"{jql}excluding epics and sub-tasks · history {long_date(h['start'])} – {long_date(h['end'])}{synced}"
    assert long_date(h["end"]) in line, "the provenance line names the data's end date"
    assert "<script" not in line.lower(), "JQL must be escaped"
    return line


def page(title: str, meta: str, body: str) -> str:
    """A complete, self-contained HTML document."""
    assert title, "pages need a title"
    assert "</html>" not in body, "body must not close the document"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title><style>{CSS}
.mc a {{ color: var(--ink); }}</style></head>
<body><div class="mc"><main>
<h1>{escape(title)}</h1>
<p class="meta">{meta}</p>
{body}
</main><div id="tip" role="tooltip"></div></div><script>{JS}</script></body></html>
"""


def render(d: dict) -> str:
    """The full report for one scope."""
    f = facts(d)
    ep = d["epics"]
    track, footnote = track_section(d["calibrate"])
    sections = [
        tiles(f), summary_section(f, ep.get("priority")), when_section(f), flow_section(f), plan_section(f, ep),
        other_epics_section(f, ep), stuck_section(f, d["aging"]["items"]), cleanup_section(f, ep.get("open_without_epic", 0)),
        track, f"<footer>{footnote}Generated by mcmc from Jira data. Nothing on this page is sent anywhere.</footer>",
    ]
    html = page(f"{d['scope']} delivery forecast", provenance(d), "\n".join(s for s in sections if s))
    assert html.count("<section") == html.count("</section>"), "unbalanced sections"
    assert "The short version" in html, "the summary always leads"
    return html


def render_index(rows: list[dict], root: Path) -> str:
    """Index of saved reports, newest first, grouped by scope, linking by relative path."""
    by_scope: dict[str, list[dict]] = {}
    for row in rows:
        by_scope.setdefault(row["scope"], []).append(row)
    sections = []
    for scope, items in sorted(by_scope.items()):
        body = []
        for i, row in enumerate(items):
            href = escape(os.path.relpath(row["path"], root), quote=True)
            stamp = escape(long_date(row["created_at"].date())) + f" {row['created_at']:%H:%M}"
            chance = f"{row['chance']:.0%} by {long_date(row['target_date'])}" if row["chance"] is not None else "—"
            body.append([f'<a href="{href}">{stamp}</a>' + (" <b>latest</b>" if i == 0 else ""), long_date(row["as_of"]),
                         str(row["open_items"]), f"<b>{long_date(row['p85'])}</b>",
                         "shrinking" if row["shrinking"] else "not shrinking", chance])
        sections.append(f"<section><h2>{escape(scope)}</h2>"
                        + table(["Report", "Data as of", "Open items", "85% done by", "Backlog", "Chance by target"],
                                body, frozenset({2}), frozenset({4, 5})) + "</section>")
    assert sum(len(v) for v in by_scope.values()) == len(rows), "every report appears once"
    assert len(sections) == len(by_scope), "one section per scope"
    meta = 'Saved locally, newest first. Watching the "85% done by" date across reports shows whether delivery is slipping or holding.'
    return page("Delivery forecast reports", meta, "".join(sections) or '<p class="lede">No reports yet.</p>')

