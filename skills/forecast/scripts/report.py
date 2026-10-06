"""Render the forecast data as one self-contained HTML page (inline SVG, no network)."""

from __future__ import annotations

import math
from datetime import date, timedelta
from html import escape

W = 720  # SVG viewBox width; charts scale to the container

CSS = """
.mc {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --ring: rgba(11,11,11,0.10);
  --s1: #2a78d6; --s2: #eb6834; --s3: #1baf7a;
  --good: #0ca30c; --warning: #fab219; --critical: #d03b3b;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) .mc {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,0.10);
    --s1: #3987e5; --s2: #d95926; --s3: #199e70;
  }
}
:root[data-theme="dark"] .mc {
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,0.10);
  --s1: #3987e5; --s2: #d95926; --s3: #199e70;
}
html, body { margin: 0; background: #f9f9f7; }
@media (prefers-color-scheme: dark) { html:where(:not([data-theme="light"])), html:where(:not([data-theme="light"])) body { background: #0d0d0d; } }
.mc { background: var(--page); color: var(--ink); font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
  min-height: 100vh; padding: 32px 16px 64px; box-sizing: border-box; }
.mc main { max-width: 960px; margin: 0 auto; }
.mc h1 { font-size: 26px; font-weight: 600; margin: 0 0 4px; }
.mc h2 { font-size: 18px; font-weight: 600; margin: 0 0 4px; }
.mc .chart-head { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 8px; margin: 8px 0; }
.mc .toggle { display: inline-flex; border: 1px solid var(--ring); border-radius: 8px; overflow: hidden; }
.mc .toggle button { font: inherit; font-size: 13px; color: var(--ink-2); background: transparent; border: 0; padding: 5px 12px; cursor: pointer; }
.mc .toggle button[aria-pressed="true"] { background: var(--grid); color: var(--ink); font-weight: 600; }
.mc svg[data-xhair]:focus-visible { outline: 2px solid var(--s1); outline-offset: 4px; border-radius: 4px; }
.mc .sub { color: var(--ink-2); margin: 0 0 24px; }
.mc .lede { color: var(--ink-2); margin: 0 0 16px; max-width: 70ch; }
.mc section { background: var(--surface); border: 1px solid var(--ring); border-radius: 12px; padding: 20px; margin: 0 0 20px; }
.mc .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin: 0 0 20px; }
.mc .tile { background: var(--surface); border: 1px solid var(--ring); border-radius: 12px; padding: 14px 16px; }
.mc .tile .label { color: var(--ink-2); font-size: 13px; }
.mc .tile .value { font-size: 24px; font-weight: 600; margin-top: 2px; }
.mc .tile .note { color: var(--muted); font-size: 12px; }
.mc .hero .value { font-size: 30px; }
.mc svg { width: 100%; height: auto; display: block; overflow: visible; }
.mc svg text { font: 12px system-ui, -apple-system, "Segoe UI", sans-serif; fill: var(--muted); font-variant-numeric: tabular-nums; }
.mc svg text.ink { fill: var(--ink-2); }
.mc .legend { display: flex; flex-wrap: wrap; gap: 6px 18px; color: var(--ink-2); font-size: 13px; margin: 0 0 8px; }
.mc .legend span { display: inline-flex; align-items: center; gap: 6px; }
.mc .key-line { width: 14px; height: 2px; border-radius: 1px; display: inline-block; }
.mc .key-dot { width: 9px; height: 9px; border-radius: 50%; display: inline-block; }
.mc .key-rect { width: 10px; height: 10px; border-radius: 2px; display: inline-block; }
.mc table { border-collapse: collapse; width: 100%; font-size: 13px; font-variant-numeric: tabular-nums; }
.mc th, .mc td { text-align: left; padding: 6px 10px 6px 0; border-bottom: 1px solid var(--grid); }
.mc th { color: var(--ink-2); font-weight: 600; }
.mc td:first-child, .mc th:first-child { white-space: nowrap; }
.mc td.num, .mc th.num { text-align: right; }
.mc .scroll { overflow-x: auto; }
.mc details { margin-top: 12px; color: var(--ink-2); font-size: 13px; }
.mc details summary { cursor: pointer; }
.mc details > div { margin-top: 8px; }
.mc .badge { display: inline-flex; align-items: center; gap: 5px; white-space: nowrap; }
.mc .warn { color: var(--ink-2); border-left: 3px solid var(--warning); padding: 2px 0 2px 10px; margin: 8px 0; }
.mc .note { color: var(--muted); font-size: 13px; }
.mc [data-tip] { cursor: default; }
.mc [data-tip]:focus { outline: none; }
.mc [data-tip]:focus-visible { outline: 2px solid var(--s1); outline-offset: 2px; }
.mc .col:hover .hair, .mc .col:focus .hair { opacity: 1; }
.mc .hair { opacity: 0; }
.mc .mark:hover, .mc .mark:focus { filter: brightness(1.15); }
#tip { position: fixed; pointer-events: none; z-index: 10; display: none; background: var(--surface); color: var(--ink);
  border: 1px solid var(--ring); border-radius: 8px; padding: 8px 10px; font-size: 13px; box-shadow: 0 4px 16px rgba(0,0,0,.12);
  max-width: 280px; }
#tip .t { color: var(--ink-2); margin-bottom: 2px; }
#tip .r { display: flex; align-items: center; gap: 6px; }
#tip .r b { font-weight: 600; }
"""

JS = """
(() => {
  const tip = document.getElementById('tip');
  const showRows = (rows, x, y) => {
    tip.replaceChildren();
    const title = document.createElement('div'); title.className = 't'; title.textContent = rows[0]; tip.append(title);
    for (const [value, label, color] of rows.slice(1)) {
      const r = document.createElement('div'); r.className = 'r';
      if (color) { const k = document.createElement('span'); k.className = 'key-line'; k.style.background = color; r.append(k); }
      const b = document.createElement('b'); b.textContent = value; r.append(b);
      if (label) { const l = document.createElement('span'); l.textContent = label; r.append(l); }
      tip.append(r);
    }
    tip.style.display = 'block';
    const w = tip.offsetWidth, h = tip.offsetHeight;
    tip.style.left = Math.min(x + 14, innerWidth - w - 8) + 'px';
    tip.style.top = Math.max(8, y - h - 12) + 'px';
  };
  const show = (el, x, y) => showRows(JSON.parse(el.dataset.tip), x, y);
  const hide = () => { tip.style.display = 'none'; };
  const fmtDate = (iso, add) => {
    const d = new Date(iso + 'T00:00:00Z'); d.setUTCDate(d.getUTCDate() + add);
    return d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' });
  };
  // Continuous charts: a crosshair snaps to the nearest day; arrow keys step (shift = a week).
  document.querySelectorAll('svg[data-xhair]').forEach(svg => {
    const d = JSON.parse(svg.dataset.xhair);
    const line = svg.querySelector('.xline');
    let idx = null;
    const render = (i, cx, cy) => {
      idx = Math.max(0, Math.min(d.n - 1, i));
      const x = d.l + (d.n > 1 ? d.pw * idx / (d.n - 1) : 0);
      line.setAttribute('x1', x); line.setAttribute('x2', x); line.style.opacity = 1;
      const rows = [fmtDate(d.start, d.offset + idx)];
      for (const [label, color, values] of d.series) {
        const v = values[idx];
        rows[rows.length] = [(v * 100).toFixed(v > 0.005 && v < 0.995 ? 0 : 1) + '%', label, color];
      }
      showRows(rows, cx, cy);
    };
    const toIndex = (cx, cy) => {
      const pt = svg.createSVGPoint(); pt.x = cx; pt.y = cy;
      const p = pt.matrixTransform(svg.getScreenCTM().inverse());
      return Math.round((p.x - d.l) / d.pw * (d.n - 1));
    };
    svg.addEventListener('pointermove', e => render(toIndex(e.clientX, e.clientY), e.clientX, e.clientY));
    svg.addEventListener('pointerleave', () => { line.style.opacity = 0; hide(); });
    svg.addEventListener('focus', () => { const b = svg.getBoundingClientRect(); render(idx ?? Math.floor(d.n / 2), b.left + b.width / 2, b.top + 40); });
    svg.addEventListener('blur', () => { line.style.opacity = 0; hide(); });
    svg.addEventListener('keydown', e => {
      if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
      e.preventDefault();
      const step = (e.shiftKey ? 7 : 1) * (e.key === 'ArrowLeft' ? -1 : 1);
      const b = svg.getBoundingClientRect();
      render((idx ?? 0) + step, b.left + b.width / 2, b.top + 40);
    });
  });
  // View toggles (probability density by default, cumulative on request).
  document.querySelectorAll('.toggle').forEach(group => {
    const panes = group.parentElement.querySelectorAll('[data-pane]');
    group.querySelectorAll('button').forEach(btn => btn.addEventListener('click', () => {
      group.querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', String(b === btn)));
      panes.forEach(p => { p.hidden = p.dataset.pane !== btn.dataset.view; });
    }));
  });
  document.addEventListener('pointerover', e => { const el = e.target.closest('[data-tip]'); if (el) show(el, e.clientX, e.clientY); });
  document.addEventListener('pointermove', e => {
    const el = e.target.closest('[data-tip]');
    if (el) show(el, e.clientX, e.clientY); else if (!e.target.closest('svg[data-xhair]')) hide();
  });
  document.addEventListener('focusin', e => { const el = e.target.closest('[data-tip]'); if (el) { const b = el.getBoundingClientRect(); show(el, b.right, b.top); } });
  document.addEventListener('focusout', e => { if (e.target.closest('[data-tip]')) hide(); });
})();
"""

STATUS = {"stale": ("var(--critical)", "▲", "Stale"), "at risk": ("var(--warning)", "●", "At risk"), "ok": ("var(--muted)", "○", "OK")}


# --- helpers -----------------------------------------------------------------


def tip(title: str, *rows: tuple[str, str, str | None]) -> str:
    import json

    return f' tabindex="0" data-tip="{escape(json.dumps([title, *rows]), quote=True)}"'


def fmt_date(d: str | date | None) -> str:
    if d is None:
        return "—"
    d = date.fromisoformat(d) if isinstance(d, str) else d
    return f"{d.day} {d:%b %Y}"


def short_date(d: date) -> str:
    return f"{d.day} {d:%b}"


def nice_max(v: float) -> tuple[float, float]:
    """A round axis max and tick step covering v."""
    if v <= 0:
        return 1, 1
    exp = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 2.5, 5, 10):
        step = m * exp / 4
        if step * 4 >= v:
            return step * 4, step
    return v, v / 4


def column(x: float, y: float, w: float, base: float, r: float = 4) -> str:
    """Column with a rounded data-end, square at the baseline."""
    h = base - y
    r = min(r, w / 2, h)
    return f"M{x:.1f},{base:.1f}V{y + r:.1f}Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f}H{x + w - r:.1f}Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f}V{base:.1f}Z"


def table(head: list[str], rows: list[list[str]], num: set[int] = frozenset()) -> str:
    th = "".join(f'<th class="{"num" if i in num else ""}">{escape(h)}</th>' for i, h in enumerate(head))
    body = "".join(
        "<tr>" + "".join(f'<td class="{"num" if i in num else ""}">{c}</td>' for i, c in enumerate(r)) + "</tr>" for r in rows
    )
    return f'<div class="scroll"><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'


def data_table(head, rows, num=frozenset(), label="Show data") -> str:
    return f"<details><summary>{escape(label)}</summary><div>{table(head, rows, num)}</div></details>"


# --- charts --------------------------------------------------------------------


def weekly_chart(weekly: list[dict]) -> str:
    """Completed vs created per week: two lines, crosshair tooltip per week."""
    if not weekly:
        return '<p class="note">No weekly history.</p>'
    l, r, t, b, h = 40, 96, 12, 28, 240
    pw, ph = W - l - r, h - t - b
    n = len(weekly)
    top, step = nice_max(max(max(w["completed"], w["created"]) for w in weekly))
    x = lambda i: l + (pw * i / (n - 1) if n > 1 else pw / 2)  # noqa: E731
    y = lambda v: t + ph - ph * v / top  # noqa: E731
    out = [f'<svg viewBox="0 0 {W} {h}" role="img" aria-label="Items completed and created per week">']
    v = 0.0
    while v <= top + 1e-9:
        out.append(f'<line x1="{l}" x2="{l + pw}" y1="{y(v):.1f}" y2="{y(v):.1f}" stroke="var(--grid)" stroke-width="1"/>')
        out.append(f'<text x="{l - 8}" y="{y(v) + 4:.1f}" text-anchor="end">{v:g}</text>')
        v += step
    out.append(f'<line x1="{l}" x2="{l + pw}" y1="{y(0):.1f}" y2="{y(0):.1f}" stroke="var(--axis)" stroke-width="1"/>')
    every = max(1, round(n / 6))
    for i, w in enumerate(weekly):
        if i % every == 0 or i == n - 1:
            out.append(f'<text x="{x(i):.1f}" y="{h - 8}" text-anchor="middle">{short_date(date.fromisoformat(w["week"]))}</text>')
    series = [("completed", "Completed", "var(--s1)"), ("created", "Created", "var(--s2)")]
    for key, _, color in series:
        pts = " ".join(f"{x(i):.1f},{y(w[key]):.1f}" for i, w in enumerate(weekly))
        out.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>')
    # End markers + direct labels (dropped when they'd collide; the legend still names them).
    ends = [(y(weekly[-1][k]), lab, c, weekly[-1][k]) for k, lab, c in series]
    collide = abs(ends[0][0] - ends[1][0]) < 16
    for ey, lab, c, val in ends:
        out.append(f'<circle cx="{x(n - 1):.1f}" cy="{ey:.1f}" r="4" fill="{c}" stroke="var(--surface)" stroke-width="2"/>')
        if not collide:
            out.append(f'<text class="ink" x="{x(n - 1) + 10:.1f}" y="{ey + 4:.1f}">{lab} {val}</text>')
    # Crosshair columns: the whole week band is the hit target.
    band = pw / max(n - 1, 1)
    for i, w in enumerate(weekly):
        x0 = max(l, x(i) - band / 2)
        x1 = min(l + pw, x(i) + band / 2) if n > 1 else l + pw
        t_ = tip(f"Week of {fmt_date(w['week'])}", (str(w["completed"]), "completed", "var(--s1)"), (str(w["created"]), "created", "var(--s2)"))
        out.append(
            f'<g class="col"{t_}><rect x="{x0:.1f}" y="{t}" width="{x1 - x0:.1f}" height="{ph}" fill="transparent"/>'
            f'<line class="hair" x1="{x(i):.1f}" x2="{x(i):.1f}" y1="{t}" y2="{t + ph}" stroke="var(--axis)" stroke-width="1"/></g>'
        )
    out.append("</svg>")
    legend = (
        '<div class="legend"><span><i class="key-line" style="background:var(--s1)"></i>Completed per week</span>'
        '<span><i class="key-line" style="background:var(--s2)"></i>Created per week</span></div>'
    )
    tbl = data_table(["Week of", "Completed", "Created"], [[fmt_date(w["week"]), str(w["completed"]), str(w["created"])] for w in weekly], {1, 2})
    return legend + "".join(out) + tbl


def cdf(samples: list[float], days: int) -> list[float]:
    """P(done within d days) for d = 0..days; runs that never finish count as not done."""
    counts = [0] * (days + 1)
    for v in samples:
        if v != math.inf and v <= days:
            counts[int(v)] += 1
    out, acc = [], 0
    for c in counts:
        acc += c
        out.append(acc / len(samples))
    return out


def kde(samples: list[float], days: int) -> list[float]:
    """Chance of finishing on each day 0..days: a Gaussian kernel density of the finished runs
    (Silverman bandwidth, at least 1 day), scaled by the share of runs that finish at all."""
    finite = [v for v in samples if v != math.inf]
    if len(finite) < 2:
        return [0.0] * (days + 1)
    n = len(finite)
    mean = sum(finite) / n
    sd = math.sqrt(sum((v - mean) ** 2 for v in finite) / n)
    bw = max(1.0, 1.06 * sd * n ** -0.2)
    counts: dict[float, int] = {}
    for v in finite:
        counts[v] = counts.get(v, 0) + 1
    norm = len(samples) * bw * math.sqrt(2 * math.pi)
    reach = 4 * bw
    return [
        sum(c * math.exp(-0.5 * ((g - v) / bw) ** 2) for v, c in counts.items() if abs(g - v) <= reach) / norm
        for g in range(days + 1)
    ]


def _xhair(l: float, pw: float, start: date, n: int, series: list) -> str:
    import json

    payload = {"l": l, "pw": pw, "start": start.isoformat(), "offset": 0, "n": n, "series": series}
    return f' tabindex="0" data-xhair="{escape(json.dumps(payload), quote=True)}"'


def _date_ticks(out: list, start: date, span: int, x, y_text: float) -> None:
    step = next((s for s in (7, 14, 28, 56, 91, 182, 365) if span / s <= 7), 730)
    d = (-start.weekday()) % 7 if step < 365 else 0  # weekly-ish ticks land on Mondays
    while d <= span:
        out.append(f'<text x="{x(d):.1f}" y="{y_text}" text-anchor="middle">{short_date(start + timedelta(days=d))}</text>')
        d += step


FINISH_MODELS = [("no_growth", "Finish date", "var(--s1)")]


def finish_chart(samples: dict[str, list[float]], start: date, percentiles: dict, target: str | None, max_days: int) -> str:
    """Finish-date distribution: probability density by default, cumulative chance behind a toggle.

    Both views share the x axis and the hover readout (chance of being done by the hovered date).
    """
    models = [(k, lab, c) for k, lab, c in FINISH_MODELS if samples.get(k)]
    if not models:
        return '<p class="note">No simulation results.</p>'
    frozen = sorted(v for v in samples[models[0][0]] if v != math.inf)
    if not frozen:
        return '<p class="note">No simulated run finished within the limit.</p>'
    # Frame the frozen-scope distribution; a slower model runs off the right edge (its end label says how much).
    ends = [frozen[min(len(frozen) - 1, int(len(frozen) * 0.995))] * 1.15]
    if target:
        ends.append((date.fromisoformat(target) - start).days * 1.05)
    span = int(min(max_days, max(ends + [14])))
    cum = {k: cdf(samples[k], span) for k, *_ in models}
    dens = {k: kde(samples[k], span) for k, *_ in models}
    readout = [["chance done by then", c, [round(v, 4) for v in cum[k]]] for k, lab, c in models]

    l, r, t, b, h = 52, 110, 24, 28, 250
    pw, ph = W - l - r, h - t - b
    x = lambda d: l + pw * d / span  # noqa: E731

    def frame(aria: str, top: float, y_ticks: list[tuple[float, str]]) -> list[str]:
        out = [f'<svg viewBox="0 0 {W} {h}" role="img" aria-label="{escape(aria)}"{_xhair(l, pw, start, span + 1, readout)}>']
        for v, lab in y_ticks:
            yy = t + ph - ph * v / top
            out.append(f'<line x1="{l}" x2="{l + pw}" y1="{yy:.1f}" y2="{yy:.1f}" stroke="var(--grid)" stroke-width="1"/>')
            out.append(f'<text x="{l - 8}" y="{yy + 4:.1f}" text-anchor="end">{lab}</text>')
        out.append(f'<line x1="{l}" x2="{l + pw}" y1="{t + ph}" y2="{t + ph}" stroke="var(--axis)" stroke-width="1"/>')
        if target:
            td = (date.fromisoformat(target) - start).days
            if 0 <= td <= span:
                out.append(f'<line x1="{x(td):.1f}" x2="{x(td):.1f}" y1="{t}" y2="{t + ph}" stroke="var(--ink-2)" stroke-width="1"/>')
                out.append(f'<text class="ink" x="{x(td) + 6:.1f}" y="{t + 10}">target {short_date(date.fromisoformat(target))}</text>')
        _date_ticks(out, start, span, x, h - 8)
        return out

    def finish(out: list[str], end_labels: list[tuple[float, str]]) -> str:
        if len(end_labels) < 2 or abs(end_labels[0][0] - end_labels[1][0]) >= 16:
            for ly, text in end_labels:
                out.append(f'<text class="ink" x="{x(span) + 10:.1f}" y="{ly + 4:.1f}">{escape(text)}</text>')
        out.append(f'<line class="xline" x1="0" x2="0" y1="{t}" y2="{t + ph}" stroke="var(--axis)" stroke-width="1" style="opacity:0"/>')
        out.append("</svg>")
        return "".join(out)

    # Probability density: chance of finishing on each day, in % per day.
    top_d, step_d = nice_max(max(max(v) for v in dens.values()) * 100 * 1.1)
    yd = lambda v: t + ph - ph * v * 100 / top_d  # noqa: E731
    ticks, v = [], 0.0
    while v <= top_d + 1e-9:
        ticks.append((v, f"{v:g}%"))
        v += step_d
    pdf = frame("Probability of finishing on each date", top_d, ticks)
    for k, lab, c in models:
        pts = " ".join(f"{x(d):.1f},{yd(p):.1f}" for d, p in enumerate(dens[k]))
        pdf.append(f'<polygon points="{x(0):.1f},{t + ph} {pts} {x(span):.1f},{t + ph}" fill="{c}" fill-opacity="0.1"/>')
        pdf.append(f'<polyline points="{pts}" fill="none" stroke="{c}" stroke-width="2" stroke-linejoin="round"/>')
    for p in ("p50", "p85", "p95"):
        if percentiles.get(p):
            dd = (date.fromisoformat(percentiles[p]) - start).days
            if 0 <= dd <= span:
                py = yd(dens[models[0][0]][dd])
                pdf.append(f'<line x1="{x(dd):.1f}" x2="{x(dd):.1f}" y1="{py:.1f}" y2="{t + ph}" stroke="var(--ink-2)" stroke-width="1"/>')
                pdf.append(f'<text class="ink" x="{x(dd):.1f}" y="{py - 8:.1f}" text-anchor="middle">{p[1:]}%</text>')
    pdf_svg = finish(pdf, [])

    # Cumulative: chance of being done by each date.
    cdf_out = frame("Chance of being done by each date", 1, [(v, f"{v:.0%}") for v in (0, 0.25, 0.5, 0.75, 1)])
    labels = []
    for k, lab, c in models:
        pts = " ".join(f"{x(d):.1f},{t + ph - ph * v:.1f}" for d, v in enumerate(cum[k]))
        cdf_out.append(f'<polyline points="{pts}" fill="none" stroke="{c}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>')
        end = cum[k][-1]
        cdf_out.append(f'<circle cx="{x(span):.1f}" cy="{t + ph - ph * end:.1f}" r="4" fill="{c}" stroke="var(--surface)" stroke-width="2"/>')
        if len(models) > 1:
            labels.append((t + ph - ph * end, f"{lab} {end:.0%}"))
    cdf_svg = finish(cdf_out, labels)

    legend = "<div></div>" if len(models) == 1 else '<div class="legend">' + "".join(
        f'<span><i class="key-line" style="background:{c}"></i>{escape(lab)}</span>' for _, lab, c in models
    ) + "</div>"
    toggle = (
        '<div class="toggle" role="group" aria-label="Chart view">'
        '<button type="button" data-view="pdf" aria-pressed="true">Probability</button>'
        '<button type="button" data-view="cdf" aria-pressed="false">Cumulative</button></div>'
    )
    weekly = [
        [fmt_date(start + timedelta(days=d))] + [f"{cum[k][d]:.0%}" for k, *_ in models]
        for d in range(7, span + 1, 7)
    ]
    tbl = data_table(["Date", "Chance done by then"], weekly,
                     set(range(1, len(models) + 1)), "Show weekly chances")
    edge = short_date(start + timedelta(days=span))
    beyond = "".join(
        f'<p class="note">{escape(lab)}: {cum[k][-1]:.0%} of runs finish by {edge}, '
        + (f"{sum(v != math.inf for v in samples[k]) / len(samples[k]):.0%} within {max_days // 365} years.</p>"
           if any(v == math.inf for v in samples[k]) else "the rest later.</p>")
        for k, lab, _ in models
        if cum[k][-1] < 0.99
    )
    return (
        f'<div><div class="chart-head">{legend}{toggle}</div>'
        f'<div data-pane="pdf">{pdf_svg}</div><div data-pane="cdf" hidden>{cdf_svg}</div>{beyond}{tbl}</div>'
    )


def epic_dots(epics: list[dict], start: date, has_priority: bool, max_days: int) -> str:
    """One row per epic: the p85 date under each model on a shared date axis."""
    models = [("current_pace", "Current pace", "var(--s1)")]
    if has_priority:
        models.append(("priority", "Priority order", "var(--s2)"))
    models.append(("sole_focus", "Sole focus", "var(--s3)"))
    rows = [e for e in epics if e["open"]]
    if not rows:
        return '<p class="note">No open epics.</p>'
    finite = [
        (date.fromisoformat(e[m]["p85"]) - start).days for e in rows for m, _, _ in models if e.get(m) and e[m]["p85"]
    ]
    span = max(finite + [30])
    l, r, t, rh = 92, 72, 24, 30
    pw = W - l - r
    h = t + rh * len(rows) + 28
    x = lambda d: l + pw * min(d, span) / span  # noqa: E731
    out = [f'<svg viewBox="0 0 {W} {h}" role="img" aria-label="85% confidence finish date per epic under each model">']
    _, step = nice_max(span)
    tick = step
    out.append(f'<text x="{l}" y="{h - 8}" text-anchor="middle">{short_date(start)}</text>')
    while tick <= span + 1e-9:
        tx = x(tick)
        out.append(f'<line x1="{tx:.1f}" x2="{tx:.1f}" y1="{t - 8}" y2="{t + rh * len(rows)}" stroke="var(--grid)" stroke-width="1"/>')
        out.append(f'<text x="{tx:.1f}" y="{h - 8}" text-anchor="middle">{short_date(start + timedelta(days=int(tick)))}</text>')
        tick += step
    out.append(f'<line x1="{l}" x2="{l}" y1="{t - 8}" y2="{t + rh * len(rows)}" stroke="var(--axis)" stroke-width="1"/>')
    for i, e in enumerate(rows):
        cy = t + rh * i + rh / 2
        out.append(f'<text class="ink" x="{l - 12}" y="{cy + 4:.1f}" text-anchor="end">{escape(e["epic"])}</text>')
        pts = []
        for m, label, color in models:
            fc = e.get(m)
            if not fc:
                continue
            d = fc["p85"]
            days = (date.fromisoformat(d) - start).days if d else math.inf
            pts.append((days, label, color, d))
        if not pts:
            out.append(f'<text x="{l + 8}" y="{cy + 4:.1f}">no progress in the window</text>')
            continue
        xs = [x(d) for d, *_ in pts]
        out.append(f'<line x1="{min(xs):.1f}" x2="{max(xs):.1f}" y1="{cy:.1f}" y2="{cy:.1f}" stroke="var(--axis)" stroke-width="1"/>')
        for days, label, color, d in sorted(pts, key=lambda p: -p[0]):
            beyond = days == math.inf or days > span
            when = fmt_date(d) if d else f"beyond {max_days} days"
            t_ = tip(f"{e['epic']} · {e['open']} open", (when, f"{label}, 85%", color))
            cx = x(span if beyond else days)
            out.append(f'<g class="mark"{t_}><circle cx="{cx:.1f}" cy="{cy:.1f}" r="12" fill="transparent"/>'
                       f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="5" fill="{color}" stroke="var(--surface)" stroke-width="2"/></g>')
            if beyond and label == "Current pace":
                out.append(f'<text x="{cx + 10:.1f}" y="{cy + 4:.1f}">later →</text>')
    out.append("</svg>")
    legend = '<div class="legend">' + "".join(
        f'<span><i class="key-dot" style="background:{c}"></i>{escape(lab)}</span>' for _, lab, c in models
    ) + "<span>(each dot: 85% confidence finish date)</span></div>"
    return legend + "".join(out)


def aging_strips(items: list[dict]) -> str:
    """Per issue type: each open item's age as a dot, with the type's p85/p95 lead time marked."""
    if not items:
        return '<p class="note">No open items.</p>'
    types = sorted({i["type"] or "?" for i in items}, key=lambda t: -sum((i["type"] or "?") == t for i in items))
    top = max(max(i["age_days"] for i in items), max(i["lead_time_p95"] for i in items))
    top, step = nice_max(top)
    l, r, t, rh = 104, 24, 12, 36
    pw = W - l - r
    h = t + rh * len(types) + 28
    x = lambda d: l + pw * d / top  # noqa: E731
    out = [f'<svg viewBox="0 0 {W} {h}" role="img" aria-label="Age of open items by type against usual lead time">']
    v = 0.0
    while v <= top + 1e-9:
        out.append(f'<line x1="{x(v):.1f}" x2="{x(v):.1f}" y1="{t}" y2="{t + rh * len(types)}" stroke="var(--grid)" stroke-width="1"/>')
        out.append(f'<text x="{x(v):.1f}" y="{h - 8}" text-anchor="middle">{v:g}d</text>')
        v += step
    for row, ty in enumerate(types):
        cy = t + rh * row + rh / 2
        out.append(f'<text class="ink" x="{l - 12}" y="{cy + 4:.1f}" text-anchor="end">{escape(ty[:14])}</text>')
        sample = next(i for i in items if (i["type"] or "?") == ty)
        for p, w_ in (("lead_time_p85", 1), ("lead_time_p95", 2)):
            px = x(sample[p])
            out.append(f'<line x1="{px:.1f}" x2="{px:.1f}" y1="{cy - 12:.1f}" y2="{cy + 12:.1f}" stroke="var(--ink-2)" stroke-width="{w_}"/>')
        for it in (i for i in items if (i["type"] or "?") == ty):
            jitter = (sum(map(ord, it["key"])) % 9 - 4) * 1.6
            color, icon, label = STATUS[it["risk"]]
            t_ = tip(
                f"{it['key']} · {it['type']} · {it['status'].replace('_', ' ')}",
                (f"{it['age_days']} days old", f"{label}; p85 {it['lead_time_p85']}d, p95 {it['lead_time_p95']}d", None),
                (it["epic"] or "no epic", "epic", None),
            )
            cx = x(it["age_days"])
            out.append(f'<g class="mark"{t_}><circle cx="{cx:.1f}" cy="{cy + jitter:.1f}" r="11" fill="transparent"/>'
                       f'<circle cx="{cx:.1f}" cy="{cy + jitter:.1f}" r="4" fill="{color}" stroke="var(--surface)" stroke-width="2"/></g>')
    out.append("</svg>")
    legend = (
        '<div class="legend">'
        + "".join(f'<span><i class="key-dot" style="background:{c}"></i>{lab}</span>' for c, _, lab in STATUS.values())
        + '<span><i class="key-line" style="background:var(--ink-2);width:1px;height:12px"></i>p85 lead time</span>'
        + '<span><i class="key-line" style="background:var(--ink-2);width:2px;height:12px"></i>p95 lead time</span></div>'
    )
    return legend + "".join(out)


# --- page ----------------------------------------------------------------------


def badge(risk: str) -> str:
    color, icon, label = STATUS[risk]
    return f'<span class="badge"><span style="color:{color}" aria-hidden="true">{icon}</span>{label}</span>'


def pct(v: float | None) -> str:
    return "—" if v is None else f"{v:.0%}"


def render(d: dict) -> str:
    scope, fc, ep, ag, st, cal = d["scope"], d["forecast"], d["epics"], d["aging"], d["stats"], d["calibrate"]
    start = date.fromisoformat(fc["start"])
    h = fc["history"]
    frozen = fc["percentiles"].get("no_growth", {})
    max_days = fc["max_days"]

    tiles = [
        ("hero", "85% confidence", fmt_date(frozen.get("p85")), f"{fc['items']} open items, if nothing new is added"),
        ("", "Completed per week", f"{h['completed_per_week']:g}", f"{h['completed']} in {h['days']} days"),
        ("", "Created per week", f"{h['created_per_week']:g}", f"{h['created']} in {h['days']} days"),
    ]
    if "chance" in fc:
        tiles.insert(1, ("", f"Chance done by {fmt_date(fc['target_date'])}", pct(fc["chance"]["no_growth"]),
                         "if nothing new is added"))
    tile_html = '<div class="tiles">' + "".join(
        f'<div class="tile {cls}"><div class="label">{escape(lab)}</div><div class="value">{escape(val)}</div>'
        f'<div class="note">{escape(note)}</div></div>'
        for cls, lab, val, note in tiles
    ) + "</div>"

    done_wk, new_wk = h["completed_per_week"], h["created_per_week"]
    notes = [w[0].upper() + w[1:] + "." for w in fc.get("warnings", [])]
    if new_wk >= done_wk:
        notes.append(
            f"New work is being created as fast as it is finished ({new_wk:g} vs {done_wk:g} a week). These dates assume "
            "nothing new is added; at the current rate the backlog won't shrink on its own."
        )
    elif new_wk >= 0.5 * done_wk:
        notes.append(
            f"New work is still arriving ({new_wk:g} a week vs {done_wk:g} finished). These dates cover today's "
            "backlog only; anything added pushes them out."
        )
    warnings = "".join(f'<p class="warn">{escape(n)}</p>' for n in notes)
    pct_rows = [[f"{p}%", fmt_date(frozen.get(f"p{p}"))] for p in (50, 70, 85, 95)]
    pct_head = ["Confidence", "Done by"]

    sections = []
    sections.append(
        f"""<section><h2>When will the open backlog be done?</h2>
<p class="lede">{fc['items']} open items (excluding epics and sub-tasks). Each of {fc['runs']:,} simulated runs replays randomly
chosen days from the last {h['days']} days of history until the backlog is empty. The curve shows how likely each
finish date is; hover it (or focus it and use the arrow keys) for the chance of being done by any date.</p>{warnings}
{finish_chart({k.split("(")[1].rstrip(")"): v for k, v in fc["_samples"].items()}, start, frozen, fc.get("target_date"), max_days)}
{table(pct_head, pct_rows)}</section>"""
    )
    sections.append(
        f"""<section><h2>Throughput and intake</h2>
<p class="lede">The backlog only shrinks when the blue line sits above the orange one.</p>
{weekly_chart(st["weekly"])}</section>"""
    )

    if ep["epics"]:
        prio = ep.get("priority")
        epic_rows = []
        for e in ep["epics"]:
            cp = e["current_pace"]
            row = [
                escape(e["epic"]),
                escape(e["status"]) + (" ⚠" if e["status"] == "done" and e["open"] else ""),
                str(e["open"]),
                str(e["completed_in_window"]),
                ("no progress" if e["open"] and not cp else fmt_date(cp["p85"]) if cp and cp["p85"] else "later" if cp else "done"),
            ]
            if prio:
                pr = e.get("priority")
                row.append(fmt_date(pr["p85"]) if pr and pr["p85"] else "paused" if not pr else "later")
            row.append(fmt_date(e["sole_focus"]["p85"]) if e.get("sole_focus") else "—")
            if "target_date" in ep:
                c = e.get("chance", {})
                row.append(" / ".join(pct(c.get(m)) for m in (["current_pace"] + (["priority"] if prio else []))))
            epic_rows.append(row)
        head = ["Epic", "Epic status", "Open", "Done in window", "Current pace (85%)"]
        if prio:
            head.append("Priority order (85%)")
        head.append("Sole focus (85%)")
        if "target_date" in ep:
            head.append(f"Chance by {fmt_date(ep['target_date'])}" + (" (pace / priority)" if prio else ""))
        stale = [e["epic"] for e in ep["epics"] if e["status"] == "done" and e["open"]]
        prio_note = (
            f" Priority order: {escape(' → '.join(prio['order']))}, {prio['wip']} at a time, with "
            f"{prio['epic_share']:.0%} of team throughput on epic work; other epics paused."
            if prio else ""
        )
        sections.append(
            f"""<section><h2>Epics</h2>
<p class="lede"><b>Current pace</b> replays each epic's own recent completions; <b>sole focus</b> is the whole team on
that epic alone — the best case.{prio_note}</p>
{epic_dots(ep["epics"], date.fromisoformat(ep["start"]), bool(prio), max_days)}
{table(head, epic_rows, {2, 3})}
{f'<p class="warn">Marked done but with open work: {escape(", ".join(stale))}.</p>' if stale else ''}
<p class="note">{ep['open_without_epic']} open items have no epic.</p></section>"""
        )

    flagged = [i for i in ag["items"] if i["risk"] != "ok"]
    flagged.sort(key=lambda i: (i["status"] != "in_progress", -i["age_days"]))
    aging_rows = [
        [escape(i["key"]), escape(i["type"] or "?"), escape(i["status"].replace("_", " ")), escape(i["epic"] or "—"),
         str(i["age_days"]), f"{i['older_than_pct_of_completed']}%", badge(i["risk"])]
        for i in flagged
    ]
    sections.append(
        f"""<section><h2>Ageing open work</h2>
<p class="lede">{ag['stale']} stale (older than 95% of recently finished items of the same type) and {ag['at_risk']} at
risk (older than 85%), out of {ag['open']} open. In-progress items are listed first: they are the likeliest to be stuck;
old to-do items are usually deprioritised.</p>
{aging_strips(ag["items"])}
{table(["Key", "Type", "Status", "Epic", "Age (days)", "Older than", "Risk"], aging_rows, {4, 5}) if aging_rows else '<p class="note">Nothing is older than its type usually takes.</p>'}
<p class="note">Age and lead time count from creation. Only finished items set the baseline, so a flag means
“older than nearly everything we finish”, not “late”.</p></section>"""
    )

    resolved = {
        m: {p: s for p, s in by_p.items() if s["held"] + s["missed"]} for m, by_p in cal["summary"].items()
    }
    resolved = {m: v for m, v in resolved.items() if v}
    if resolved:
        cal_rows = [
            [escape(m), p[1:] + "%", f"{s['held'] / (s['held'] + s['missed']):.0%}", f"{s['held']}/{s['held'] + s['missed']}", str(s["pending"])]
            for m, by_p in resolved.items() for p, s in by_p.items()
        ]
        cal_html = table(["Model", "Confidence", "Held", "Resolved", "Pending"], cal_rows, {2, 3, 4})
    else:
        n = len(cal["forecasts"])
        cal_html = (
            f'<p class="note">{n} forecasts recorded; none has reached its date yet.</p>'
            if n else '<p class="note">No forecasts recorded yet. Each forecast run is saved and scored here once its date passes.</p>'
        )
    sections.append(
        f"""<section><h2>Track record</h2>
<p class="lede">How often past forecasts held. A well-calibrated 85% answer holds about 85% of the time.</p>{cal_html}</section>"""
    )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(scope)} delivery forecast</title><style>{CSS}</style></head>
<body><div class="mc"><main>
<h1>{escape(scope)} delivery forecast</h1>
<p class="sub">As of {fmt_date(h['end'])} · history {fmt_date(h['start'])} – {fmt_date(h['end'])} · {fc['runs']:,} Monte Carlo runs</p>
{tile_html}
{"".join(sections)}
<p class="note">Generated by mcmc. Forecasts resample historical daily throughput; they assume items are roughly similar
in size and that the future looks like the history window.</p>
</main><div id="tip" role="tooltip"></div></div><script>{JS}</script></body></html>
"""
