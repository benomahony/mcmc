"""Stylesheet and script for the HTML report: one self-contained page, no network.

Colour carries one meaning across the page: blue = work finishing (and the forecast built
from it), orange = new work arriving, aqua = the epic plan, grey = current pace, and the
status colours only for stuck work.
"""

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
