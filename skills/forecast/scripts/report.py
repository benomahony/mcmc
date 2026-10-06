"""Render the forecast data as one self-contained HTML page (inline SVG, no network).

Every chart is drawn twice, at desktop and phone widths, and CSS shows the one that fits,
so chart text stays legible instead of shrinking with the viewBox.

Colour carries one meaning across the page: blue = work finishing (and the forecast built
from it), orange = new work arriving, aqua = the epic plan, grey = current pace, and the
status colours only for stuck work.
"""

from __future__ import annotations

import json
import math
from collections.abc import Collection
from datetime import date, datetime, timedelta
from html import escape

WIDE, NARROW = 720, 380  # SVG viewBox widths for the two renders

CSS = """
.mc {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --ring: rgba(11,11,11,0.10);
  --done: #2a78d6; --new: #eb6834; --plan: #1baf7a; --pace: #898781;
  --warning: #fab219; --critical: #d03b3b;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) .mc {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,0.10);
    --done: #3987e5; --new: #d95926; --plan: #199e70; --pace: #898781;
  }
}
:root[data-theme="dark"] .mc {
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,0.10);
  --done: #3987e5; --new: #d95926; --plan: #199e70; --pace: #898781;
}
html, body { margin: 0; background: #f9f9f7; }
@media (prefers-color-scheme: dark) { html:where(:not([data-theme="light"])), html:where(:not([data-theme="light"])) body { background: #0d0d0d; } }
:root[data-theme="dark"], :root[data-theme="dark"] body { background: #0d0d0d; }
.mc { background: var(--page); color: var(--ink); font: 15px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif;
  min-height: 100vh; padding: 32px 16px 64px; box-sizing: border-box; }
.mc main { max-width: 920px; margin: 0 auto; }
.mc h1 { font-size: 28px; line-height: 1.2; font-weight: 650; margin: 0 0 6px; }
.mc h2 { font-size: 21px; line-height: 1.3; font-weight: 650; margin: 0 0 6px; }
.mc .meta { color: var(--muted); font-size: 13px; margin: 0 0 24px; }
.mc .meta code { font: 12px ui-monospace, SFMono-Regular, Menlo, monospace; color: var(--ink-2); }
.mc .answer { font-size: 17px; margin: 0 0 14px; max-width: 68ch; }
.mc .lede { color: var(--ink-2); margin: 0 0 14px; max-width: 68ch; }
.mc section { background: var(--surface); border: 1px solid var(--ring); border-radius: 12px; padding: 22px; margin: 0 0 20px; }
.mc section.summary { border-left: 4px solid var(--ink); }
.mc .summary ul { margin: 0; padding-left: 20px; }
.mc .summary li { margin: 0 0 8px; max-width: 72ch; }
.mc .summary .basis { color: var(--muted); font-size: 13px; margin: 14px 0 0; max-width: 72ch; }
.mc .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 12px; margin: 0 0 20px; }
.mc .tile { background: var(--surface); border: 1px solid var(--ring); border-radius: 12px; padding: 14px 16px; }
.mc .tile .label { color: var(--ink-2); font-size: 13px; }
.mc .tile .value { font-size: 24px; font-weight: 650; margin-top: 2px; line-height: 1.25; }
.mc .tile .note { color: var(--muted); font-size: 12px; margin-top: 2px; }
.mc .chart-head { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 8px; margin: 4px 0 6px; }
.mc .toggle { display: inline-flex; border: 1px solid var(--ring); border-radius: 8px; overflow: hidden; }
.mc .toggle button { font: inherit; font-size: 13px; color: var(--ink-2); background: transparent; border: 0; padding: 5px 12px; cursor: pointer; }
.mc .toggle button[aria-pressed="true"] { background: var(--grid); color: var(--ink); font-weight: 600; }
.mc .wide { display: block; } .mc .narrow { display: none; }
@media (max-width: 700px) {
  .mc .wide { display: none; } .mc .narrow { display: block; }
  .mc .narrow svg { max-width: 440px; }
  .mc section { padding: 16px; }
  .mc .hide-narrow { display: none; }
}
.mc svg { width: 100%; height: auto; display: block; overflow: visible; }
.mc svg text { font: 12px system-ui, -apple-system, "Segoe UI", sans-serif; fill: var(--muted); font-variant-numeric: tabular-nums; }
.mc svg text.ink { fill: var(--ink-2); }
.mc svg text.strong { fill: var(--ink); font-weight: 600; }
.mc svg[data-xhair]:focus-visible { outline: 2px solid var(--done); outline-offset: 4px; border-radius: 4px; }
.mc .legend { display: flex; flex-wrap: wrap; gap: 6px 18px; color: var(--ink-2); font-size: 13px; margin: 0 0 8px; }
.mc .legend span { display: inline-flex; align-items: center; gap: 6px; }
.mc .key-line { width: 16px; height: 2px; border-radius: 1px; display: inline-block; }
.mc .key-dot { width: 9px; height: 9px; border-radius: 50%; display: inline-block; box-sizing: border-box; }
.mc .key-zone { width: 14px; height: 12px; border-radius: 2px; display: inline-block; }
.mc .pcts { display: flex; flex-wrap: wrap; gap: 4px 20px; font-size: 14px; color: var(--ink-2); margin: 10px 0 0; font-variant-numeric: tabular-nums; }
.mc .pcts b { color: var(--ink); font-weight: 600; }
.mc table { border-collapse: collapse; width: 100%; font-size: 13px; font-variant-numeric: tabular-nums; }
.mc th, .mc td { text-align: left; padding: 7px 12px 7px 0; border-bottom: 1px solid var(--grid); vertical-align: top; }
.mc th { color: var(--ink-2); font-weight: 600; }
.mc td:first-child, .mc th:first-child { white-space: nowrap; }
.mc td.num, .mc th.num { text-align: right; }
.mc .scroll { overflow-x: auto; margin-top: 12px; }
.mc details { margin-top: 12px; color: var(--ink-2); font-size: 13px; }
.mc details summary { cursor: pointer; }
.mc .badge { display: inline-flex; align-items: center; gap: 5px; white-space: nowrap; }
.mc .callout { border-radius: 8px; padding: 10px 12px; margin: 0 0 14px; background: color-mix(in srgb, var(--warning) 14%, transparent); max-width: 72ch; }
.mc .note { color: var(--muted); font-size: 13px; margin: 10px 0 0; }
.mc ul.plain { margin: 0; padding-left: 20px; } .mc ul.plain li { margin: 0 0 6px; }
.mc footer { color: var(--muted); font-size: 13px; margin-top: 24px; max-width: 72ch; }
.mc [data-tip] { cursor: default; }
.mc [data-tip]:focus { outline: none; }
.mc [data-tip]:focus-visible { outline: 2px solid var(--done); outline-offset: 2px; }
.mc .col:hover .hair, .mc .col:focus .hair { opacity: 1; }
.mc .hair { opacity: 0; }
.mc .mark:hover circle:last-child, .mc .mark:focus circle:last-child { stroke-width: 3; }
.mc .vh { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
#tip { position: fixed; pointer-events: none; z-index: 10; display: none; background: var(--surface); color: var(--ink);
  border: 1px solid var(--ring); border-radius: 8px; padding: 8px 10px; font-size: 13px; box-shadow: 0 4px 16px rgba(0,0,0,.12);
  max-width: 300px; }
#tip .t { color: var(--ink-2); margin-bottom: 2px; }
#tip .r { display: flex; align-items: center; gap: 6px; }
#tip .r b { font-weight: 600; }
@media print {
  .mc { min-height: 0; padding: 0; background: #fff; }
  .mc section { break-inside: avoid; border-color: #ccc; }
  .mc .toggle, #tip { display: none !important; }
  .mc .wide { display: block !important; } .mc .narrow { display: none !important; }
}
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
    tip.style.left = Math.max(8, Math.min(x + 14, innerWidth - w - 8)) + 'px';
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
  // View toggles: each switches the panes in its own section.
  document.querySelectorAll('.toggle').forEach(group => {
    const panes = group.closest('section').querySelectorAll('[data-pane]');
    group.querySelectorAll('button').forEach(btn => btn.addEventListener('click', () => {
      group.querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', String(b === btn)));
      panes.forEach(p => { p.hidden = p.dataset.pane !== btn.dataset.view; });
    }));
  });
  // Printing shows everything: every pane and every collapsed table.
  addEventListener('beforeprint', () => {
    document.querySelectorAll('details').forEach(d => { d.dataset.wasOpen = d.open; d.open = true; });
    document.querySelectorAll('[data-pane]').forEach(p => { p.dataset.wasHidden = p.hidden; p.hidden = false; });
  });
  addEventListener('afterprint', () => {
    document.querySelectorAll('details').forEach(d => { d.open = d.dataset.wasOpen === 'true'; });
    document.querySelectorAll('[data-pane]').forEach(p => { p.hidden = p.dataset.wasHidden === 'true'; });
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

RISK = {
    "stale": ("var(--critical)", "▲", "Stale"),
    "at risk": ("var(--warning)", "◆", "At risk"),
    "ok": ("var(--muted)", "", "OK"),
}


# --- formatting ------------------------------------------------------------------


def _d(v: str | date) -> date:
    return date.fromisoformat(v) if isinstance(v, str) else v


def long_date(v: str | date | None) -> str:
    if v is None:
        return "—"
    v = _d(v)
    return f"{v.day} {v:%b %Y}"


def short_date(v: str | date) -> str:
    v = _d(v)
    return f"{v.day} {v:%b}"


def month(v: str | date) -> str:
    return f"{_d(v):%b %Y}"


def pct(v: float | None) -> str:
    return "—" if v is None else f"{v:.0%}"


def per_week(v: float) -> str:
    return f"{v:.0f}" if v >= 10 else f"{v:.1f}".rstrip("0").rstrip(".")


def tip(title: str, *rows: tuple[str, str, str | None]) -> str:
    return f' tabindex="0" data-tip="{escape(json.dumps([title, *rows]), quote=True)}"'


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


def table(head: list[str], rows: list[list[str]], num: Collection[int] = (), narrow_hide: Collection[int] = ()) -> str:
    def cls(i):
        return " ".join(c for c in ("num" if i in num else "", "hide-narrow" if i in narrow_hide else "") if c)

    th = "".join(f'<th class="{cls(i)}">{escape(h)}</th>' for i, h in enumerate(head))
    body = "".join("<tr>" + "".join(f'<td class="{cls(i)}">{c}</td>' for i, c in enumerate(r)) + "</tr>" for r in rows)
    return f'<div class="scroll"><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'


def responsive(draw, *args) -> str:
    """Render a chart at desktop and phone widths; CSS shows the one that fits."""
    return f'<div class="wide">{draw(WIDE, *args)}</div><div class="narrow">{draw(NARROW, *args)}</div>'


def date_ticks(out: list, start: date, lo: int, hi: int, x, y: float, w: int) -> None:
    """Date labels along the x axis, spaced for the chart width."""
    span = max(1, hi - lo)
    max_ticks = 7 if w >= WIDE else 4
    step = next((s for s in (7, 14, 28, 56, 91, 182, 365) if span / s <= max_ticks), 730)
    if step >= 28:  # month starts read better than arbitrary days
        d0 = start + timedelta(days=lo)
        m = date(d0.year + (d0.month == 12), d0.month % 12 + 1, 1) if d0.day > 1 else d0
        months = max(1, round(step / 30.4))
        while (m - start).days <= hi:
            out.append(f'<text x="{x((m - start).days):.1f}" y="{y}" text-anchor="middle">{m:%b}{" " + str(m.year) if m.month == 1 else ""}</text>')
            mm = m.month - 1 + months
            m = date(m.year + mm // 12, mm % 12 + 1, 1)
        return
    d = lo + (-(start + timedelta(days=lo)).weekday()) % 7  # Mondays
    while d <= hi:
        out.append(f'<text x="{x(d):.1f}" y="{y}" text-anchor="middle">{short_date(start + timedelta(days=d))}</text>')
        d += step


# --- statistics ----------------------------------------------------------------------


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


def rolling(values: list[float], k: int = 4) -> list[float | None]:
    return [None if i + 1 < k else sum(values[i + 1 - k : i + 1]) / k for i in range(len(values))]


# --- charts --------------------------------------------------------------------------


def finish_frame(samples: list[float], start: date, target: str | None) -> tuple[int, int]:
    """Day range framing the finish-date distribution (and the target, if any)."""
    finite = sorted(v for v in samples if v != math.inf)
    lo_q, hi_q = finite[int(len(finite) * 0.005)], finite[min(len(finite) - 1, int(len(finite) * 0.995))]
    pad = max(4, int((hi_q - lo_q) * 0.12))
    lo, hi = max(0, int(lo_q) - pad), int(hi_q) + pad
    if target:
        td = (_d(target) - start).days
        lo, hi = min(lo, max(0, td - 4)), max(hi, td + 4)
    return lo, hi


def _xhair(l: float, pw: float, start: date, offset: int, n: int, series: list) -> str:
    payload = {"l": l, "pw": pw, "start": start.isoformat(), "offset": offset, "n": n, "series": series}
    return f' tabindex="0" data-xhair="{escape(json.dumps(payload), quote=True)}"'


def _markers(out: list, x, t: float, base: float, start: date, percentiles: dict, target: str | None,
             curve_y=None) -> None:
    """Target (strong line, label row 1) and percentile marks (hairlines, label row 2), dropping colliding labels."""
    if target:
        tx = x((_d(target) - start).days)
        out.append(f'<line x1="{tx:.1f}" x2="{tx:.1f}" y1="{t - 22}" y2="{base}" stroke="var(--ink)" stroke-width="1.5"/>')
        out.append(f'<text class="strong" x="{tx + 5:.1f}" y="{t - 12}">Target {short_date(target)}</text>')
    placed: list[float] = []
    for p in ("p85", "p50", "p95"):  # 85% is the headline: it always gets its label
        if not percentiles.get(p):
            continue
        dd = (_d(percentiles[p]) - start).days
        px = x(dd)
        y1 = curve_y(dd) if curve_y else t + 6
        out.append(f'<line x1="{px:.1f}" x2="{px:.1f}" y1="{y1:.1f}" y2="{base}" stroke="var(--ink-2)" stroke-width="1"/>')
        if all(abs(px - q) >= 34 for q in placed):
            out.append(f'<text class="ink" x="{px:.1f}" y="{t + 2}" text-anchor="middle">{p[1:]}%</text>')
            out.append(f'<line x1="{px:.1f}" x2="{px:.1f}" y1="{t + 6}" y2="{y1:.1f}" stroke="var(--axis)" stroke-width="1"/>')
            placed.append(px)


def density_chart(w: int, samples: list[float], start: date, percentiles: dict, target: str | None) -> str:
    """How likely each finish date is: smoothed density, no y axis (the shape is the message)."""
    lo, hi = finish_frame(samples, start, target)
    dens = kde(samples, hi)[lo:]
    cum = cdf(samples, hi)[lo:]
    l, r, t, b, h = 12, 12, 40, 28, 230 if w >= WIDE else 210
    pw, ph = w - l - r, h - t - b
    n = hi - lo + 1
    top = max(dens) * 1.05
    x = lambda d: l + pw * (d - lo) / (n - 1)  # noqa: E731
    y = lambda v: t + ph - ph * v / top  # noqa: E731
    p85 = percentiles.get("p85")
    aria = f"Finish date distribution: 50% by {long_date(percentiles.get('p50'))}, 85% by {long_date(p85)}, 95% by {long_date(percentiles.get('p95'))}"
    readout = [["chance done by then", "var(--done)", [round(v, 4) for v in cum]]]
    out = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="{escape(aria)}"{_xhair(l, pw, start, lo, n, readout)}>']
    pts = " ".join(f"{x(lo + i):.1f},{y(v):.1f}" for i, v in enumerate(dens))
    out.append(f'<polygon points="{x(lo):.1f},{t + ph} {pts} {x(hi):.1f},{t + ph}" fill="var(--done)" fill-opacity="0.1"/>')
    out.append(f'<polyline points="{pts}" fill="none" stroke="var(--done)" stroke-width="2" stroke-linejoin="round"/>')
    out.append(f'<line x1="{l}" x2="{l + pw}" y1="{t + ph}" y2="{t + ph}" stroke="var(--axis)" stroke-width="1"/>')
    _markers(out, x, t, t + ph, start, percentiles, target, curve_y=lambda dd: y(dens[min(max(dd - lo, 0), n - 1)]))
    date_ticks(out, start, lo, hi, x, h - 8, w)
    out.append(f'<line class="xline" x1="0" x2="0" y1="{t}" y2="{t + ph}" stroke="var(--axis)" stroke-width="1" style="opacity:0"/>')
    out.append("</svg>")
    return "".join(out)


def cumulative_chart(w: int, samples: list[float], start: date, percentiles: dict, target: str | None) -> str:
    """Chance of being done by each date."""
    lo, hi = finish_frame(samples, start, target)
    cum = cdf(samples, hi)[lo:]
    l, r, t, b, h = 44, 12, 40, 28, 230 if w >= WIDE else 210
    pw, ph = w - l - r, h - t - b
    n = hi - lo + 1
    x = lambda d: l + pw * (d - lo) / (n - 1)  # noqa: E731
    y = lambda v: t + ph - ph * v  # noqa: E731
    readout = [["chance done by then", "var(--done)", [round(v, 4) for v in cum]]]
    out = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="Chance of being done by each date"{_xhair(l, pw, start, lo, n, readout)}>']
    for v in (0, 0.5, 1) if w < WIDE else (0, 0.25, 0.5, 0.75, 1):
        out.append(f'<line x1="{l}" x2="{l + pw}" y1="{y(v):.1f}" y2="{y(v):.1f}" stroke="var(--grid)" stroke-width="1"/>')
        out.append(f'<text x="{l - 8}" y="{y(v) + 4:.1f}" text-anchor="end">{v:.0%}</text>')
    out.append(f'<line x1="{l}" x2="{l + pw}" y1="{y(0):.1f}" y2="{y(0):.1f}" stroke="var(--axis)" stroke-width="1"/>')
    pts = " ".join(f"{x(lo + i):.1f},{y(v):.1f}" for i, v in enumerate(cum))
    out.append(f'<polyline points="{pts}" fill="none" stroke="var(--done)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>')
    _markers(out, x, t, t + ph, start, percentiles, target, curve_y=lambda dd: y(cum[min(max(dd - lo, 0), n - 1)]))
    date_ticks(out, start, lo, hi, x, h - 8, w)
    out.append(f'<line class="xline" x1="0" x2="0" y1="{t}" y2="{t + ph}" stroke="var(--axis)" stroke-width="1" style="opacity:0"/>')
    out.append("</svg>")
    return "".join(out)


def weekly_chart(w: int, weeks: list[dict]) -> str:
    """Finished vs created per full week: 4-week averages as lines, each week as a faint dot."""
    n = len(weeks)
    done = [wk["completed"] for wk in weeks]
    new = [wk["created"] for wk in weeks]
    avg_done, avg_new = rolling(done), rolling(new)
    l, r, t, b = 36, (104 if w >= WIDE else 12), 12, 28
    h = 240 if w >= WIDE else 220
    pw, ph = w - l - r, h - t - b
    top, step = nice_max(max(done + new))
    x = lambda i: l + (pw * i / (n - 1) if n > 1 else pw / 2)  # noqa: E731
    y = lambda v: t + ph - ph * v / top  # noqa: E731
    out = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="Items finished and created per week, with 4-week averages">']
    v = 0.0
    while v <= top + 1e-9:
        out.append(f'<line x1="{l}" x2="{l + pw}" y1="{y(v):.1f}" y2="{y(v):.1f}" stroke="var(--grid)" stroke-width="1"/>')
        out.append(f'<text x="{l - 8}" y="{y(v) + 4:.1f}" text-anchor="end">{v:g}</text>')
        v += step
    out.append(f'<line x1="{l}" x2="{l + pw}" y1="{y(0):.1f}" y2="{y(0):.1f}" stroke="var(--axis)" stroke-width="1"/>')
    every = max(1, math.ceil(n / (6 if w >= WIDE else 3)))
    for i, wk in enumerate(weeks):
        if (n - 1 - i) % every == 0:
            out.append(f'<text x="{x(i):.1f}" y="{h - 8}" text-anchor="middle">{short_date(wk["week"])}</text>')
    series = [(done, avg_done, "Finished", "var(--done)"), (new, avg_new, "Created", "var(--new)")]
    for raw, avg, _, color in series:
        for i, val in enumerate(raw):
            out.append(f'<circle cx="{x(i):.1f}" cy="{y(val):.1f}" r="3.5" fill="{color}" fill-opacity="0.35"/>')
        pts = " ".join(f"{x(i):.1f},{y(a):.1f}" for i, a in enumerate(avg) if a is not None)
        out.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>')
    last_done, last_new = avg_done[-1], avg_new[-1]
    if w >= WIDE and last_done is not None and last_new is not None:
        ends = [(y(last_done), f"Finished {per_week(last_done)}/wk"), (y(last_new), f"Created {per_week(last_new)}/wk")]
        if abs(ends[0][0] - ends[1][0]) >= 16:
            for ey, text in ends:
                out.append(f'<text class="ink" x="{x(n - 1) + 10:.1f}" y="{ey + 4:.1f}">{text}</text>')
    band = pw / max(n - 1, 1)
    for i, wk in enumerate(weeks):
        x0, x1 = max(l, x(i) - band / 2), min(l + pw, x(i) + band / 2)
        rows = [(str(done[i]), "finished", "var(--done)"), (str(new[i]), "created", "var(--new)")]
        a_done, a_new = avg_done[i], avg_new[i]
        if a_done is not None and a_new is not None:
            rows += [(per_week(a_done), "finished, 4-week average", "var(--done)"), (per_week(a_new), "created, 4-week average", "var(--new)")]
        out.append(
            f'<g class="col"{tip(f"Week of {long_date(wk['week'])}", *rows)}><rect x="{x0:.1f}" y="{t}" width="{max(x1 - x0, 1):.1f}" height="{ph}" fill="transparent"/>'
            f'<line class="hair" x1="{x(i):.1f}" x2="{x(i):.1f}" y1="{t}" y2="{t + ph}" stroke="var(--axis)" stroke-width="1"/></g>'
        )
    out.append("</svg>")
    return "".join(out)


def epic_chart(w: int, rows: list[dict], start: date, has_plan: bool, max_days: int) -> str:
    """One row per epic: 85% finish date in the plan (aqua) and at current pace (grey)."""
    dates = []
    for e in rows:
        for m in ("priority", "current_pace"):
            if e.get(m) and e[m]["p85"]:
                dates.append((_d(e[m]["p85"]) - start).days)
    span = min(max(dates + [30]) + 10, 730)
    l, r, t, rh = (84 if w >= WIDE else 70), 16, 8, 30
    pw = w - l - r
    h = t + rh * len(rows) + 30
    x = lambda d: l + pw * min(d, span) / span  # noqa: E731
    out = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="85% confidence finish date per epic">']
    out.append(f'<line x1="{l}" x2="{l}" y1="{t}" y2="{t + rh * len(rows)}" stroke="var(--axis)" stroke-width="1"/>')
    date_ticks(out, start, 0, span, x, h - 8, w)
    main = ("priority", "In the plan", "var(--plan)") if has_plan else ("current_pace", "At current pace", "var(--plan)")
    for i, e in enumerate(rows):
        cy = t + rh * i + rh / 2
        out.append(f'<text class="ink" x="{l - 10}" y="{cy + 4:.1f}" text-anchor="end">{escape(e["epic"])}</text>')
        out.append(f'<line x1="{l}" x2="{l + pw}" y1="{cy:.1f}" y2="{cy:.1f}" stroke="var(--grid)" stroke-width="1"/>')
        marks = []
        if has_plan:
            pace = e.get("current_pace")
            if pace:
                marks.append(("current_pace", "At current pace", "var(--pace)", pace["p85"]))
        fc = e.get(main[0])
        if fc:
            marks.append((main[0], main[1], main[2], fc["p85"]))
        xs = [x((_d(d) - start).days) if d else x(span) for *_, d in marks]
        if len(xs) == 2:
            out.append(f'<line x1="{min(xs):.1f}" x2="{max(xs):.1f}" y1="{cy:.1f}" y2="{cy:.1f}" stroke="var(--axis)" stroke-width="2"/>')
        for (key, label, color, d), cx in zip(marks, xs):
            when = long_date(d) if d else f"more than {max_days // 365} years away"
            beyond = not d or (_d(d) - start).days > span
            out.append(
                f'<g class="mark"{tip(f"{e['epic']}: {e['open']} open", (when, f"{label}, 85% confidence", color))}>'
                f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="11" fill="transparent"/>'
                f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="5" fill="{color}" stroke="var(--surface)" stroke-width="2"/></g>'
            )
            if beyond:
                out.append(f'<text x="{cx - 8:.1f}" y="{cy - 9:.1f}" text-anchor="end">later →</text>')
        if not e.get("current_pace"):
            cx = xs[0] if xs else l
            out.append(f'<text x="{cx + 12:.1f}" y="{cy + 4:.1f}">no recent progress</text>')
    out.append("</svg>")
    return "".join(out)


def aging_chart(w: int, items: list[dict], types: list[str]) -> str:
    """Per issue type: open items by age; shaded zones where an item is older than 85% / 95% of finished ones."""
    top, step = nice_max(max(max(i["age_days"] for i in items), max(i["lead_time_p95"] for i in items)))
    l, r, t, rh = (112 if w >= WIDE else 92), 12, 8, 40
    pw = w - l - r
    h = t + rh * len(types) + 28
    x = lambda d: l + pw * d / top  # noqa: E731
    out = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="Age of open items by type, against how long finished items usually take">']
    v = 0.0
    while v <= top + 1e-9:
        if w >= WIDE or v % (step * 2) == 0:
            out.append(f'<text x="{x(v):.1f}" y="{h - 8}" text-anchor="middle">{v:g}d</text>')
        out.append(f'<line x1="{x(v):.1f}" x2="{x(v):.1f}" y1="{t}" y2="{t + rh * len(types)}" stroke="var(--grid)" stroke-width="1"/>')
        v += step
    for row, ty in enumerate(types):
        y0 = t + rh * row
        cy = y0 + rh / 2
        its = [i for i in items if (i["type"] or "?") == ty]
        p85, p95 = its[0]["lead_time_p85"], its[0]["lead_time_p95"]
        out.append(f'<rect x="{x(p85):.1f}" y="{y0 + 4}" width="{max(x(p95) - x(p85), 0):.1f}" height="{rh - 8}" fill="var(--warning)" fill-opacity="0.18"/>')
        out.append(f'<rect x="{x(p95):.1f}" y="{y0 + 4}" width="{max(x(top) - x(p95), 0):.1f}" height="{rh - 8}" fill="var(--critical)" fill-opacity="0.12"/>')
        star = "*" if its[0]["basis"] != its[0]["type"] else ""
        out.append(f'<text class="ink" x="{l - 10}" y="{cy + 4:.1f}" text-anchor="end">{escape(ty[:14])}{star}</text>')
        for k, it in enumerate(sorted(its, key=lambda i: i["age_days"])):
            jitter = ((k % 5) - 2) * 4
            started = it["status"] == "in_progress"
            fill = "var(--ink-2)" if started else "var(--surface)"
            t_ = tip(
                f"{it['key']} · {it['type']} · {it['status'].replace('_', ' ')}",
                (f"{it['age_days']} days old", f"{RISK[it['risk']][2].lower()}; 85% of finished {it['type'] or 'items'} take ≤ {it['lead_time_p85']}d", None),
                (it["epic"] or "no epic", "epic", None),
            )
            out.append(
                f'<g class="mark"{t_}><circle cx="{x(it["age_days"]):.1f}" cy="{cy + jitter:.1f}" r="8" fill="transparent"/>'
                f'<circle cx="{x(it["age_days"]):.1f}" cy="{cy + jitter:.1f}" r="4" fill="{fill}" stroke="var(--ink-2)" stroke-width="1.5"/></g>'
            )
    out.append("</svg>")
    return "".join(out)


# --- page ----------------------------------------------------------------------------


def badge(risk: str) -> str:
    color, icon, label = RISK[risk]
    return f'<span class="badge"><span style="color:{color}" aria-hidden="true">{icon}</span>{label}</span>'


def epic_link(key: str | None) -> str:
    return escape(key) if key else "—"


def render(d: dict) -> str:
    scope, fc, ep, ag, st, cal = d["scope"], d["forecast"], d["epics"], d["aging"], d["stats"], d["calibrate"]
    start = _d(fc["start"])
    h = fc["history"]
    pcts = fc["percentiles"]["no_growth"]
    samples = fc["_samples"]["days (no_growth)"]
    max_days = fc["max_days"]
    target = fc.get("target_date")
    chance = fc.get("chance", {}).get("no_growth")

    # Throughput vs intake, from full weeks only (edge weeks of the window are partial).
    weeks = [wk for wk in st["weekly"] if wk.get("days", 7) == 7]
    done_wk, new_wk = h["completed_per_week"], h["created_per_week"]
    net = done_wk - new_wk
    shrinking = net > 0.1 * done_wk
    recent = weeks[-4:]
    earlier = weeks[:-4]
    trend = None
    if len(recent) == 4 and len(earlier) >= 4:
        r_new, e_new = sum(w["created"] for w in recent) / 4, sum(w["created"] for w in earlier) / len(earlier)
        r_done, e_done = sum(w["completed"] for w in recent) / 4, sum(w["completed"] for w in earlier) / len(earlier)
        trend = (r_done, r_new, e_done, e_new)

    # Epics: the plan (if given), other open epics, and epics marked done with open work (clean-up).
    prio = ep.get("priority")
    by_key = {e["epic"]: e for e in ep["epics"]}
    plan = [by_key[k] for k in prio["order"]] if prio else []
    in_plan = {e["epic"] for e in plan}
    open_epics = sorted(
        (e for e in ep["epics"] if e["open"] and e["status"] != "done" and e["epic"] not in in_plan),
        key=lambda e: (e["current_pace"] is None, (e["current_pace"] or {}).get("p85") or "9999"),
    )
    done_but_open = [e for e in ep["epics"] if e["status"] == "done" and e["open"] and e["epic"] not in in_plan]

    # Stuck work.
    flagged = sorted((i for i in ag["items"] if i["risk"] != "ok"), key=lambda i: (i["status"] != "in_progress", -i["age_days"]))
    stuck = [i for i in flagged if i["status"] == "in_progress"]
    stale_todo = [i for i in flagged if i["status"] != "in_progress" and i["risk"] == "stale"]

    # --- summary -----------------------------------------------------------------
    bullets = []
    if shrinking:
        bullets.append(
            f"<b>The backlog is shrinking</b>, by about {per_week(net)} items a week ({per_week(done_wk)} finished, "
            f"{per_week(new_wk)} created). If nothing new were added, the {fc['items']} open items would be done by "
            f"<b>{long_date(pcts['p85'])}</b> (85% confidence)."
        )
    else:
        bullets.append(
            f"<b>The backlog isn't shrinking.</b> Over the last {h['days']} days about as many items were created as finished "
            f"({per_week(new_wk)} vs {per_week(done_wk)} a week), so the {fc['items']} open items won't clear on their own. "
            f"Even if nothing new were added, they'd take until <b>{long_date(pcts['p85'])}</b> (85% confidence)."
        )
    if trend and abs(trend[1] - trend[3]) >= 0.25 * max(trend[3], 1):
        r_done, r_new, _, e_new = trend
        dropped = r_new < e_new
        outlook = ""
        if dropped and r_new < r_done and not shrinking:
            outlook = " If that holds, the backlog will start to shrink."
        elif not dropped and r_new > r_done:
            outlook = " At that rate the backlog grows."
        bullets.append(
            f"New work has {'dropped' if dropped else 'risen'} lately: {per_week(r_new)} a week over the last 4 weeks, "
            f"against {per_week(e_new)} before.{outlook}"
        )
    if target and chance is not None:
        bullets.append(f"There's a <b>{pct(chance)} chance</b> of clearing today's backlog by {long_date(target)}, even with nothing new added.")
    if plan:
        last = plan[-1].get("priority") or {}
        slowest = max((e["current_pace"]["p85"] for e in plan if e.get("current_pace") and e["current_pace"]["p85"]), default=None)
        no_pace = [e["epic"] for e in plan if not e.get("current_pace")]
        bullets.append(
            f"Worked in order ({escape(' → '.join(prio['order']))}), the planned epics would all land by "
            f"<b>{long_date(last.get('p85'))}</b>"
            + (f"; at today's spread-out pace the last would take until {long_date(slowest)}" if slowest else "")
            + (f", and {escape(', '.join(no_pace))} has had no progress at all" if no_pace else "")
            + "."
        )
    if stuck:
        in_plan_stuck = [i for i in stuck if i["epic"] in in_plan]
        n_stale = sum(i["risk"] == "stale" for i in stuck)
        bullets.append(
            f"<b>{len(stuck)} items in progress look stuck</b>: older than 85% of what the team finishes"
            + (f", and {n_stale} older than 95%." if n_stale else ".")
            + (f" {len(in_plan_stuck)} of them are in planned epics ({escape(', '.join(i['key'] for i in in_plan_stuck[:4]))})." if in_plan_stuck else "")
        )
    if done_but_open:
        bullets.append(f"{len(done_but_open)} epics are marked Done in Jira but still have open work.")
    if fc.get("warnings"):
        bullets += [escape(w[0].upper() + w[1:]) + "." for w in fc["warnings"] if "created at least as fast" not in w]
    summary = (
        '<section class="summary" aria-labelledby="s-summary"><h2 id="s-summary">The short version</h2><ul>'
        + "".join(f"<li>{b}</li>" for b in bullets)
        + f'</ul><p class="basis">Forecasts replay randomly chosen days from the last {h["days"]} days of history '
        f"{fc['runs']:,} times. They assume items are roughly similar in size and that the coming months look like the "
        "last few. Lead times count from when an item was created.</p></section>"
    )

    # --- tiles ---------------------------------------------------------------------
    tiles = [
        ("Backlog", "Shrinking" if shrinking else "Not shrinking", f"{per_week(done_wk)} finished vs {per_week(new_wk)} created a week"),
        ("If nothing new is added", long_date(pcts["p85"]), "85% confidence"),
    ]
    if target and chance is not None:
        tiles.append((f"Chance done by {long_date(target)}", pct(chance), "if nothing new is added"))
    tiles.append(("Stuck in progress", str(len(stuck)), "older than 85% of finished items"))
    tile_html = '<div class="tiles">' + "".join(
        f'<div class="tile"><div class="label">{escape(a)}</div><div class="value">{escape(b)}</div><div class="note">{escape(c)}</div></div>'
        for a, b, c in tiles
    ) + "</div>"

    sections = [summary]

    # --- when will today's backlog be done ------------------------------------------
    pct_line = "".join(f"<span>{p}% by <b>{long_date(pcts[f'p{p}'])}</b></span>" for p in (50, 70, 85, 95))
    weekly_chances = cdf(samples, (_d(pcts["p95"]) - start).days + 14)
    chance_rows = [[long_date(start + timedelta(days=dd)), f"{weekly_chances[dd]:.0%}"] for dd in range(7, len(weekly_chances), 7)]
    caveat = (
        "" if shrinking else
        f'<p class="callout">This assumes nothing new is added. At the current rate ({per_week(new_wk)} created, '
        f"{per_week(done_wk)} finished a week) the backlog won't shrink, so treat these dates as a best case.</p>"
    )
    sections.append(
        f"""<section aria-labelledby="s-when"><h2 id="s-when">When will today's backlog be done?</h2>
<p class="answer">85% likely by <b>{long_date(pcts['p85'])}</b> for the {fc['items']} open items, if nothing new is added.</p>
{caveat}
<div class="chart-head"><span class="lede" style="margin:0">Hover the chart for the chance of being done by any date.</span>
<div class="toggle" role="group" aria-label="Chart view"><button type="button" data-view="pdf" aria-pressed="true">How likely each date is</button><button type="button" data-view="cdf" aria-pressed="false">Chance done by date</button></div></div>
<div data-pane="pdf">{responsive(density_chart, samples, start, pcts, target)}</div>
<div data-pane="cdf" hidden>{responsive(cumulative_chart, samples, start, pcts, target)}</div>
<p class="pcts">{pct_line}</p>
<details><summary>Chance of being done, week by week</summary>{table(["Date", "Chance done by then"], chance_rows, {1})}</details>
</section>"""
    )

    # --- is the backlog shrinking --------------------------------------------------
    if len(weeks) >= 2:
        trend_txt = ""
        if trend:
            trend_txt = (
                f" Over the last 4 weeks: {per_week(trend[0])} finished and {per_week(trend[1])} created a week "
                f"(before that: {per_week(trend[2])} and {per_week(trend[3])})."
            )
        legend = (
            '<div class="legend"><span><i class="key-line" style="background:var(--done)"></i>Finished</span>'
            '<span><i class="key-line" style="background:var(--new)"></i>Created</span>'
            "<span>Lines: 4-week average · dots: each week</span></div>"
        )
        week_rows = [[long_date(wk["week"]), str(wk["completed"]), str(wk["created"])] for wk in weeks]
        sections.append(
            f"""<section aria-labelledby="s-flow"><h2 id="s-flow">Is the backlog shrinking?</h2>
<p class="answer">{"Yes" if shrinking else "No"}: the team finished {per_week(done_wk)} and took on {per_week(new_wk)} items a week over the last {h['days']} days.{trend_txt}</p>
{legend}{responsive(weekly_chart, weeks)}
<details><summary>Weekly numbers</summary>{table(["Week of", "Finished", "Created"], week_rows, {1, 2})}</details>
</section>"""
        )

    # --- epics -------------------------------------------------------------------------
    target_ep = ep.get("target_date")
    if plan:
        legend = (
            '<div class="legend"><span><i class="key-dot" style="background:var(--plan)"></i>In the plan</span>'
            '<span><i class="key-dot" style="background:var(--pace)"></i>At current pace</span>'
            "<span>Each dot: 85% confidence finish date</span></div>"
        )
        rows = []
        for e in plan:
            pr, cp = e.get("priority") or {}, e.get("current_pace")
            row = [escape(e["epic"]), str(e["open"]), f"<b>{long_date(pr.get('p85'))}</b>",
                   (long_date(cp["p85"]) if cp and cp["p85"] else "later") if cp else "no recent progress"]
            if target_ep:
                row.append(pct((e.get("chance") or {}).get("priority")))
            rows.append(row)
        head = ["Epic", "Open items", "In the plan (85%)", "At current pace (85%)"] + ([f"Chance by {long_date(target_ep)}"] if target_ep else [])
        sections.append(
            f"""<section aria-labelledby="s-plan"><h2 id="s-plan">When will the planned epics land?</h2>
<p class="answer">Worked one at a time in this order, the last lands by <b>{long_date((plan[-1].get('priority') or {}).get('p85'))}</b> (85% confidence).</p>
<p class="lede">"In the plan" assumes the team works these epics in order, {prio['wip']} at a time, spending {prio['epic_share']:.0%} of its
capacity on epic work as it has recently, with other epics paused. "At current pace" assumes effort stays spread as it is today.</p>
{legend}{responsive(epic_chart, plan, _d(ep['start']), True, max_days)}
{table(head, rows, {1, 4})}
</section>"""
        )
    if open_epics:
        rows = [
            [escape(e["epic"]), str(e["open"]), str(e["completed_in_window"]),
             (long_date(e["current_pace"]["p85"]) if e["current_pace"]["p85"] else "later") if e["current_pace"] else "no recent progress"]
            for e in open_epics
        ]
        title = "When will the other open epics land?" if plan else "When will each open epic land?"
        sections.append(
            f"""<section aria-labelledby="s-epics"><h2 id="s-epics">{title}</h2>
<p class="lede">At current pace: each epic's own recent completions, replayed forward.</p>
{responsive(epic_chart, open_epics, _d(ep['start']), False, max_days)}
{table(["Epic", "Open items", f"Finished in last {h['days']} days", "At current pace (85%)"], rows, {1, 2})}
</section>"""
        )

    # --- what looks stuck --------------------------------------------------------------
    if ag["items"]:
        types = sorted({i["type"] or "?" for i in ag["items"]}, key=lambda t: -sum((i["type"] or "?") == t for i in ag["items"]))
        starred = any(i["basis"] != i["type"] for i in ag["items"])
        legend = (
            '<div class="legend">'
            '<span><i class="key-dot" style="background:var(--ink-2)"></i>In progress</span>'
            '<span><i class="key-dot" style="border:1.5px solid var(--ink-2)"></i>To do</span>'
            '<span><i class="key-zone" style="background:color-mix(in srgb, var(--warning) 35%, transparent)"></i>Older than 85% of finished items</span>'
            '<span><i class="key-zone" style="background:color-mix(in srgb, var(--critical) 25%, transparent)"></i>Older than 95%</span></div>'
        )

        def stuck_row(i):
            return [escape(i["key"]), escape(i["type"] or "?"), epic_link(i["epic"]), str(i["age_days"]),
                    f"{i['lead_time_p85']}", badge(i["risk"])]

        head = ["Item", "Type", "Epic", "Age (days)", "Usually done within (days)", "Risk"]
        top_rows = [stuck_row(i) for i in stuck[:10]]
        rest = [stuck_row(i) for i in flagged if i not in stuck[:10]]
        sections.append(
            f"""<section aria-labelledby="s-stuck"><h2 id="s-stuck">What looks stuck?</h2>
<p class="answer">{len(stuck)} items in progress are older than 85% of what the team finishes{f" ({sum(i['risk'] == 'stale' for i in stuck)} older than 95%)" if stuck else ""}.</p>
<p class="lede">Each dot is an open item, placed by age. Items in the shaded zones are older than nearly everything the team
finishes of that type. Filled dots (in progress) there are the likeliest to be stuck; hollow ones (to do) are usually just waiting.</p>
{legend}{responsive(aging_chart, ag["items"], types)}
{'<p class="note">* Too few recent completions of this type; compared with all types.</p>' if starred else ''}
{'<p class="lede" style="margin:16px 0 0"><b>Oldest items in progress</b></p>' + table(head, top_rows, {3, 4}, {1}) if top_rows else ''}
{f'<details><summary>All {len(flagged)} flagged items, including to do</summary>{table(head, rest, {3, 4}, {1})}</details>' if rest else ''}
</section>"""
        )

    # --- clean-up ------------------------------------------------------------------------
    cleanup = []
    if done_but_open:
        cleanup.append(
            f"<b>{len(done_but_open)} epics are marked Done but still have open items:</b> "
            + escape(", ".join(f"{e['epic']} ({e['open']})" for e in done_but_open))
            + ". Reopen them or move the items; until then this work sits outside any plan."
        )
    if stale_todo:
        cleanup.append(
            f"<b>{len(stale_todo)} to-do items are older than 95% of what the team finishes</b> "
            f"(oldest: {escape(', '.join(i['key'] for i in stale_todo[:5]))}). Close or re-scope them: they inflate every backlog forecast."
        )
    if ep.get("open_without_epic"):
        cleanup.append(f"{ep['open_without_epic']} open items have no epic.")
    if cleanup:
        sections.append(
            '<section aria-labelledby="s-clean"><h2 id="s-clean">What needs tidying in Jira?</h2><ul class="plain">'
            + "".join(f"<li>{c}</li>" for c in cleanup)
            + "</ul></section>"
        )

    # --- track record --------------------------------------------------------------------
    resolved = {m: {p: s for p, s in by_p.items() if s["held"] + s["missed"]} for m, by_p in cal["summary"].items()}
    resolved = {m: v for m, v in resolved.items() if v}
    labels = {"when/no_growth": "Backlog finish date", "when/scope_growth": "Backlog finish date (with new work)",
              "how_many": "Items by a date", "epic/current_pace": "Epic finish date"}
    if resolved:
        rows = [
            [escape(labels.get(m, str(m))), p[1:] + "%", f"{s['held'] / (s['held'] + s['missed']):.0%}", f"{s['held']} of {s['held'] + s['missed']}"]
            for m, by_p in resolved.items() for p, s in by_p.items()
        ]
        sections.append(
            '<section aria-labelledby="s-cal"><h2 id="s-cal">How accurate have past forecasts been?</h2>'
            '<p class="lede">A well-calibrated 85% answer should hold about 85% of the time.</p>'
            + table(["Forecast", "Confidence", "Held", "Forecasts"], rows, {2, 3}) + "</section>"
        )
        track = ""
    else:
        track = "No past forecast for this scope has reached its date yet, so there is no track record. "

    synced = ""
    if d.get("last_sync"):
        ls = datetime.fromisoformat(d["last_sync"])
        synced = f" · synced {long_date(ls.date())} {ls:%H:%M}"
    jql = f'<code>{escape(d["jql"])}</code> · ' if d.get("jql") else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(scope)} delivery forecast</title><style>{CSS}</style></head>
<body><div class="mc"><main>
<h1>{escape(scope)} delivery forecast</h1>
<p class="meta">{jql}excluding epics and sub-tasks · history {long_date(h['start'])} – {long_date(h['end'])}{synced}</p>
{tile_html}
{"".join(sections)}
<footer>{track}Generated by mcmc from Jira data. Nothing on this page is sent anywhere.</footer>
</main><div id="tip" role="tooltip"></div></div><script>{JS}</script></body></html>
"""


def render_index(rows: list[dict], root) -> str:
    """Index of saved reports, newest first, grouped by scope, linking by relative path."""
    import os

    by_scope: dict[str, list[dict]] = {}
    for row in rows:
        by_scope.setdefault(row["scope"], []).append(row)
    sections = []
    for scope, items in sorted(by_scope.items()):
        body = []
        for i, row in enumerate(items):
            href = os.path.relpath(row["path"], root)
            outlook = "shrinking" if row["shrinking"] else "not shrinking"
            chance = f"{row['chance']:.0%} by {long_date(row['target_date'])}" if row["chance"] is not None else "—"
            body.append([
                f'<a href="{escape(href, quote=True)}">{escape(long_date(row["created_at"].date()))} {row["created_at"]:%H:%M}</a>'
                + (" <b>latest</b>" if i == 0 else ""),
                long_date(row["as_of"]), str(row["open_items"]), f"<b>{long_date(row['p85'])}</b>", outlook, chance,
            ])
        sections.append(
            f'<section><h2>{escape(scope)}</h2>'
            + table(["Report", "Data as of", "Open items", "85% done by", "Backlog", "Chance by target"], body, {2}, {4, 5})
            + "</section>"
        )
    content = "".join(sections) or '<p class="lede">No reports yet.</p>'
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Delivery forecast reports</title><style>{CSS}
.mc a {{ color: var(--ink); }}</style></head>
<body><div class="mc"><main>
<h1>Delivery forecast reports</h1>
<p class="meta">Saved locally, newest first. Watching the "85% done by" date across reports shows whether delivery is
slipping or holding.</p>
{content}
</main></div></body></html>
"""
