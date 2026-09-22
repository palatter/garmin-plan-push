/* review.js — the review screen and everything that hangs off it: session
   cards and the calendar, editing with per-step cues, the watch preview, the
   health / sanity / changes panels, adaptation, export, the library and the
   command palette. Loaded before app.js; the two share one global scope. */

'use strict';

const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const KIND_LABEL = {
  warmup: 'Warm up', run: 'Run', recover: 'Recover', rest: 'Rest',
  cooldown: 'Cool down', stride: 'Stride', exercise: 'Exercise', repeat: 'Repeat',
};

/* ------------------------------------------------------------- helpers --- */

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null || value === false) continue;
    if (key === 'class') node.className = value;
    else node.setAttribute(key, value === true ? '' : value);
  }
  node.append(...children.filter(c => c != null));
  return node;
}

function fmtDuration(seconds) {
  const s = Math.round(seconds);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h}h ${String(m).padStart(2, '0')}m` : `${m}m`;
}

function fmtDistance(metres) {
  if (!metres) return '—';
  return state.units === 'imperial'
    ? `${(metres / 1609.344).toFixed(1)} mi`
    : `${(metres / 1000).toFixed(1)} km`;
}

function fmtKm(km) {
  if (!km) return '—';
  return state.units === 'imperial' ? `${(km / 1.609344).toFixed(1)} mi` : `${Math.round(km * 10) / 10} km`;
}

function dateOf(iso) { return new Date(`${iso}T00:00:00`); }
function fmtDate(iso) {
  const d = dateOf(iso);
  return `${WEEKDAYS[d.getDay()]} ${d.getDate()} ${MONTHS[d.getMonth()]}`;
}
function isoOf(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}
function isoAdd(iso, days) { const d = dateOf(iso); d.setDate(d.getDate() + days); return isoOf(d); }
function todayIso() { return isoOf(new Date()); }
function mondayOf(iso) { const d = dateOf(iso); d.setDate(d.getDate() - ((d.getDay() + 6) % 7)); return isoOf(d); }
function nextMonday() { return isoAdd(mondayOf(todayIso()), 7); }

function downloadText(filename, text, mime = 'application/json') {
  const blob = new Blob([text], { type: mime });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  URL.revokeObjectURL(a.href);
}

async function copyText(text, okSel) {
  await navigator.clipboard.writeText(text);
  if (okSel) {
    $(okSel).hidden = false;
    setTimeout(() => { $(okSel).hidden = true; }, 2200);
  }
}

/* --------------------------------------------------------------- toast --- */

let toastTimer;
function toast(message, action = null, bad = false) {
  const box = $('#toast');
  $('#toast-text').textContent = message;
  box.classList.toggle('is-bad', bad);
  const btn = $('#toast-action');
  if (action) {
    btn.textContent = action.label;
    btn.hidden = false;
    btn.onclick = () => { hideToast(); action.run(); };
  } else {
    btn.hidden = true;
    btn.onclick = null;
  }
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(hideToast, bad ? 9000 : 6000);
}
function hideToast() { $('#toast').hidden = true; }

/* ---------------------------------------------------------- plan state --- */
/* state.plan is the server's description of the current plan; state.plan.json
   is the plan itself and the only thing we ever edit. Every edit goes back
   through /api/preview so the numbers on screen are the compiler's, never a
   JavaScript approximation, and the sanity report re-runs on each change. */

function openPlan(described, fresh, label) {
  state.plan = described;
  if (fresh) {
    state.baseline = structuredClone(described.json);
    state.baselineLabel = label || 'generated';
    state.history = [];
    state.lastAdaptation = null;
  }
  renderReview(described);
  show('review');
  loadRecaps(described);
}

async function applyPlan(json) {
  const described = await api('/api/preview', { plan: json });
  state.plan = described;
  renderReview(described);
  return described;
}

async function mutate(change) {
  const before = structuredClone(state.plan.json);
  const next = structuredClone(before);
  change(next);
  state.history.push(before);
  try {
    return await applyPlan(next);
  } catch (err) {
    state.history.pop();
    throw err;
  }
}

async function undo() {
  const previous = state.history.pop();
  if (!previous) return;
  state.lastAdaptation = null;
  try {
    await applyPlan(previous);
    toast('Undone.');
  } catch (err) {
    toast(err.message, null, true);
  }
}

async function resetToBaseline() {
  if (!state.baseline) return;
  state.history.push(structuredClone(state.plan.json));
  state.lastAdaptation = null;
  try {
    await applyPlan(structuredClone(state.baseline));
    toast(`Back to the plan as ${state.baselineLabel}.`);
  } catch (err) {
    toast(err.message, null, true);
  }
}

function newFindings(before, after) {
  const seen = new Set((before.findings || []).map(f => f.message));
  return (after.findings || []).filter(f => !seen.has(f.message));
}

function sortedIndexes(workouts) {
  return workouts.map((w, i) => i)
    .sort((a, b) => workouts[a].date.localeCompare(workouts[b].date) || a - b);
}

function nextIndex() {
  const today = todayIso();
  const order = sortedIndexes(state.plan.workouts);
  return order.find(i => state.plan.workouts[i].date >= today) ?? order[0] ?? 0;
}

/* -------------------------------------------------------------- review --- */

function renderLegend() {
  const names = [
    ['Recovery', 0.08], ['Easy', 0.24], ['Steady', 0.42],
    ['Marathon', 0.56], ['Threshold', 0.72], ['Interval', 0.88], ['Reps', 1.0],
  ];
  const host = $('#r-legend');
  host.innerHTML = '';
  for (const [label, t] of names) {
    const dot = el('i');
    dot.style.background = rampColor(t);
    host.append(el('span', {}, dot, label));
  }
}

function renderReview(data) {
  $('#r-title').textContent = data.plan;
  const total = data.workouts.reduce((a, w) => a + w.seconds, 0);
  const dist = data.workouts.reduce((a, w) => a + (w.summary.metres || 0), 0);
  let summary = `${data.workouts.length} sessions · about ${fmtDuration(total)} of running · ${fmtDistance(dist)}`;
  if (data.race && data.race.date) summary += ` · ${data.race.name || 'race'} on ${fmtDate(data.race.date)}`;
  $('#r-summary').textContent = summary;

  renderLegend();
  renderHealth(data.dashboard);
  renderFocus(data.load_focus);
  renderWeek(data.agenda);
  renderSanity(data.report);
  renderChanges();
  renderRationale(data);
  renderMileage(data);

  const list = $('#r-workouts'), cal = $('#r-calendar');
  if (state.view === 'calendar') {
    list.hidden = true;
    cal.hidden = false;
    renderCalendar(data);
  } else {
    cal.hidden = true;
    list.hidden = false;
    list.innerHTML = '';
    sortedIndexes(data.workouts).forEach((i, n) => list.append(workoutCard(data.workouts[i], i, n)));
  }
  $('#r-push').classList.toggle('has-blocks', data.report.blocks > 0);
}

function setView(view) {
  state.view = view;
  $$('.seg[data-view]').forEach(b => {
    const on = b.dataset.view === view;
    b.classList.toggle('is-active', on);
    b.setAttribute('aria-checked', String(on));
    b.tabIndex = on ? 0 : -1;
  });
  if (state.plan) renderReview(state.plan);
}

/* --- panels --- */

function renderHealth(d) {
  const host = $('#health-body');
  host.innerHTML = '';
  if (!d || !d.weeks || !d.weeks.length) { $('#health-totals').textContent = ''; return; }
  $('#health-totals').textContent =
    `${fmtKm(d.total_km)} · ${fmtDuration(d.total_minutes * 60)} · ${Math.round(d.hard_share * 100)}% hard` +
    ` · peak week ${fmtKm(d.peak_week_km)} · longest run ${fmtKm(d.longest_run_km)}`;
  const size = w => w.km || w.minutes / 10;
  const peak = Math.max(...d.weeks.map(size), 1);
  d.weeks.forEach((w, i) => {
    const title = `Week of ${fmtDate(w.start)}: ${w.sessions} sessions, ${fmtKm(w.km)}, ` +
      `${fmtDuration(w.minutes * 60)}, ${Math.round(w.hard_share * 100)}% hard, long run ${fmtKm(w.longest_run_km)}`;
    const col = el('div', { class: 'hw', title });
    const fill = el('div', { class: 'hw-fill' });
    fill.style.height = `${Math.max(4, (size(w) / peak) * 100)}%`;
    fill.style.background = rampColor(Math.min(1, w.hard_share * 2.5));
    const flags = [];
    if (w.hard_share > 0.3) flags.push(el('span', { class: 'flag' }, 'hard'));
    if (w.monotony != null && w.monotony > 2) flags.push(el('span', { class: 'flag' }, 'flat'));
    col.append(
      el('div', { class: 'hw-bar' }, fill),
      el('span', { class: 'hw-week' }, `W${i + 1}`),
      el('span', { class: 'hw-val' }, w.km ? fmtKm(w.km) : fmtDuration(w.minutes * 60)),
      el('span', { class: 'hw-sub' }, `${Math.round(w.hard_share * 100)}% hard`),
      ...flags,
    );
    host.append(col);
  });
}

function renderSanity(r) {
  const host = $('#sanity-body');
  host.innerHTML = '';
  const badge = $('#sanity-badge');
  badge.textContent = r.blocks ? `${r.blocks} blocking`
    : r.warns ? `${r.warns} warning${r.warns > 1 ? 's' : ''}` : 'Nothing to flag';
  badge.className = `badge ${r.blocks ? 'is-block' : r.warns ? 'is-warn' : 'is-ok'}`;
  if (!r.findings.length) {
    host.append(el('li', { class: 'hint' },
      'Ramp, rest days, hard days, taper and the long run all look sensible.'));
    return;
  }
  for (const f of r.findings) {
    const li = el('li', { class: `finding ${f.severity}` },
      el('span', { class: 'sev' }, f.severity), el('span', { class: 'msg' }, f.message));
    if (f.dates && f.dates.length) {
      const dates = el('span', { class: 'f-dates' });
      for (const d of f.dates) {
        // A finding may say "2026-10-04 Long run (18.4 km vs 16.6 km)": the
        // date is the anchor, the rest is the label.
        const m = /^(\d{4}-\d{2}-\d{2})\s*(.*)$/.exec(d);
        const label = m ? fmtDate(m[1]) + (m[2] ? ` · ${m[2]}` : '') : d;
        const b = el('button', { type: 'button', class: 'chip sm' }, label);
        b.addEventListener('click', () => focusDate(m ? m[1] : d));
        dates.append(b);
      }
      li.append(dates);
    }
    host.append(li);
  }
}

function focusDate(iso) {
  const target = document.querySelector(`.workout[data-date="${iso}"], .cal-day[data-date="${iso}"]`);
  if (!target) return;
  target.scrollIntoView({ behavior: 'smooth', block: 'center' });
  target.classList.add('is-flash');
  setTimeout(() => target.classList.remove('is-flash'), 1800);
}

let changesSeq = 0;
async function renderChanges() {
  const panel = $('#panel-changes');
  const host = $('#changes-body');
  if (!state.baseline || !state.plan) { panel.hidden = true; return; }
  panel.hidden = false;
  $('#changes-label').textContent = `since ${state.baselineLabel}`;
  $('#changes-undo').disabled = !state.history.length;
  const seq = ++changesSeq;
  let diff;
  try {
    diff = await api('/api/diff', { before: state.baseline, after: state.plan.json });
  } catch (err) {
    host.replaceChildren(el('li', { class: 'hint' }, err.message));
    return;
  }
  if (seq !== changesSeq) return;                       // a newer render won
  host.innerHTML = '';
  const reasons = (state.lastAdaptation && state.lastAdaptation.reasons) || [];
  for (const r of reasons) {
    host.append(el('li', { class: 'change reason' }, el('span', { class: 'mark' }, '→'), el('span', { class: 'key' }, r)));
  }
  const items = [...diff.meta.map(m => ({ kind: 'meta', key: m, details: [] })), ...diff.changes];
  $('#changes-reset').disabled = !items.length;
  if (!items.length && !reasons.length) {
    host.append(el('li', { class: 'hint' }, 'No changes yet. Drag a session, edit one, or mark one missed.'));
    return;
  }
  const marks = { added: '+', removed: '−', changed: '~', meta: '•' };
  for (const c of items) {
    const li = el('li', { class: `change ${c.kind}` },
      el('span', { class: 'mark' }, marks[c.kind] || '•'), el('span', { class: 'key' }, c.key));
    if (c.details && c.details.length) {
      li.append(el('ul', { class: 'details' }, ...c.details.map(d => el('li', {}, d))));
    }
    host.append(li);
  }
}

/* --- cards --- */

/* Collapse the expanded timeline back into a spoken description, folding
   consecutive repeats: "15:00 warm up, then 4 times (8:00 threshold, 2:00
   recovery), then 10:00 cool down". */
function describeSession(w) {
  const parts = [];
  let i = 0;
  while (i < w.timeline.length) {
    const b = w.timeline[i];
    if (b.of && b.of > 1) {
      const groupSize = w.timeline.slice(i).findIndex(x => x.rep !== b.rep);
      const size = groupSize === -1 ? w.timeline.length - i : groupSize;
      const inner = w.timeline.slice(i, i + size)
        .map(x => `${x.extent} ${KIND_LABEL[x.kind] || x.kind}, ${x.target}`).join('; ');
      parts.push(`${b.of} times: ${inner}`);
      i += size * b.of;                       // skip the other repetitions
    } else {
      parts.push(`${b.extent} ${KIND_LABEL[b.kind] || b.kind}, ${b.target}`);
      i += 1;
    }
  }
  return `Session profile. ${parts.join('. Then ')}.`;
}

function workoutCard(w, index, position) {
  const card = el('article', { class: 'workout', 'data-date': w.date, 'data-index': String(index) });
  card.style.animationDelay = `${Math.min(position * 45, 300)}ms`;

  const date = dateOf(w.date);
  const head = el('div', { class: 'w-head' });
  head.innerHTML = `
    <div class="w-date">
      <span class="dow">${WEEKDAYS[date.getDay()]}</span>
      <span class="dom">${date.getDate()}</span>
    </div>
    <div>
      <div class="w-name"></div>
      <div class="w-note"></div>
    </div>
    <div class="w-stats">
      <div class="w-stat"><span class="v">${fmtDuration(w.seconds)}</span><span class="k">Time</span></div>
      <div class="w-stat"><span class="v">${fmtDistance(w.summary.metres)}</span><span class="k">Distance</span></div>
      <div class="w-stat" title="Session load: minutes weighted by intensity (a TRIMP-style number, never a ratio)"><span class="v">${w.load ?? '—'}</span><span class="k">Load</span></div>
    </div>`;
  $('.w-name', head).textContent = w.name;
  if (w.notes) $('.w-note', head).textContent = w.notes;
  else $('.w-note', head).remove();

  const bar = el('div', { class: 'profile-bar', role: 'img', 'aria-label': describeSession(w) });
  const totalSeconds = w.timeline.reduce((a, b) => a + b.seconds, 0) || 1;
  for (const block of w.timeline) {
    const seg = el('div', { class: 'seg-block' + (block.open_ended ? ' is-open' : '') });
    seg.style.flex = `${(block.seconds / totalSeconds) * 100} 0 0`;
    seg.style.background = rampColor(block.intensity);
    seg.style.height = `${28 + block.intensity * 72}%`;
    const rep = block.rep ? ` (rep ${block.rep}/${block.of})` : '';
    seg.title = `${KIND_LABEL[block.kind] || block.kind}${rep} · ${block.extent} · ${block.target}`;
    bar.append(seg);
  }

  const steps = el('details', { class: 'w-steps' });
  const tbody = el('tbody');
  for (const b of w.timeline) {
    const dot = el('i', { class: 'dot' });
    dot.style.background = rampColor(b.intensity);
    const kind = el('span', { class: 'kind' }, dot, KIND_LABEL[b.kind] || b.kind);
    if (b.rep) kind.append(' ', el('span', { class: 'muted' }, `${b.rep}/${b.of}`));
    const tr = el('tr', {}, el('td', {}, b.extent), el('td', {}, kind), el('td', { class: 'tgt' }, b.target));
    if (b.note) tr.append(el('td', { class: 'cue' }, `“${b.note}”`));
    tbody.append(tr);
  }
  const notes = [];
  if (w.summary.open_ended) notes.push('includes a lap-button step');
  if (w.summary.truncated) notes.push('chart shows the first ' + w.timeline.length);
  steps.append(
    el('summary', {}, `${w.timeline.length} step${w.timeline.length === 1 ? '' : 's'}${notes.length ? ' · ' + notes.join(' · ') : ''}`),
    el('table', {}, tbody),
  );

  const actions = el('div', { class: 'w-actions' });
  const action = (label, run, title) => {
    const b = el('button', { type: 'button', class: 'btn sm ghost', title }, label);
    b.addEventListener('click', run);
    return b;
  };
  actions.append(
    action('Edit', () => openEdit(index), 'Change the date, steps or cues'),
    action('Move…', () => openMove(index), 'Move to another day without dragging'),
    action('On the watch', () => openWatch(index), 'Preview the step screens'),
    action('Missed', () => openMissed(index), 'Readapt around it, or keep the plan'),
    action('Rewrite', () => openRewrite(index), 'Let the model rewrite just this session'),
    action('Save', () => saveWorkoutToLibrary(index), 'Keep this session in your library'),
  );

  card.append(head, bar, steps, cardExtras(w, index), actions);
  return card;
}

/* --- calendar --- */

function renderCalendar(data) {
  const host = $('#r-calendar');
  host.innerHTML = '';
  if (!data.workouts.length) { host.append(el('p', { class: 'empty' }, 'Nothing on the calendar.')); return; }
  const dates = data.workouts.map(w => w.date);
  if (data.race && data.race.date) dates.push(data.race.date);
  const first = dates.reduce((a, b) => (a < b ? a : b));
  const last = dates.reduce((a, b) => (a > b ? a : b));
  const start = mondayOf(first);
  const end = isoAdd(mondayOf(last), 6);
  const byDate = {};
  data.workouts.forEach((w, i) => { (byDate[w.date] ||= []).push({ w, i }); });
  const weekStats = Object.fromEntries((data.dashboard.weeks || []).map(w => [w.start, w]));
  const today = todayIso();

  const grid = el('div', { class: 'cal', role: 'grid', 'aria-label': 'Plan calendar' });
  const head = el('div', { class: 'cal-head', role: 'row' });
  for (const d of ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun', 'Week']) head.append(el('div', { role: 'columnheader' }, d));
  grid.append(head);

  let week = 1;
  for (let d = start; d <= end; d = isoAdd(d, 7)) {
    const row = el('div', { class: 'cal-row', role: 'row' });
    for (let k = 0; k < 7; k++) {
      const iso = isoAdd(d, k);
      const cell = el('div', { class: 'cal-day', role: 'gridcell', 'data-date': iso });
      if (iso === today) cell.classList.add('is-today');
      if (iso < today) cell.classList.add('is-past');
      const dt = dateOf(iso);
      const label = dt.getDate() === 1 || (d === start && k === 0) ? `${MONTHS[dt.getMonth()]} ${dt.getDate()}` : String(dt.getDate());
      cell.append(el('div', { class: 'cal-date' }, label));
      if (data.race && data.race.date === iso) cell.append(el('div', { class: 'cal-race' }, `🏁 ${data.race.name || 'Race'}`));
      for (const { w, i } of byDate[iso] || []) cell.append(calChip(w, i));
      cell.addEventListener('dragover', e => { e.preventDefault(); cell.classList.add('is-over'); });
      cell.addEventListener('dragleave', () => cell.classList.remove('is-over'));
      cell.addEventListener('drop', e => {
        e.preventDefault();
        cell.classList.remove('is-over');
        const i = Number(e.dataTransfer.getData('text/plain'));
        if (Number.isInteger(i)) moveWorkout(i, iso);
      });
      row.append(cell);
    }
    const ws = weekStats[d];
    const sum = el('div', { class: 'cal-week', role: 'gridcell' }, el('strong', {}, `W${week++}`));
    if (ws) {
      sum.append(
        el('span', {}, ws.km ? fmtKm(ws.km) : fmtDuration(ws.minutes * 60)),
        el('span', {}, `${ws.sessions} session${ws.sessions === 1 ? '' : 's'}`),
        el('span', { class: ws.hard_share > 0.3 ? 'is-warn' : '' }, `${Math.round(ws.hard_share * 100)}% hard`),
      );
    }
    row.append(sum);
    grid.append(row);
  }
  host.append(grid);
}

function calChip(w, index) {
  const chip = el('button', {
    type: 'button', class: 'cal-chip', draggable: 'true', 'data-index': String(index),
    title: `${w.name} · ${fmtDuration(w.seconds)}${w.summary.metres ? ' · ' + fmtDistance(w.summary.metres) : ''} — drag to move, Enter to edit`,
  });
  const share = w.seconds ? w.hard_seconds / w.seconds : 0;
  chip.style.setProperty('--chip', rampColor(Math.min(1, share * 2.2)));
  chip.append(el('span', { class: 'cc-name' }, w.name), el('span', { class: 'cc-meta' }, fmtDuration(w.seconds)));
  chip.addEventListener('dragstart', e => {
    e.dataTransfer.setData('text/plain', String(index));
    e.dataTransfer.effectAllowed = 'move';
    chip.classList.add('is-dragging');
  });
  chip.addEventListener('dragend', () => chip.classList.remove('is-dragging'));
  chip.addEventListener('click', () => openEdit(index));
  chip.addEventListener('keydown', e => {
    const delta = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 }[e.key];
    if (!delta) return;
    e.preventDefault();
    moveWorkout(index, isoAdd(w.date, delta));
  });
  return chip;
}

async function moveWorkout(index, iso) {
  const w = state.plan.json.workouts[index];
  if (!w || w.date === iso) return;
  const before = state.plan.report;
  try {
    await mutate(p => { p.workouts[index].date = iso; });
  } catch (err) {
    toast(err.message, null, true);
    return;
  }
  const fresh = newFindings(before, state.plan.report);
  const message = `Moved ${w.name} to ${fmtDate(iso)}.` + (fresh.length ? ` New: ${fresh[0].message}` : '');
  toast(message, { label: 'Undo', run: undo }, fresh.some(f => f.severity === 'block'));
  const chip = $(`.cal-chip[data-index="${index}"]`);
  if (chip) chip.focus();
}

/* ---------------------------------------------------------------- edit --- */

const edit = { index: null, steps: [], jsonDirty: false };

function stepExtent(s) {
  if (s.duration != null) return typeof s.duration === 'number' ? fmtDuration(s.duration) : s.duration;
  if (s.distance != null) return typeof s.distance === 'number' ? fmtDistance(s.distance) : s.distance;
  if (s.until) return 'lap button';
  if (s.count) return `${s.count} reps`;
  return '';
}

function stepTarget(t) {
  if (!t || !t.type || t.type === 'none') return 'no target';
  if (t.zone) return `${t.type} ${t.zone}`;
  if (t.type === 'pace') return `${t.slow}–${t.fast}`;
  if (t.type === 'hr') return `HR ${t.low}–${t.high}`;
  if (t.type === 'power') return `${t.low}–${t.high} W`;
  if (t.type === 'cadence') return `${t.low}–${t.high} spm`;
  if (t.type === 'rpe') return `RPE ${t.value}`;
  return t.type;
}

function flattenSteps(steps) {
  const out = [];
  steps.forEach((s, i) => {
    if (s.kind === 'repeat') {
      out.push({ path: [i], step: s, group: true });
      (s.steps || []).forEach((c, j) => out.push({ path: [i, j], step: c }));
    } else {
      out.push({ path: [i], step: s });
    }
  });
  return out;
}

function stepAt(steps, path) {
  return path.length === 1 ? steps[path[0]] : steps[path[0]].steps[path[1]];
}

function openEdit(index) {
  const w = state.plan.json.workouts[index];
  edit.index = index;
  edit.steps = structuredClone(w.steps);
  $('#e-name').value = w.name;
  $('#e-date').value = w.date;
  $('#e-role').value = w.role || '';
  $('#e-notes').value = w.notes || '';
  $('#e-line').value = '';
  $('#e-line-note').textContent = '';
  renderCueTable();
  refreshJson();
  selectEtab('cues');
  setError('#edit-error', '');
  renderWhy('#e-why', state.plan.json.workouts[index]);
  $('#edit-dialog').showModal();
}

function renderCueTable() {
  const tbody = $('#e-cues tbody');
  tbody.innerHTML = '';
  for (const { path, step, group } of flattenSteps(edit.steps)) {
    const label = group
      ? `${step.reps} × repeat`
      : (step.exercise || KIND_LABEL[step.kind] || step.kind);
    const tr = el('tr', { class: group ? 'is-group' : path.length === 2 ? 'is-child' : '' },
      el('td', {}, label),
      el('td', {}, group ? '' : stepExtent(step)),
      el('td', { class: 'tgt' }, group ? '' : stepTarget(step.target)));
    const input = el('input', { type: 'text', maxlength: '60', placeholder: 'cue', 'aria-label': `Cue for ${label}` });
    input.value = step.note || '';
    input.addEventListener('input', () => {
      const target = stepAt(edit.steps, path);
      if (input.value.trim()) target.note = input.value.trim();
      else delete target.note;
      refreshJson();
    });
    tr.append(el('td', {}, input));
    tbody.append(tr);
  }
}

function refreshJson() {
  $('#e-json').value = JSON.stringify(edit.steps, null, 2);
  edit.jsonDirty = false;
}

function selectEtab(name) {
  $$('.tab[data-etab]').forEach(t => {
    const on = t.dataset.etab === name;
    t.classList.toggle('is-active', on);
    t.setAttribute('aria-selected', String(on));
    t.tabIndex = on ? 0 : -1;
  });
  $$('.etab').forEach(p => { p.hidden = p.dataset.epanel !== name; });
}

async function parseLine() {
  const text = $('#e-line').value.trim();
  if (!text) return;
  try {
    const { workout } = await api('/api/oneline', {
      text, date: $('#e-date').value || todayIso(), name: $('#e-name').value,
    });
    edit.steps = workout.steps;
    renderCueTable();
    refreshJson();
    const n = workout.steps.length;
    $('#e-line-note').textContent = `${n} step${n === 1 ? '' : 's'} — add cues on the first tab, then save.`;
    setError('#edit-error', '');
  } catch (err) {
    setError('#edit-error', err.message);
  }
}

async function saveEdit() {
  const index = edit.index;
  let steps = edit.steps;
  if (edit.jsonDirty) {
    try {
      steps = JSON.parse($('#e-json').value);
      if (!Array.isArray(steps)) throw new Error('steps must be a list');
    } catch (err) {
      setError('#edit-error', `JSON: ${err.message}`);
      return;
    }
  }
  const name = $('#e-name').value.trim(), date = $('#e-date').value;
  if (!name || !date) { setError('#edit-error', 'A session needs a name and a date.'); return; }
  const role = $('#e-role').value.trim(), notes = $('#e-notes').value.trim();
  try {
    await mutate(p => {
      const w = p.workouts[index];
      w.name = name;
      w.date = date;
      if (role) w.role = role; else delete w.role;
      if (notes) w.notes = notes; else delete w.notes;
      w.steps = steps;
    });
  } catch (err) {
    setError('#edit-error', err.message);
    return;
  }
  $('#edit-dialog').close();
  toast(`Saved ${name}.`, { label: 'Undo', run: undo });
}

function initEdit() {
  rovingGroup($$('.tab[data-etab]'), 'aria-selected', tab => selectEtab(tab.dataset.etab));
  $('#e-cancel').addEventListener('click', () => $('#edit-dialog').close());
  $('#e-save').addEventListener('click', saveEdit);
  $('#e-parse').addEventListener('click', parseLine);
  $('#e-json').addEventListener('input', () => { edit.jsonDirty = true; });
  // Enter in a field must not close the dialog (method="dialog" would).
  $('#edit-form').addEventListener('submit', e => {
    e.preventDefault();
    if (document.activeElement === $('#e-line')) parseLine();
    else if (document.activeElement.tagName !== 'TEXTAREA') saveEdit();
  });
}

/* --------------------------------------------------------------- watch --- */

const watch = { index: 0, k: 0 };

function openWatch(index) {
  watch.index = index;
  watch.k = 0;
  renderWatch();
  $('#watch-dialog').showModal();
}

function renderWatch() {
  const w = state.plan.workouts[watch.index];
  const blocks = w.timeline;
  const b = blocks[watch.k];
  $('#wt-title').textContent = w.name;
  $('#wt-step').textContent = `Step ${watch.k + 1} of ${blocks.length}`;
  $('#wt-rep').textContent = b.of ? `${b.rep}/${b.of}` : '';
  $('#wt-label').textContent = (b.kind === 'exercise' && b.label ? b.label : (KIND_LABEL[b.kind] || b.kind)).toUpperCase();
  $('#wt-extent').textContent = b.extent;
  $('#wt-target').textContent = b.target;
  // The workout's "why" line rides on the first step when that step has no cue of its own.
  $('#wt-note').textContent = b.note || (watch.k === 0 && w.notes ? w.notes : '');
  $('#wt-face').style.setProperty('--wt-color', rampColor(b.intensity));
  renderWhy('#wt-why', state.plan.json.workouts[watch.index], 1);
  const list = $('#wt-list');
  list.innerHTML = '';
  blocks.forEach((x, i) => {
    const btn = el('button', { type: 'button' },
      `${x.extent} ${KIND_LABEL[x.kind] || x.kind}${x.of ? ` ${x.rep}/${x.of}` : ''}`);
    btn.addEventListener('click', () => { watch.k = i; renderWatch(); });
    list.append(el('li', { class: i === watch.k ? 'is-current' : '' }, btn));
  });
  $('#wt-prev').disabled = watch.k === 0;
  $('#wt-next').disabled = watch.k >= blocks.length - 1;
}

function initWatch() {
  $('#wt-close').addEventListener('click', () => $('#watch-dialog').close());
  $('#wt-prev').addEventListener('click', () => { watch.k = Math.max(0, watch.k - 1); renderWatch(); });
  $('#wt-next').addEventListener('click', () => {
    watch.k = Math.min(state.plan.workouts[watch.index].timeline.length - 1, watch.k + 1);
    renderWatch();
  });
  $('#watch-dialog').addEventListener('keydown', e => {
    if (e.key === 'ArrowRight') $('#wt-next').click();
    if (e.key === 'ArrowLeft') $('#wt-prev').click();
  });
}

/* --------------------------------------------------------- adaptation --- */

const missed = { index: null };

function openMissed(index) {
  missed.index = index;
  const w = state.plan.workouts[index];
  $('#m-text').textContent = `${w.name}, ${fmtDate(w.date)}.`;
  setError('#missed-error', '');
  $('#missed-dialog').showModal();
}

function openDialog(sel) {
  if (sel === '#pause-dialog') { $('#pa-start').value = todayIso(); setError('#pause-error', ''); }
  if (sel === '#return-dialog') { $('#rt-start').value = todayIso(); setError('#return-error', ''); }
  $(sel).showModal();
}

/* Adaptation is a server rule, never a guess made here: the plan goes up,
   the adapted plan comes back with a reason for every change. */
async function adaptPlan(body, message) {
  const before = structuredClone(state.plan.json);
  const described = await api('/api/adapt', { plan: before, ...body });
  state.history.push(before);
  state.lastAdaptation = described.adaptation;
  state.plan = described;
  renderReview(described);
  const n = described.adaptation.reasons.length;
  toast(`${message} ${n} change${n === 1 ? '' : 's'}, each with its reason under Changes.`,
    { label: 'Undo', run: undo });
  return described;
}

function initAdapt() {
  $('#m-cancel').addEventListener('click', () => $('#missed-dialog').close());
  $('#m-keep').addEventListener('click', () => {
    $('#missed-dialog').close();
    toast('Kept as is.');
  });
  $('#m-readapt').addEventListener('click', async () => {
    const w = state.plan.workouts[missed.index];
    try {
      await adaptPlan({ action: 'missed', dates: [w.date] }, `Readapted around ${w.name}.`);
      $('#missed-dialog').close();
    } catch (err) {
      setError('#missed-error', err.message);
    }
  });

  $('#pa-cancel').addEventListener('click', () => $('#pause-dialog').close());
  $('#pa-go').addEventListener('click', async () => {
    try {
      await adaptPlan({
        action: 'pause', start: $('#pa-start').value,
        days: Number($('#pa-days').value), reason: $('#pa-reason').value,
      }, 'Paused.');
      $('#pause-dialog').close();
    } catch (err) {
      setError('#pause-error', err.message);
    }
  });

  $('#rt-cancel').addEventListener('click', () => $('#return-dialog').close());
  $('#rt-go').addEventListener('click', async () => {
    try {
      const described = await api('/api/adapt', {
        action: 'return', days_off: Number($('#rt-days').value), start: $('#rt-start').value,
      });
      $('#return-dialog').close();
      openPlan(described, true, 'built');
      toast(described.adaptation.reasons[0] || 'Built.');
    } catch (err) {
      setError('#return-error', err.message);
    }
  });
}

/* ------------------------------------------------------------- rewrite --- */

const rewrite = { index: null };

function openRewrite(index) {
  rewrite.index = index;
  const w = state.plan.workouts[index];
  $('#rw-title').textContent = `Rewrite ${w.name}`;
  $('#rw-text').value = '';
  $('#rw-log').innerHTML = '';
  $('#rw-progress').hidden = true;
  $('#rw-go').disabled = false;
  setError('#rewrite-error', '');
  const provider = state.providers.find(p => p.name === $('#c-provider').value);
  if (provider && (provider.kind === 'manual' || provider.kind === 'paste')) {
    setError('#rewrite-error', 'Rewriting one session needs an API provider. With paste, use Edit instead.');
    $('#rw-go').disabled = true;
  }
  $('#rewrite-dialog').showModal();
}

function initRewrite() {
  $('#rw-cancel').addEventListener('click', () => {
    clearTimeout(state.pollTimer);
    $('#rewrite-dialog').close();
  });
  $('#rw-go').addEventListener('click', async () => {
    const instruction = $('#rw-text').value.trim();
    if (!instruction) { setError('#rewrite-error', 'Say what should change.'); return; }
    const w = state.plan.workouts[rewrite.index];
    const before = structuredClone(state.plan.json);
    $('#rw-go').disabled = true;
    $('#rw-progress').hidden = false;
    $('#rw-log').innerHTML = '';
    setError('#rewrite-error', '');
    try {
      const { job } = await api('/api/regenerate', {
        plan: before, date: w.date, instruction, provider: $('#c-provider').value,
      });
      const done = await pollJob(job, '#rw-log');
      state.history.push(before);
      state.plan = done.result;
      renderReview(done.result);
      $('#rewrite-dialog').close();
      toast(`Rewrote ${w.name}.`, { label: 'Undo', run: undo });
    } catch (err) {
      setError('#rewrite-error', err.message);
      $('#rw-go').disabled = false;
      $('#rw-progress').hidden = true;
    }
  });
}

/* -------------------------------------------------------------- export --- */

function openExport() {
  if (!state.plan) return;
  const select = $('#x-workout');
  select.innerHTML = '';
  for (const i of sortedIndexes(state.plan.workouts)) {
    const w = state.plan.workouts[i];
    select.append(el('option', { value: String(i) }, `${fmtDate(w.date)} — ${w.name}`));
  }
  setError('#export-error', '');
  $('#x-copied').hidden = true;
  $('#export-dialog').showModal();
}

async function exportAs(format, index) {
  const r = await api('/api/export', { plan: state.plan.json, format, index });
  if (r.base64) downloadBase64(r.filename, r.base64, r.mime);
  else downloadText(r.filename, r.text, r.mime);
  return r;
}

/* Binary exports (FIT) travel as base64 (#144). */
function downloadBase64(filename, base64, mime) {
  const bytes = Uint8Array.from(atob(base64), c => c.charCodeAt(0));
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([bytes], { type: mime }));
  a.download = filename;
  a.click();
  URL.revokeObjectURL(a.href);
}

function initExport() {
  $('#x-close').addEventListener('click', () => $('#export-dialog').close());
  $$('#export-dialog [data-export]').forEach(b => b.addEventListener('click', async () => {
    try { await exportAs(b.dataset.export); } catch (err) { setError('#export-error', err.message); }
  }));
  $('#x-download').addEventListener('click', async () => {
    try { await exportAs($('#x-format').value, Number($('#x-workout').value)); }
    catch (err) { setError('#export-error', err.message); }
  });
  $('#x-copy').addEventListener('click', async () => {
    try {
      const r = await api('/api/export', {
        plan: state.plan.json, format: $('#x-format').value, index: Number($('#x-workout').value),
      });
      await copyText(r.text, '#x-copied');
    } catch (err) {
      setError('#export-error', err.message);
    }
  });
}

/* ------------------------------------------------------------- library --- */

async function openLibrary() {
  setError('#library-error', '');
  $('#l-save-plan').hidden = !state.plan;
  const last = state.plan ? state.plan.workouts.map(w => w.date).sort().at(-1) : null;
  $('#l-date').value = last ? isoAdd(last, 1) : todayIso();
  $('#l-start').value = nextMonday();
  $('#library-dialog').showModal();
  try {
    renderLibrary(await api('/api/library', { action: 'list' }));
  } catch (err) {
    setError('#library-error', err.message);
  }
}

function renderLibrary(lib) {
  const ws = $('#l-workouts');
  ws.innerHTML = '';
  for (const w of lib.workouts) {
    const add = el('button', { type: 'button', class: 'btn sm' }, state.plan ? 'Add' : 'Start a plan');
    add.addEventListener('click', () => addLibraryWorkout(w.slug, w.name));
    ws.append(el('li', {}, el('span', { class: 'l-name' }, w.name), el('span', { class: 'tag' }, w.source), add));
  }
  const ps = $('#l-plans');
  ps.innerHTML = '';
  if (!lib.plans.length) ps.append(el('li', { class: 'hint' }, 'No templates yet. Save the current plan as one.'));
  for (const p of lib.plans) {
    const open = el('button', { type: 'button', class: 'btn sm' }, 'Open');
    open.addEventListener('click', async () => {
      try {
        const described = await api('/api/library', { action: 'plan', name: p.slug, start: $('#l-start').value });
        $('#library-dialog').close();
        openPlan(described, true, 'opened from the library');
      } catch (err) {
        setError('#library-error', err.message);
      }
    });
    ps.append(el('li', {}, el('span', { class: 'l-name' }, `${p.name} · ${p.workouts} sessions`), open));
  }
}

async function addLibraryWorkout(slug, name) {
  const date = $('#l-date').value || todayIso();
  try {
    const { workout } = await api('/api/library', { action: 'workout', name: slug, date });
    if (state.plan) {
      await mutate(p => { p.workouts.push(workout); });
      $('#l-date').value = isoAdd(date, 1);
      toast(`Added ${name} on ${fmtDate(date)}.`, { label: 'Undo', run: undo });
    } else {
      const described = await api('/api/preview', { plan: { plan: name, workouts: [workout] } });
      $('#library-dialog').close();
      openPlan(described, true, 'started from the library');
    }
  } catch (err) {
    setError('#library-error', err.message);
  }
}

async function savePlanTemplate() {
  if (!state.plan) return;
  try {
    const r = await api('/api/library', { action: 'save_plan', plan: state.plan.json });
    toast(`Saved as a template: ${r.saved}`);
  } catch (err) {
    toast(err.message, null, true);
  }
}

async function saveWorkoutToLibrary(index) {
  try {
    const r = await api('/api/library', { action: 'save_workout', workout: state.plan.json.workouts[index] });
    toast(`Saved to the library: ${r.saved}`);
  } catch (err) {
    toast(err.message, null, true);
  }
}

function initLibrary() {
  $('#l-close').addEventListener('click', () => $('#library-dialog').close());
  $('#l-save-plan').addEventListener('click', savePlanTemplate);
}

/* ------------------------------------------------------------- palette --- */

function paletteCommands() {
  const hasPlan = () => !!state.plan;
  const always = () => true;
  return [
    { label: 'Write a new plan', when: always, run: () => show('compose') },
    { label: 'Review the plan', when: hasPlan, run: () => show('review') },
    { label: 'Calendar view', when: hasPlan, run: () => { setView('calendar'); show('review'); } },
    { label: 'List view', when: hasPlan, run: () => { setView('list'); show('review'); } },
    { label: 'Send to Garmin', when: hasPlan, run: () => $('#r-push').click() },
    { label: 'Export…', when: hasPlan, run: openExport },
    { label: 'Export a calendar file (.ics)', when: hasPlan, run: () => exportAs('ics').catch(err => toast(err.message, null, true)) },
    { label: 'Hot day: show paces slowed for today…', when: hasPlan, run: () => $('#heat-dialog').showModal() },
    { label: 'Print the plan', when: hasPlan, run: () => window.print() },
    { label: 'Keyboard shortcuts', when: always, run: () => $('#keys-dialog').showModal() },
    { label: 'Download the plan as JSON', when: hasPlan, run: () => exportAs('json').catch(err => toast(err.message, null, true)) },
    { label: 'Library…', when: always, run: openLibrary },
    { label: 'Save the plan as a template', when: hasPlan, run: savePlanTemplate },
    { label: 'Pause the plan (illness or holiday)…', when: hasPlan, run: () => openDialog('#pause-dialog') },
    { label: 'Build a return-to-run ramp…', when: always, run: () => openDialog('#return-dialog') },
    { label: 'Preview the next session on the watch', when: hasPlan, run: () => openWatch(nextIndex()) },
    { label: 'Undo the last change', when: () => state.history.length > 0, run: undo },
    { label: 'Edit profile', when: always, run: () => show('setup') },
    { label: 'Toggle dark mode', when: always, run: () => $('#theme-toggle').click() },
  ].filter(c => c.when());
}

function initPalette() {
  const dialog = $('#palette'), input = $('#pal-input'), list = $('#pal-list');
  let items = [], cursor = 0;

  const render = () => {
    const q = input.value.trim().toLowerCase();
    items = paletteCommands().filter(c => !q || c.label.toLowerCase().includes(q));
    cursor = Math.min(cursor, Math.max(0, items.length - 1));
    list.innerHTML = '';
    items.forEach((c, i) => {
      const li = el('li', { role: 'option', id: `pal-${i}`, 'aria-selected': String(i === cursor) }, c.label);
      li.addEventListener('mousemove', () => { if (cursor !== i) { cursor = i; render(); } });
      li.addEventListener('click', () => run(i));
      list.append(li);
    });
    if (!items.length) list.append(el('li', { class: 'hint' }, 'No matching command.'));
    input.setAttribute('aria-activedescendant', items.length ? `pal-${cursor}` : '');
  };
  const run = i => {
    const c = items[i];
    dialog.close();
    if (c) c.run();
  };
  const open = () => {
    input.value = '';
    cursor = 0;
    render();
    dialog.showModal();
    input.focus();
  };

  $('#palette-btn').addEventListener('click', open);
  document.addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      if (dialog.open) dialog.close(); else open();
    }
    const typing = ['INPUT', 'TEXTAREA', 'SELECT'].includes((e.target || {}).tagName);
    if (e.key === '?' && !typing && !document.querySelector('dialog[open]')) {
      e.preventDefault();
      $('#keys-dialog').showModal();
    }
  });
  input.addEventListener('input', () => { cursor = 0; render(); });
  input.addEventListener('keydown', e => {
    const n = Math.max(1, items.length);
    if (e.key === 'ArrowDown') { e.preventDefault(); cursor = (cursor + 1) % n; render(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); cursor = (cursor - 1 + n) % n; render(); }
    else if (e.key === 'Enter') { e.preventDefault(); run(cursor); }
  });
  dialog.addEventListener('click', e => { if (e.target === dialog) dialog.close(); });
}

/* ---------------------------------------------------------------- init --- */

function initReview() {
  $('#r-back').addEventListener('click', () => show('compose'));
  $('#r-export').addEventListener('click', openExport);
  $('#r-library').addEventListener('click', openLibrary);
  $('#c-library').addEventListener('click', openLibrary);
  $('#r-push').addEventListener('click', () => {
    setError('#push-error', '');
    $('#p-results').hidden = true;
    $('#p-results').innerHTML = '';
    $('#p-progress').hidden = true;
    $('#p-mfa').hidden = true;
    $('#p-log').innerHTML = '';
    $('#p-go').disabled = false;
    const blocks = state.plan ? state.plan.report.blocks : 0;
    const notice = $('#p-blocks');
    notice.hidden = !blocks;
    if (blocks) {
      notice.textContent = `The sanity report has ${blocks} blocking finding${blocks > 1 ? 's' : ''}. ` +
        'Garmin will accept the workouts; the plan itself may not be safe as written.';
    }
    $('#push-dialog').showModal();
  });
  rovingGroup($$('.seg[data-view]'), 'aria-checked', btn => setView(btn.dataset.view));
  $('#changes-undo').addEventListener('click', undo);
  $('#changes-reset').addEventListener('click', resetToBaseline);
  initEdit();
  initWatch();
  initAdapt();
  initRewrite();
  initExport();
  initLibrary();
  initMove();
  initHeat();
  $('#keys-close').addEventListener('click', () => $('#keys-dialog').close());
}


/* ------------------------------------------------------- more panels --- */

/* The model's one-paragraph rationale for the plan (#158). */
function renderRationale(data) {
  const node = $('#r-rationale');
  const text = (data.json && data.json.summary) || '';
  node.hidden = !text;
  node.textContent = text;
}

/* Garmin's three load buckets, computed from the plan (#159). */
function renderFocus(focus) {
  const host = $('#health-focus');
  host.innerHTML = '';
  if (!focus || !focus.minutes) return;
  const labels = { low_aerobic: 'Low aerobic', high_aerobic: 'High aerobic', anaerobic: 'Anaerobic' };
  const colors = { low_aerobic: rampColor(0.24), high_aerobic: rampColor(0.62), anaerobic: rampColor(0.95) };
  const weeks = focus.window_weeks;
  host.append(el('div', { class: 'focus-title' }, `Load focus, ${weeks === 1 ? 'this week' : `last ${weeks} weeks`}`));
  for (const key of ['low_aerobic', 'high_aerobic', 'anaerobic']) {
    const share = focus.shares[key] || 0;
    const bar = el('div', { class: 'focus-bar' });
    const fill = el('div', { class: 'focus-fill' });
    fill.style.width = `${Math.max(1, share * 100)}%`;
    fill.style.background = colors[key];
    bar.append(fill);
    host.append(
      el('div', { class: 'focus-row', title: `${focus.minutes[key]} min` },
        el('span', { class: 'focus-label' }, labels[key]), bar,
        el('span', { class: 'focus-val' }, `${Math.round(share * 100)}%`)),
    );
  }
  host.append(el('p', { class: 'hint' }, focus.verdict));
}

/* This week and next, from the server's agenda (#156). */
function renderWeek(agenda) {
  const panel = $('#panel-week');
  const host = $('#week-body');
  host.innerHTML = '';
  panel.hidden = !agenda;
  if (!agenda) return;
  const week = agenda.this_week;
  $('#week-title').textContent = week.sessions.length
    ? `This week · ${fmtKm(week.km)} · ${Math.round((week.hard_share || 0) * 100)}% hard`
    : 'This week: nothing planned';
  $('#week-race').textContent = agenda.days_to_race != null && agenda.days_to_race >= 0
    ? `${agenda.days_to_race} days to ${agenda.race}` : '';
  for (const s of week.sessions) {
    const li = el('li', { class: `week-item${s.done ? ' is-done' : ''}${s.name === week.key_session ? ' is-key' : ''}` });
    li.append(
      el('span', { class: 'wk-day' }, s.day),
      el('span', { class: 'wk-name' }, s.name),
      el('span', { class: 'wk-meta' }, `${s.role} · ${s.minutes} min`),
    );
    host.append(li);
  }
  const nxt = agenda.next_week;
  const parts = [];
  if (nxt.sessions.length) {
    parts.push(`Next week: ${nxt.sessions.length} sessions, ${fmtKm(nxt.km)}${nxt.key_session ? `, key session ${nxt.key_session}` : ''}.`);
  }
  if (agenda.phase_change) parts.push(`${agenda.phase_change}.`);
  $('#week-next').textContent = parts.join(' ');
}

/* Synced weekly volume against the plan (#167): shown only once there is history. */
async function renderMileage(data) {
  const panel = $('#panel-mileage');
  if (state.mileage === null) {
    try { state.mileage = (await api('/api/history', { since_days: 84 })).weeks || []; }
    catch { state.mileage = []; }
  }
  const actual = state.mileage.filter(w => w.km > 0);
  panel.hidden = !actual.length;
  if (!actual.length) return;
  const host = $('#mileage-body');
  host.innerHTML = '';
  const planned = Object.fromEntries((data.dashboard.weeks || []).map(w => [w.start, w.km]));
  const rows = state.mileage.slice(-8).map(w => ({ start: w.start, actual: w.km || 0, planned: planned[w.start] || 0 }));
  const peak = Math.max(...rows.map(r => Math.max(r.actual, r.planned)), 1);
  for (const r of rows) {
    const col = el('div', { class: 'hw', title: `Week of ${r.start}: ran ${fmtKm(r.actual)}${r.planned ? `, planned ${fmtKm(r.planned)}` : ''}` });
    const bars = el('div', { class: 'hw-bar dual' });
    const a = el('div', { class: 'hw-fill' });
    a.style.height = `${(r.actual / peak) * 100}%`;
    a.style.background = 'var(--accent)';
    const p = el('div', { class: 'hw-fill planned' });
    p.style.height = `${(r.planned / peak) * 100}%`;
    bars.append(a, p);
    col.append(bars, el('span', { class: 'hw-week' }, r.start.slice(5)), el('span', { class: 'hw-val' }, fmtKm(r.actual)));
    host.append(col);
  }
}

/* --------------------------------------------------------- card extras --- */

function zonesOf(workout) {
  const out = [];
  const walk = steps => {
    for (const s of steps || []) {
      if (s.kind === 'repeat') walk(s.steps);
      const t = s.target;
      if (t && t.type === 'pace' && typeof t.zone === 'string' && !out.includes(t.zone)) out.push(t.zone);
    }
  };
  walk(workout && workout.steps);
  return out;
}

/* Recap line from the synced run (#155) and hot-day paces (#166) under a card. */
function cardExtras(w, index) {
  const box = el('div', { class: 'w-extra' });
  const recap = state.recaps[`${w.date}|${w.name}`];
  if (recap) box.append(el('p', { class: `recap ${recap.status}` }, el('span', { class: 'sev' }, recap.status), recap.text));
  if (state.heat && state.plan) {
    const zones = zonesOf(state.plan.json.workouts[index]).filter(z => state.heat.zones[z]);
    if (zones.length) {
      const text = zones.map(z => `${z} ${state.heat.zones[z].slow}–${state.heat.zones[z].fast}`).join(' · ');
      box.append(el('p', { class: 'heat-hint' }, `Hot-day paces (dew point ${state.heat.dew_point_c} °C): ${text}`));
    }
  }
  return box;  // empty boxes collapse in CSS; append(null) would print the word null
}

async function loadRecaps(described) {
  const today = todayIso();
  state.recaps = {};
  if (!described.workouts.some(w => w.date < today)) return;
  try {
    const { recaps } = await api('/api/recap', { plan: described.json });
    state.recaps = Object.fromEntries(recaps.map(r => [`${r.date}|${r.name}`, r]));
  } catch {
    state.recaps = {};
  }
  if (Object.keys(state.recaps).length && state.plan === described) renderReview(described);
}

/* ------------------------------------------------------------ education --- */

/* One or two short explanations of the session type, with the evidence line (#164). */
function renderWhy(sel, workout, max = 2) {
  const host = $(sel);
  host.innerHTML = '';
  const keys = [];
  const role = workout ? workout.role : null;
  const generic = role === 'quality' || role === 'cross';
  if (role && !generic && state.education[role]) keys.push(role);
  // The work zones say what the session is for; the warm-up zone comes last.
  const zones = zonesOf(workout);
  const work = zones.filter(z => z !== 'easy' && z !== 'recovery');
  for (const z of [...work, ...zones.filter(z => !work.includes(z))]) {
    if (state.education[z] && !keys.includes(z)) keys.push(z);
  }
  if (!keys.length && role === 'quality') keys.push('threshold');
  const entries = keys.slice(0, max).map(k => state.education[k]);
  host.hidden = !entries.length;
  for (const e of entries) {
    host.append(
      el('p', {}, el('strong', {}, `${e.title}: `), e.what, ' ', el('em', {}, e.feel)),
      el('p', { class: 'hint' }, `${e.evidence} — ${e.source}`),
    );
  }
}

/* ----------------------------------------------------------------- move --- */

const move = { index: null };

function openMove(index) {
  move.index = index;
  const w = state.plan.workouts[index];
  $('#mv-title').textContent = `Move ${w.name}`;
  $('#mv-date').value = w.date;
  setError('#move-error', '');
  $('#move-dialog').showModal();
}

function initMove() {
  $('#mv-cancel').addEventListener('click', () => $('#move-dialog').close());
  $$('#move-dialog [data-shift]').forEach(b => b.addEventListener('click', () => {
    $('#mv-date').value = isoAdd($('#mv-date').value || todayIso(), Number(b.dataset.shift));
  }));
  $('#mv-go').addEventListener('click', async () => {
    const iso = $('#mv-date').value;
    if (!iso) { setError('#move-error', 'Pick a date.'); return; }
    $('#move-dialog').close();
    await moveWorkout(move.index, iso);
  });
}

/* -------------------------------------------------------------- hot day --- */

function initHeat() {
  $('#r-heat-badge').addEventListener('click', () => $('#heat-dialog').showModal());
  $('#h-clear').addEventListener('click', () => {
    state.heat = null;
    $('#r-heat-badge').hidden = true;
    $('#h-note').textContent = '';
    $('#heat-dialog').close();
    if (state.plan) renderReview(state.plan);
  });
  $('#h-apply').addEventListener('click', async () => {
    setError('#heat-error', '');
    try {
      state.heat = await api('/api/heat', { temp_c: $('#h-temp').value, humidity_pct: $('#h-humidity').value });
    } catch (err) {
      setError('#heat-error', err.message);
      return;
    }
    $('#h-note').textContent = `Dew point ${state.heat.dew_point_c} °C, +${state.heat.slowdown_spk} s/km. ${state.heat.note}`;
    const badge = $('#r-heat-badge');
    badge.hidden = false;
    badge.textContent = `Hot day: +${state.heat.slowdown_spk} s/km`;
    $('#heat-dialog').close();
    if (state.plan) renderReview(state.plan);
    toast(state.heat.note);
  });
}
