"""Inline-SVG charts for the HTML report.

Every chart is drawn at a given viewBox width (desktop or phone) so its text stays legible,
and is built from a `Plot`: the drawing area plus the x mapping from days to pixels.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from html import escape

WIDE, NARROW = 720, 380  # SVG viewBox widths for the two renders
RISK = {
    "stale": ("var(--critical)", "▲", "Stale"),
    "at risk": ("var(--warning)", "◆", "At risk"),
    "ok": ("var(--muted)", "", "OK"),
}


# --- formatting -----------------------------------------------------------------------------


def as_date(v: str | date) -> date:
    """A date from a date or an ISO string."""
    d = date.fromisoformat(v) if isinstance(v, str) else v
    assert isinstance(d, date), f"expected a date, got {v!r}"
    assert not isinstance(v, str) or d.isoformat() == v[:10], f"{v!r} read as {d}"
    return d


def long_date(v: str | date | None) -> str:
    """'19 Dec 2026', or an em dash for no date."""
    if v is None:
        return "—"
    d = as_date(v)
    text = f"{d.day} {d:%b %Y}"
    assert text.endswith(str(d.year)), f"{text!r} lost the year"
    assert 10 <= len(text) <= 11, f"unexpected date format {text!r}"
    return text


def short_date(v: str | date) -> str:
    """'19 Dec' for axis labels."""
    d = as_date(v)
    text = f"{d.day} {d:%b}"
    assert text.startswith(str(d.day)), f"{text!r} lost the day"
    assert len(text) <= 6, f"axis label {text!r} too long"
    return text


def pct(v: float | None) -> str:
    """'7%', or an em dash for no value."""
    if v is None:
        return "—"
    assert 0 <= v <= 1, f"a share must be between 0 and 1, got {v}"
    text = f"{v:.0%}"
    assert text.endswith("%"), f"{text!r} isn't a percentage"
    return text


def per_week(v: float) -> str:
    """A weekly rate: whole numbers from 10 up, one decimal below."""
    assert v >= 0, f"rates can't be negative, got {v}"
    text = f"{v:.0f}" if v >= 10 else f"{v:.1f}".rstrip("0").rstrip(".")
    assert text and float(text) == round(v, 0 if v >= 10 else 1), f"{v} formatted as {text!r}"
    return text


def tip(title: str, *rows: tuple[str, str, str | None]) -> str:
    """Attributes giving an element a hover/focus tooltip: a title, then (value, label, colour) rows."""
    assert title, "tooltips need a title"
    assert all(len(r) == 3 for r in rows), f"tooltip rows are (value, label, colour), got {rows}"
    return f' tabindex="0" data-tip="{escape(json.dumps([title, *rows]), quote=True)}"'


def nice_max(v: float) -> tuple[float, float]:
    """A round axis maximum covering v, and its tick step (four steps)."""
    if v <= 0:
        return 1, 1
    exp = 10 ** math.floor(math.log10(v))
    top, step = v, v / 4
    for m in (1, 2, 2.5, 5, 10):
        if m * exp >= v:
            top, step = m * exp, m * exp / 4
            break
    assert top >= v, f"axis max {top} below the data max {v}"
    assert math.isclose(top, step * 4), f"step {step} doesn't divide {top} into four"
    return top, step


def table(head: list[str], rows: list[list[str]], num: frozenset[int] = frozenset(), narrow_hide: frozenset[int] = frozenset()) -> str:
    """An HTML table; `num` columns right-aligned, `narrow_hide` columns hidden on phones. Cells are trusted HTML."""
    widths = {len(r) for r in rows}
    assert widths <= {len(head)}, f"rows have {sorted(widths)} cells for {len(head)} columns"
    assert max(num | narrow_hide, default=0) < len(head), f"column index out of range for {len(head)} columns"
    classes = [" ".join(c for c in ("num" if i in num else "", "hide-narrow" if i in narrow_hide else "") if c) for i in range(len(head))]
    th = "".join(f'<th class="{classes[i]}">{escape(h)}</th>' for i, h in enumerate(head))
    body = "".join("<tr>" + "".join(f'<td class="{classes[i]}">{c}</td>' for i, c in enumerate(r)) + "</tr>" for r in rows)
    return f'<div class="scroll"><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'


def responsive(draw: Callable[[int], str]) -> str:
    """A chart drawn at desktop and phone widths; CSS shows the one that fits."""
    wide, narrow = draw(WIDE), draw(NARROW)
    assert wide.startswith("<svg") and narrow.startswith("<svg"), "charts must be SVG"
    assert f'viewBox="0 0 {WIDE} ' in wide and f'viewBox="0 0 {NARROW} ' in narrow, "chart drawn at the wrong width"
    return f'<div class="wide">{wide}</div><div class="narrow">{narrow}</div>'


# --- statistics ---------------------------------------------------------------------------


def cdf(samples: Sequence[float], days: int) -> list[float]:
    """P(done within d days) for d = 0..days; runs that never finish count as not done."""
    assert samples, "no simulated runs"
    assert days >= 0, f"can't chart {days} days"
    counts = [0] * (days + 1)
    for v in samples:
        if v != math.inf and v <= days:
            counts[int(v)] += 1
    out, acc = [], 0
    for c in counts:
        acc += c
        out.append(acc / len(samples))
    return out


def kde(samples: Sequence[float], days: int) -> list[float]:
    """Chance of finishing on each day 0..days: a Gaussian kernel density of the finished runs
    (Silverman bandwidth, at least 1 day), scaled by the share of runs that finish at all."""
    finite = [v for v in samples if v != math.inf]
    if len(finite) < 2:
        return [0.0] * (days + 1)
    n = len(finite)
    mean = sum(finite) / n
    bw = max(1.0, 1.06 * math.sqrt(sum((v - mean) ** 2 for v in finite) / n) * n**-0.2)
    counts: dict[float, int] = {}
    for v in finite:
        counts[v] = counts.get(v, 0) + 1
    norm = len(samples) * bw * math.sqrt(2 * math.pi)
    dens = [
        sum(c * math.exp(-0.5 * ((g - v) / bw) ** 2) for v, c in counts.items() if abs(g - v) <= 4 * bw) / norm
        for g in range(days + 1)
    ]
    assert len(dens) == days + 1, f"{len(dens)} densities for {days + 1} days"
    assert min(dens) >= 0, f"negative density {min(dens)}"
    return dens


def rolling(values: Sequence[float], k: int = 4) -> list[float | None]:
    """k-point trailing averages; None until k values are available."""
    assert k >= 1, f"window of {k}"
    avg = [None if i + 1 < k else sum(values[i + 1 - k : i + 1]) / k for i in range(len(values))]
    assert len(avg) == len(values), f"{len(avg)} averages for {len(values)} values"
    return avg


# --- plotting -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Plot:
    """A chart's drawing area, in viewBox units, and its day range on the x axis."""

    width: int
    left: float
    top: float
    plot_height: float
    lo: int  # first day shown
    hi: int  # last day shown

    def __post_init__(self) -> None:
        assert 0 <= self.left < self.width, f"left margin {self.left} outside width {self.width}"
        assert self.lo < self.hi, f"empty day range {self.lo}..{self.hi}"

    @property
    def inner_width(self) -> float:
        right = 12 if self.width >= WIDE else 8
        inner = self.width - self.left - right
        assert inner > 0, f"no room to plot: width {self.width}, left {self.left}"
        assert inner <= self.width, f"plot wider than the chart: {inner}"
        return inner

    @property
    def base(self) -> float:
        """The y of the x axis."""
        base = self.top + self.plot_height
        assert base > self.top, f"plot height {self.plot_height}"
        assert self.plot_height > 0, f"plot height {self.plot_height}"
        return base

    def x(self, day: float) -> float:
        """x position of a day (clamped to the visible range)."""
        clamped = min(max(day, self.lo), self.hi)
        px = self.left + self.inner_width * (clamped - self.lo) / (self.hi - self.lo)
        assert self.left <= px <= self.left + self.inner_width, f"day {day} plotted outside at {px}"
        return px


@dataclass(frozen=True)
class FinishChart:
    """The simulated finish days of a backlog forecast, and what to mark on them."""

    samples: Sequence[float]
    start: date
    percentiles: dict[str, str | None]
    target: str | None = None

    def __post_init__(self) -> None:
        assert self.samples, "no simulated runs to chart"
        assert any(v != math.inf for v in self.samples), "no simulated run finished"

    def frame(self) -> tuple[int, int]:
        """The day range framing the distribution (and the target, if any)."""
        finite = sorted(v for v in self.samples if v != math.inf)
        n = len(finite)
        lo_q, hi_q = finite[int(n * 0.005)], finite[min(n - 1, int(n * 0.995))]
        pad = max(4, int((hi_q - lo_q) * 0.12))
        lo, hi = max(0, int(lo_q) - pad), int(hi_q) + pad
        if self.target:
            td = (as_date(self.target) - self.start).days
            lo, hi = min(lo, max(0, td - 4)), max(hi, td + 4)
        assert lo < hi, f"empty frame {lo}..{hi}"
        assert lo <= finite[n // 2] <= hi, f"frame {lo}..{hi} misses the median finish day {finite[n // 2]}"
        return lo, hi


def svg_open(width: int, height: float, label: str, extra: str = "") -> str:
    """The opening <svg> tag with an accessible label."""
    assert width in (WIDE, NARROW), f"charts are drawn at {WIDE} or {NARROW}, got {width}"
    assert label, "every chart needs an accessible label"
    return f'<svg viewBox="0 0 {width} {height:g}" role="img" aria-label="{escape(label)}"{extra}>'


def crosshair(plot: Plot, start: date, series: list) -> str:
    """Attributes for a chart whose hover crosshair snaps to days and reads off each series."""
    n = plot.hi - plot.lo + 1
    assert all(len(values) == n for _, _, values in series), f"every series needs {n} values"
    payload = {"l": plot.left, "pw": plot.inner_width, "start": start.isoformat(), "offset": plot.lo, "n": n, "series": series}
    attrs = f' tabindex="0" data-xhair="{escape(json.dumps(payload), quote=True)}"'
    assert "<" not in attrs, "attribute payload must be escaped"
    return attrs


def date_ticks(plot: Plot, start: date) -> list[str]:
    """Date labels along the x axis: Mondays for short ranges, month starts for long ones."""
    span = plot.hi - plot.lo
    step = next((s for s in (7, 14, 28, 56, 91, 182, 365) if span / s <= (7 if plot.width >= WIDE else 4)), 730)
    y = plot.base + 20
    labels: list[str] = []
    if step >= 28:  # month starts read better than arbitrary days
        first = start + timedelta(days=plot.lo)
        m = first if first.day == 1 else date(first.year + (first.month == 12), first.month % 12 + 1, 1)
        months = max(1, round(step / 30.4))
        while (m - start).days <= plot.hi:
            year = f" {m.year}" if m.month == 1 else ""
            labels.append(f'<text x="{plot.x((m - start).days):.1f}" y="{y}" text-anchor="middle">{m:%b}{year}</text>')
            mm = m.month - 1 + months
            m = date(m.year + mm // 12, mm % 12 + 1, 1)
    else:
        d = plot.lo + (-(start + timedelta(days=plot.lo)).weekday()) % 7
        while d <= plot.hi:
            labels.append(f'<text x="{plot.x(d):.1f}" y="{y}" text-anchor="middle">{short_date(start + timedelta(days=d))}</text>')
            d += step
    assert len(labels) <= 13, f"{len(labels)} tick labels would collide"
    assert step > 0, f"tick step {step}"
    return labels


def hline(plot: Plot, y: float, color: str = "var(--grid)") -> str:
    assert plot.top - 1 <= y <= plot.base + 1, f"gridline at {y} outside {plot.top}..{plot.base}"
    assert color.startswith("var(--"), f"use a theme token, got {color}"
    return f'<line x1="{plot.left}" x2="{plot.left + plot.inner_width:.1f}" y1="{y:.1f}" y2="{y:.1f}" stroke="{color}" stroke-width="1"/>'


def markers(plot: Plot, chart: FinishChart, curve_y: Callable[[int], float]) -> list[str]:
    """Target (strong line, label row 1) and percentile marks (hairlines, label row 2), dropping colliding labels."""
    out: list[str] = []
    if chart.target:
        tx = plot.x((as_date(chart.target) - chart.start).days)
        out.append(f'<line x1="{tx:.1f}" x2="{tx:.1f}" y1="{plot.top - 22}" y2="{plot.base}" stroke="var(--ink)" stroke-width="1.5"/>')
        out.append(f'<text class="strong" x="{tx + 5:.1f}" y="{plot.top - 12}">Target {short_date(chart.target)}</text>')
    placed: list[float] = []
    for p in ("p85", "p50", "p95"):  # 85% is the headline: it always gets its label
        when = chart.percentiles.get(p)
        if not when:
            continue
        day = (as_date(when) - chart.start).days
        px, y1 = plot.x(day), curve_y(day)
        out.append(f'<line x1="{px:.1f}" x2="{px:.1f}" y1="{y1:.1f}" y2="{plot.base}" stroke="var(--ink-2)" stroke-width="1"/>')
        if all(abs(px - q) >= 34 for q in placed):
            out.append(f'<text class="ink" x="{px:.1f}" y="{plot.top + 2}" text-anchor="middle">{p[1:]}%</text>')
            out.append(f'<line x1="{px:.1f}" x2="{px:.1f}" y1="{plot.top + 6}" y2="{y1:.1f}" stroke="var(--axis)" stroke-width="1"/>')
            placed.append(px)
    assert len(placed) <= 3, f"{len(placed)} percentile labels"
    assert not chart.percentiles.get("p85") or placed, "the 85% mark must always be labelled"
    return out


# --- charts -------------------------------------------------------------------------------


def finish_plot(width: int, chart: FinishChart, left: float) -> Plot:
    lo, hi = chart.frame()
    plot = Plot(width, left, 40, 162 if width >= WIDE else 142, lo, hi)
    assert plot.lo == lo and plot.hi == hi, "plot range drifted from the frame"
    assert plot.top >= 34, "room above the plot for the target and percentile labels"
    return plot


def density_chart(width: int, chart: FinishChart) -> str:
    """How likely each finish date is: a smoothed density without a y axis (the shape is the message)."""
    plot = finish_plot(width, chart, 12)
    dens = kde(chart.samples, plot.hi)[plot.lo :]
    cum = cdf(chart.samples, plot.hi)[plot.lo :]
    top = max(dens) * 1.05 or 1

    def y(v: float) -> float:
        return plot.base - plot.plot_height * v / top

    p = chart.percentiles
    label = f"Finish date distribution: 50% by {long_date(p.get('p50'))}, 85% by {long_date(p.get('p85'))}, 95% by {long_date(p.get('p95'))}"
    pts = " ".join(f"{plot.x(plot.lo + i):.1f},{y(v):.1f}" for i, v in enumerate(dens))
    readout = [["chance done by then", "var(--done)", [round(v, 4) for v in cum]]]
    out = [
        svg_open(width, plot.base + 28, label, crosshair(plot, chart.start, readout)),
        f'<polygon points="{plot.x(plot.lo):.1f},{plot.base} {pts} {plot.x(plot.hi):.1f},{plot.base}" fill="var(--done)" fill-opacity="0.1"/>',
        f'<polyline points="{pts}" fill="none" stroke="var(--done)" stroke-width="2" stroke-linejoin="round"/>',
        hline(plot, plot.base, "var(--axis)"),
        *markers(plot, chart, lambda d: y(dens[min(max(d - plot.lo, 0), len(dens) - 1)])),
        *date_ticks(plot, chart.start),
        f'<line class="xline" x1="0" x2="0" y1="{plot.top}" y2="{plot.base}" stroke="var(--axis)" stroke-width="1" style="opacity:0"/>',
        "</svg>",
    ]
    assert len(dens) == plot.hi - plot.lo + 1, "one density per visible day"
    assert out[-1] == "</svg>", "chart not closed"
    return "".join(out)


def cumulative_chart(width: int, chart: FinishChart) -> str:
    """Chance of being done by each date."""
    plot = finish_plot(width, chart, 44)
    cum = cdf(chart.samples, plot.hi)[plot.lo :]

    def y(v: float) -> float:
        return plot.base - plot.plot_height * v

    ticks = (0, 0.25, 0.5, 0.75, 1) if width >= WIDE else (0, 0.5, 1)
    grid = [hline(plot, y(v)) + f'<text x="{plot.left - 8}" y="{y(v) + 4:.1f}" text-anchor="end">{v:.0%}</text>' for v in ticks]
    pts = " ".join(f"{plot.x(plot.lo + i):.1f},{y(v):.1f}" for i, v in enumerate(cum))
    readout = [["chance done by then", "var(--done)", [round(v, 4) for v in cum]]]
    out = [
        svg_open(width, plot.base + 28, "Chance of being done by each date", crosshair(plot, chart.start, readout)),
        *grid,
        hline(plot, plot.base, "var(--axis)"),
        f'<polyline points="{pts}" fill="none" stroke="var(--done)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>',
        *markers(plot, chart, lambda d: y(cum[min(max(d - plot.lo, 0), len(cum) - 1)])),
        *date_ticks(plot, chart.start),
        f'<line class="xline" x1="0" x2="0" y1="{plot.top}" y2="{plot.base}" stroke="var(--axis)" stroke-width="1" style="opacity:0"/>',
        "</svg>",
    ]
    assert cum == sorted(cum), "cumulative chances must never fall"
    assert out[-1] == "</svg>", "chart not closed"
    return "".join(out)


@dataclass(frozen=True)
class Weeks:
    """Full weeks of finished vs created items, with 4-week averages."""

    starts: list[str]
    done: list[int]
    new: list[int]

    def __post_init__(self) -> None:
        assert len(self.starts) == len(self.done) == len(self.new) >= 2, f"{len(self.starts)} weeks; a trend needs two"
        assert min(self.done + self.new) >= 0, "negative weekly count"


def week_columns(plot: Plot, weeks: Weeks, x: Callable[[int], float]) -> list[str]:
    """Per-week hover columns: the whole week's band is the hit target."""
    avg_done, avg_new = rolling(weeks.done), rolling(weeks.new)
    last = len(weeks.starts) - 1
    band = (x(last) - x(0)) / last
    out = []
    for i, start in enumerate(weeks.starts):
        x0, x1 = max(plot.left, x(i) - band / 2), min(x(last) + band / 2, x(i) + band / 2)
        rows = [(str(weeks.done[i]), "finished", "var(--done)"), (str(weeks.new[i]), "created", "var(--new)")]
        a_done, a_new = avg_done[i], avg_new[i]
        if a_done is not None and a_new is not None:
            rows += [(per_week(a_done), "finished, 4-week average", "var(--done)"), (per_week(a_new), "created, 4-week average", "var(--new)")]
        title = "Week of " + long_date(start)
        out.append(
            f'<g class="col"{tip(title, *rows)}><rect x="{x0:.1f}" y="{plot.top}" width="{max(x1 - x0, 1):.1f}" height="{plot.plot_height}" fill="transparent"/>'
            f'<line class="hair" x1="{x(i):.1f}" x2="{x(i):.1f}" y1="{plot.top}" y2="{plot.base}" stroke="var(--axis)" stroke-width="1"/></g>'
        )
    assert len(out) == len(weeks.starts), "one hover column per week"
    assert band > 0, f"week band {band}"
    return out


def weekly_chart(width: int, weeks: Weeks) -> str:
    """Finished vs created per full week: 4-week averages as lines, each week as a faint dot."""
    n = len(weeks.starts)
    plot = Plot(width, 36, 12, 200 if width >= WIDE else 180, 0, n - 1)
    end_labels = width >= WIDE
    usable = plot.inner_width - (92 if end_labels else 0)
    top, step = nice_max(max(weeks.done + weeks.new))

    def x(i: int) -> float:
        return plot.left + usable * i / (n - 1)

    def y(v: float) -> float:
        return plot.base - plot.plot_height * v / top

    out = [svg_open(width, plot.base + 28, "Items finished and created per week, with 4-week averages")]
    out += [hline(plot, y(i * step)) + f'<text x="{plot.left - 8}" y="{y(i * step) + 4:.1f}" text-anchor="end">{i * step:g}</text>' for i in range(5)]
    every = max(1, math.ceil(n / (6 if end_labels else 3)))
    out += [f'<text x="{x(i):.1f}" y="{plot.base + 20}" text-anchor="middle">{short_date(s)}</text>'
            for i, s in enumerate(weeks.starts) if (n - 1 - i) % every == 0]
    ends = []
    for raw, color, name in ((weeks.done, "var(--done)", "Finished"), (weeks.new, "var(--new)", "Created")):
        avg = rolling(raw)
        out += [f'<circle cx="{x(i):.1f}" cy="{y(v):.1f}" r="3.5" fill="{color}" fill-opacity="0.35"/>' for i, v in enumerate(raw)]
        pts = " ".join(f"{x(i):.1f},{y(a):.1f}" for i, a in enumerate(avg) if a is not None)
        out.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>')
        if avg[-1] is not None:
            ends.append((y(avg[-1]), f"{name} {per_week(avg[-1])}/wk"))
    if end_labels and len(ends) == 2 and abs(ends[0][0] - ends[1][0]) >= 16:
        out += [f'<text class="ink" x="{x(n - 1) + 10:.1f}" y="{ey + 4:.1f}">{text}</text>' for ey, text in ends]
    out += week_columns(plot, weeks, x)
    out.append("</svg>")
    assert top >= max(weeks.done + weeks.new), f"axis top {top} cuts off the data"
    assert out[-1] == "</svg>", "chart not closed"
    return "".join(out)


@dataclass(frozen=True)
class EpicChart:
    """Epics to chart, one row each, and whether the plan model is shown beside current pace."""

    rows: list[dict]
    start: date
    has_plan: bool
    max_days: int

    def __post_init__(self) -> None:
        assert self.rows, "no epics to chart"
        assert all("epic" in e for e in self.rows), "every row needs an epic key"


def epic_marks(e: dict, has_plan: bool) -> list[tuple[str, str, str | None]]:
    """(label, colour, 85% date) for the models shown on one epic's row."""
    marks: list[tuple[str, str, str | None]] = []
    pace = e.get("current_pace")
    if has_plan and pace:
        marks.append(("At current pace", "var(--pace)", pace["p85"]))
    main = e.get("priority") if has_plan else pace
    if main:
        marks.append(("In the plan" if has_plan else "At current pace", "var(--plan)", main["p85"]))
    assert len(marks) <= 2, f"{len(marks)} marks on one row"
    assert all(color.startswith("var(--") for _, color, _ in marks), "marks use theme colours"
    return marks


def epic_row(plot: Plot, chart: EpicChart, i: int) -> list[str]:
    """One epic: its key, a guide line, and a dot per model (joined when there are two)."""
    e = chart.rows[i]
    cy = 8 + 30 * i + 15
    out = [f'<text class="ink" x="{plot.left - 10}" y="{cy + 4:.1f}" text-anchor="end">{escape(e["epic"])}</text>',
           f'<line x1="{plot.left}" x2="{plot.left + plot.inner_width:.1f}" y1="{cy:.1f}" y2="{cy:.1f}" stroke="var(--grid)" stroke-width="1"/>']
    marks = epic_marks(e, chart.has_plan)
    xs = [plot.x((as_date(d) - chart.start).days if d else plot.hi) for _, _, d in marks]
    if len(xs) == 2:
        out.append(f'<line x1="{min(xs):.1f}" x2="{max(xs):.1f}" y1="{cy:.1f}" y2="{cy:.1f}" stroke="var(--axis)" stroke-width="2"/>')
    for (label, color, d), cx in zip(marks, xs, strict=True):
        when = long_date(d) if d else f"more than {chart.max_days // 365} years away"
        title = f"{e['epic']}: {e['open']} open"
        out.append(f'<g class="mark"{tip(title, (when, label + ", 85% confidence", color))}>'
                   f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="11" fill="transparent"/>'
                   f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="5" fill="{color}" stroke="var(--surface)" stroke-width="2"/></g>')
        if not d or (as_date(d) - chart.start).days > plot.hi:
            out.append(f'<text x="{cx - 8:.1f}" y="{cy - 9:.1f}" text-anchor="end">later →</text>')
    if not e.get("current_pace"):
        out.append(f'<text x="{(xs[0] if xs else plot.left) + 12:.1f}" y="{cy + 4:.1f}">no recent progress</text>')
    assert out[0].startswith("<text"), "rows start with the epic key"
    assert len(xs) == len(marks), "one position per mark"
    return out


def epic_chart(width: int, chart: EpicChart) -> str:
    """One row per epic: the 85% finish date in the plan (aqua) and at current pace (grey)."""
    days = [(as_date(d) - chart.start).days for e in chart.rows for _, _, d in epic_marks(e, chart.has_plan) if d]
    span = min(max(days + [30]) + 10, 730)
    plot = Plot(width, 84 if width >= WIDE else 70, 8, 30 * len(chart.rows), 0, span)
    out = [svg_open(width, plot.base + 30, "85% confidence finish date per epic"),
           f'<line x1="{plot.left}" x2="{plot.left}" y1="{plot.top}" y2="{plot.base}" stroke="var(--axis)" stroke-width="1"/>',
           *date_ticks(plot, chart.start)]
    for i in range(len(chart.rows)):
        out += epic_row(plot, chart, i)
    out.append("</svg>")
    assert span <= 730, f"epic chart spans {span} days"
    assert out[-1] == "</svg>", "chart not closed"
    return "".join(out)


def aging_dot(x: float, cy: float, it: dict) -> str:
    """One open item: filled if in progress, hollow if to do; hover for the details."""
    assert it["risk"] in RISK, f"unknown risk {it['risk']!r}"
    assert it["age_days"] >= 0, f"{it['key']} is {it['age_days']} days old"
    fill = "var(--ink-2)" if it["status"] == "in_progress" else "var(--surface)"
    kind = it["type"] or "items"
    detail = f"{RISK[it['risk']][2].lower()}; 85% of finished {kind} take ≤ {it['lead_time_p85']}d"
    title = f"{it['key']} · {it['type']} · " + it["status"].replace("_", " ")
    hover = tip(title, (f"{it['age_days']} days old", detail, None), (it["epic"] or "no epic", "epic", None))
    return (f'<g class="mark"{hover}><circle cx="{x:.1f}" cy="{cy:.1f}" r="8" fill="transparent"/>'
            f'<circle cx="{x:.1f}" cy="{cy:.1f}" r="4" fill="{fill}" stroke="var(--ink-2)" stroke-width="1.5"/></g>')


def aging_chart(width: int, items: list[dict]) -> str:
    """Per issue type: open items by age, with shaded zones where an item is older than 85% / 95% of finished ones."""
    types = sorted({i["type"] or "?" for i in items}, key=lambda t: -sum((i["type"] or "?") == t for i in items))
    top, step = nice_max(max(max(i["age_days"] for i in items), max(i["lead_time_p95"] for i in items)))
    plot = Plot(width, 112 if width >= WIDE else 92, 8, 40 * len(types), 0, int(top))
    out = [svg_open(width, plot.base + 28, "Age of open items by type, against how long finished items usually take")]
    for k in range(5):
        v = k * step
        if width >= WIDE or k % 2 == 0:
            out.append(f'<text x="{plot.x(v):.1f}" y="{plot.base + 20}" text-anchor="middle">{v:g}d</text>')
        out.append(f'<line x1="{plot.x(v):.1f}" x2="{plot.x(v):.1f}" y1="{plot.top}" y2="{plot.base}" stroke="var(--grid)" stroke-width="1"/>')
    for row, ty in enumerate(types):
        y0 = plot.top + 40 * row
        its = sorted((i for i in items if (i["type"] or "?") == ty), key=lambda i: i["age_days"])
        p85, p95 = plot.x(its[0]["lead_time_p85"]), plot.x(its[0]["lead_time_p95"])
        star = "*" if its[0]["basis"] != its[0]["type"] else ""
        out += [f'<rect x="{p85:.1f}" y="{y0 + 4}" width="{p95 - p85:.1f}" height="32" fill="var(--warning)" fill-opacity="0.18"/>',
                f'<rect x="{p95:.1f}" y="{y0 + 4}" width="{plot.x(top) - p95:.1f}" height="32" fill="var(--critical)" fill-opacity="0.12"/>',
                f'<text class="ink" x="{plot.left - 10}" y="{y0 + 24}" text-anchor="end">{escape(ty[:14])}{star}</text>']
        out += [aging_dot(plot.x(it["age_days"]), y0 + 20 + ((k % 5) - 2) * 4, it) for k, it in enumerate(its)]
    out.append("</svg>")
    assert out.count("</svg>") == 1, "chart closed twice"
    assert len(types) >= 1, "no issue types to chart"
    return "".join(out)
