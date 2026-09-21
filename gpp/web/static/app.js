/* garmin-plan-push — local UI.
   No framework, no build step: this file is what runs. */

'use strict';

const TOKEN = document.querySelector('meta[name="gpp-token"]').content;
const POLL_MS = 700;

const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = {
  units: 'metric',
  threshold: null,      // "4:30/km"
  plan: null,           // last describe() payload
  providers: [],
  pollTimer: null,
};

/* ---------------------------------------------------------------- api --- */

async function api(path, body = {}) {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-GPP-Token': TOKEN },
    body: JSON.stringify(body),
  });
  let data;
  try { data = await res.json(); } catch { data = {}; }
  if (!res.ok) throw new Error(data.error || `request failed (${res.status})`);
  return data;
}

/* -------------------------------------------------------------- colour --- */
/* Intensity 0..1 -> a cool-to-hot ramp. Stops are interpolated in RGB, which
   is good enough across these short hops and keeps the maths readable. */

const RAMP = [
  [0.00, [ 82, 132, 219]],
  [0.25, [ 56, 168, 168]],
  [0.50, [ 74, 168, 104]],
  [0.70, [226, 158,  46]],
  [0.86, [227, 112,  48]],
  [1.00, [214,  68,  68]],
];

function rampColor(t) {
  t = Math.max(0, Math.min(1, Number(t) || 0));
  for (let i = 1; i < RAMP.length; i++) {
    if (t <= RAMP[i][0]) {
      const [t0, c0] = RAMP[i - 1], [t1, c1] = RAMP[i];
      const k = (t - t0) / (t1 - t0 || 1);
      const c = c0.map((v, j) => Math.round(v + (c1[j] - v) * k));
      return `rgb(${c[0]} ${c[1]} ${c[2]})`;
    }
  }
  return `rgb(${RAMP.at(-1)[1].join(' ')})`;
}

/* --------------------------------------------------------------- views --- */

function show(view) {
  $$('.view').forEach(v => { v.hidden = v.id !== `view-${view}`; });
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function setError(el, message) {
  const node = $(el);
  node.textContent = message || '';
  node.hidden = !message;
}

/* --------------------------------------------------------------- theme --- */

function initTheme() {
  const saved = localStorage.getItem('gpp-theme');
  if (saved) document.documentElement.dataset.theme = saved;
  $('#theme-toggle').addEventListener('click', () => {
    const root = document.documentElement;
    const isDark = root.dataset.theme
      ? root.dataset.theme === 'dark'
      : matchMedia('(prefers-color-scheme: dark)').matches;
    const next = isDark ? 'light' : 'dark';
    root.dataset.theme = next;
    localStorage.setItem('gpp-theme', next);
  });
}

/* --------------------------------------------------------------- setup --- */

function renderZones(target, zones, hint) {
  const list = $(target);
  list.innerHTML = '';
  if (!zones || !zones.length) return;
  if (hint) $(hint).hidden = true;
  for (const z of zones) {
    const li = document.createElement('li');
    const sw = document.createElement('span');
    sw.className = 'swatch';
    sw.style.background = rampColor(z.intensity);
    const name = document.createElement('span');
    name.className = 'zname';
    name.textContent = z.name;
    const val = document.createElement('span');
    val.className = 'zval';
    val.textContent = `${z.slow} – ${z.fast}`;
    li.append(sw, name, val);
    list.append(li);
  }
}

let zoneDebounce;
function scheduleZonePreview() {
  clearTimeout(zoneDebounce);
  zoneDebounce = setTimeout(refreshZonePreview, 260);
}

async function refreshZonePreview() {
  const activeTab = $('.tab.is-active').dataset.tab;
  setError('#setup-error', '');

  try {
    if (activeTab === 'race') {
      const time = $('#s-race-time').value.trim();
      if (!time) { $('#s-save').disabled = true; return; }
      const est = await api('/api/estimate', {
        distance: $('#s-race-dist').value,
        time,
        imperial: state.units === 'imperial',
      });
      state.threshold = est.threshold;
      $('#s-estimate-note').hidden = false;
      $('#s-estimate-note').textContent =
        `Threshold ≈ ${est.threshold} — about ${est.hour_distance_km} km in an hour.` +
        (est.reliable ? '' : ' (rough: outside the formula’s best range)');
    } else {
      const value = $('#s-threshold').value.trim();
      if (!value) { $('#s-save').disabled = true; return; }
      state.threshold = value;
    }

    const preview = await api('/api/zones-preview', {
      threshold: state.threshold,
      imperial: state.units === 'imperial',
    });
    renderZones('#zone-list', preview.zones, '#zone-hint');
    $('#s-save').disabled = false;
  } catch (err) {
    $('#s-save').disabled = true;
    setError('#setup-error', err.message);
  }
}

function initSetup() {
  $$('.seg').forEach(btn => btn.addEventListener('click', () => {
    $$('.seg').forEach(b => {
      b.classList.toggle('is-active', b === btn);
      b.setAttribute('aria-checked', String(b === btn));
    });
    state.units = btn.dataset.units;
    scheduleZonePreview();
  }));

  $$('.tab').forEach(tab => tab.addEventListener('click', () => {
    $$('.tab').forEach(t => {
      t.classList.toggle('is-active', t === tab);
      t.setAttribute('aria-selected', String(t === tab));
    });
    $$('.tab-panel').forEach(p => { p.hidden = p.dataset.panel !== tab.dataset.tab; });
    scheduleZonePreview();
  }));

  ['#s-race-time', '#s-threshold'].forEach(sel =>
    $(sel).addEventListener('input', scheduleZonePreview));
  $('#s-race-dist').addEventListener('change', scheduleZonePreview);

  $('#s-hrmax').addEventListener('change', async () => {
    const hrMax = Number($('#s-hrmax').value);
    if (!hrMax || $('#s-lthr').value) return;
    try {
      const { lthr } = await api('/api/estimate-lthr', { hr_max: hrMax });
      $('#s-lthr').value = lthr;
    } catch { /* an estimate is a nicety, not worth an error */ }
  });

  $('#s-save').addEventListener('click', async () => {
    const btn = $('#s-save');
    btn.disabled = true;
    try {
      await api('/api/profile', {
        name: $('#s-name').value,
        imperial: state.units === 'imperial',
        threshold: state.threshold,
        lthr: $('#s-lthr').value || null,
        hr_max: $('#s-hrmax').value || null,
      });
      await boot();
    } catch (err) {
      setError('#setup-error', err.message);
      btn.disabled = false;
    }
  });
}

/* ------------------------------------------------------------- compose --- */

function initCompose() {
  $$('#c-examples .chip').forEach(chip => chip.addEventListener('click', () => {
    $('#c-request').value = chip.dataset.fill;
    $('#c-request').focus();
  }));

  $('#c-generate').addEventListener('click', generate);
  $('#c-request').addEventListener('keydown', e => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') generate();
  });
}

async function generate() {
  const request = $('#c-request').value.trim();
  setError('#compose-error', '');
  if (!request) { setError('#compose-error', 'Describe the training you want first.'); return; }

  const btn = $('#c-generate');
  btn.disabled = true;
  $('.btn-label', btn).textContent = 'Writing…';
  $('#c-progress').hidden = false;
  $('#c-log').innerHTML = '';

  try {
    const { job } = await api('/api/generate', {
      request, provider: $('#c-provider').value,
    });
    const done = await pollJob(job, '#c-log', showRelay);
    hideRelay();
    state.plan = done.result;
    renderReview(done.result);
    show('review');
  } catch (err) {
    setError('#compose-error', err.message);
  } finally {
    btn.disabled = false;
    $('.btn-label', btn).textContent = 'Write my plan';
    $('#c-progress').hidden = true;
  }
}

/* --------------------------------------------------------------- relay --- */
/* The no-API-key path: we hand the user the prompt, they hand back the reply. */

function showRelay(prompt, id, snap) {
  state.relayJob = id;
  state.relayText = snap.relay || '';
  $('#c-relay-label').textContent = prompt || 'Paste the reply';
  $('#c-relay').hidden = false;
  $('#c-relay-value').focus();
}

function hideRelay() {
  $('#c-relay').hidden = true;
  $('#c-relay-value').value = '';
  $('#c-relay-copied').hidden = true;
}

function initRelay() {
  $('#c-relay-copy').addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(state.relayText || '');
      $('#c-relay-copied').hidden = false;
      setTimeout(() => { $('#c-relay-copied').hidden = true; }, 2200);
    } catch {
      // Clipboard access can be refused; falling back to a selectable box
      // beats failing silently.
      const box = $('#c-relay-value');
      box.value = state.relayText || '';
      box.select();
      setError('#compose-error',
        'Could not reach the clipboard — the prompt is in the box below, copy it from there.');
    }
  });

  $('#c-relay-send').addEventListener('click', async () => {
    const value = $('#c-relay-value').value.trim();
    if (!value || !state.relayJob) return;
    $('#c-relay').hidden = true;
    try {
      await api('/api/job-input', { id: state.relayJob, value });
    } catch (err) {
      setError('#compose-error', err.message);
    }
  });
}

/* ---------------------------------------------------------------- jobs --- */

function appendLog(sel, message, bad = false) {
  const list = $(sel);
  if (!list) return;
  const li = document.createElement('li');
  li.textContent = message;
  if (bad) li.classList.add('is-bad');
  list.append(li);
  list.scrollTop = list.scrollHeight;
}

function pollJob(id, logSel, onInput) {
  return new Promise((resolve, reject) => {
    let seen = 0;
    let asked = false;  // only raise the input UI once per pause
    const tick = async () => {
      let snap;
      try {
        snap = await api('/api/job', { id });
      } catch (err) { return reject(err); }

      snap.log.slice(seen).forEach(line => appendLog(logSel, line, /error|failed/i.test(line)));
      seen = snap.log.length;

      if (snap.status === 'awaiting_input') {
        if (onInput && !asked) { onInput(snap.prompt, id, snap); asked = true; }
        state.pollTimer = setTimeout(tick, POLL_MS);
        return;
      }
      asked = false;
      if (snap.status === 'done')  return resolve(snap);
      if (snap.status === 'error') return reject(new Error(snap.error || 'failed'));
      state.pollTimer = setTimeout(tick, POLL_MS);
    };
    tick();
  });
}

/* -------------------------------------------------------------- review --- */

const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const KIND_LABEL = {
  warmup: 'Warm up', run: 'Run', recover: 'Recover',
  rest: 'Rest', cooldown: 'Cool down',
};

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

function renderLegend() {
  const names = [
    ['Recovery', 0.08], ['Easy', 0.24], ['Steady', 0.42],
    ['Marathon', 0.56], ['Threshold', 0.72], ['Interval', 0.88], ['Reps', 1.0],
  ];
  $('#r-legend').innerHTML = '';
  for (const [label, t] of names) {
    const span = document.createElement('span');
    const dot = document.createElement('i');
    dot.style.background = rampColor(t);
    span.append(dot, document.createTextNode(label));
    $('#r-legend').append(span);
  }
}

function renderReview(data) {
  $('#r-title').textContent = data.plan;

  const total = data.workouts.reduce((a, w) => a + w.seconds, 0);
  const dist = data.workouts.reduce((a, w) => a + (w.summary.metres || 0), 0);
  $('#r-summary').textContent =
    `${data.workouts.length} sessions · about ${fmtDuration(total)} of running · ${fmtDistance(dist)}`;

  renderLegend();

  const host = $('#r-workouts');
  host.innerHTML = '';
  data.workouts.forEach((w, i) => {
    host.append(workoutCard(w, i));
  });
}

function workoutCard(w, index) {
  const card = document.createElement('article');
  card.className = 'workout';
  card.style.animationDelay = `${Math.min(index * 45, 300)}ms`;

  const date = new Date(`${w.date}T00:00:00`);
  const head = document.createElement('div');
  head.className = 'w-head';
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
    </div>`;
  $('.w-name', head).textContent = w.name;
  if (w.notes) $('.w-note', head).textContent = w.notes;
  else $('.w-note', head).remove();

  const bar = document.createElement('div');
  bar.className = 'profile-bar';
  const totalSeconds = w.timeline.reduce((a, b) => a + b.seconds, 0) || 1;
  for (const block of w.timeline) {
    const seg = document.createElement('div');
    seg.className = 'seg-block' + (block.open_ended ? ' is-open' : '');
    seg.style.flex = `${(block.seconds / totalSeconds) * 100} 0 0`;
    seg.style.background = rampColor(block.intensity);
    seg.style.height = `${28 + block.intensity * 72}%`;
    const rep = block.rep ? ` (rep ${block.rep}/${block.of})` : '';
    seg.title = `${KIND_LABEL[block.kind] || block.kind}${rep} · ${block.extent} · ${block.target}`;
    bar.append(seg);
  }

  const steps = document.createElement('details');
  steps.className = 'w-steps';
  const rows = w.timeline.map(b => {
    const tr = document.createElement('tr');
    const rep = b.rep ? ` <span class="muted">${b.rep}/${b.of}</span>` : '';
    tr.innerHTML = `
      <td>${b.extent}</td>
      <td><span class="kind"><i class="dot"></i>${KIND_LABEL[b.kind] || b.kind}${rep}</span></td>
      <td class="tgt">${b.target}</td>`;
    $('.dot', tr).style.background = rampColor(b.intensity);
    return tr;
  });
  const table = document.createElement('table');
  const tbody = document.createElement('tbody');
  rows.forEach(r => tbody.append(r));
  table.append(tbody);
  const notes = [];
  if (w.summary.open_ended) notes.push('includes a lap-button step');
  if (w.summary.truncated) notes.push('chart shows the first ' + w.timeline.length);
  steps.innerHTML =
    `<summary>${w.timeline.length} steps${notes.length ? ' · ' + notes.join(' · ') : ''}</summary>`;
  steps.append(table);

  card.append(head, bar, steps);
  return card;
}

function initReview() {
  $('#r-back').addEventListener('click', () => show('compose'));

  $('#r-download').addEventListener('click', () => {
    if (!state.plan) return;
    const blob = new Blob([JSON.stringify(state.plan.json, null, 2)],
                          { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `${state.plan.plan.replace(/[^a-z0-9]+/gi, '-').toLowerCase()}.json`;
    a.click();
    URL.revokeObjectURL(a.href);
  });

  $('#r-push').addEventListener('click', () => {
    setError('#push-error', '');
    $('#p-results').hidden = true;
    $('#p-results').innerHTML = '';
    $('#p-progress').hidden = true;
    $('#p-mfa').hidden = true;
    $('#p-log').innerHTML = '';
    $('#p-go').disabled = false;
    $('#push-dialog').showModal();
  });
}

/* ---------------------------------------------------------------- push --- */

function initPush() {
  $('#p-cancel').addEventListener('click', () => {
    clearTimeout(state.pollTimer);
    $('#push-dialog').close();
  });

  $('#p-mfa-send').addEventListener('click', async () => {
    const code = $('#p-mfa-code').value.trim();
    if (!code || !state.mfaJob) return;
    $('#p-mfa').hidden = true;
    try {
      await api('/api/job-input', { id: state.mfaJob, value: code });
    } catch (err) {
      setError('#push-error', err.message);
    }
  });

  $('#p-go').addEventListener('click', async () => {
    const email = $('#p-email').value.trim();
    if (!email) { setError('#push-error', 'Your Garmin Connect email is needed.'); return; }
    if (!state.plan) return;

    setError('#push-error', '');
    $('#p-go').disabled = true;
    $('#p-progress').hidden = false;
    $('#p-log').innerHTML = '';

    try {
      const { job } = await api('/api/push', {
        plan: state.plan.json,
        email,
        password: $('#p-password').value,
        replace: $('#p-replace').checked,
      });
      const done = await pollJob(job, '#p-log', (prompt, id) => {
        state.mfaJob = id;
        $('#p-mfa-label').textContent = prompt;
        $('#p-mfa').hidden = false;
        $('#p-mfa-code').focus();
      });
      $('#p-password').value = '';
      // renderPushResults keeps the progress block up, because its last log
      // line is the one telling the user to sync their watch. Hiding it here
      // would erase the only instruction that matters.
      renderPushResults(done.result.results);
    } catch (err) {
      setError('#push-error', err.message);
      $('#p-go').disabled = false;
      $('#p-progress').hidden = true;
    }
  });
}

function renderPushResults(results) {
  const list = $('#p-results');
  list.innerHTML = '';
  for (const r of results) {
    const li = document.createElement('li');
    const tag = document.createElement('span');
    tag.className = `tag ${r.action}`;
    tag.textContent = r.action;
    const body = document.createElement('span');
    body.textContent = `${r.date} — ${r.name}`;
    li.append(tag, body);
    if (r.detail) {
      const detail = document.createElement('span');
      detail.className = 'detail';
      detail.textContent = r.detail;
      li.append(detail);
    }
    list.append(li);
  }
  list.hidden = false;
  const ok = results.every(r => r.action !== 'failed');
  appendLog('#p-log', ok ? 'Done. Sync your watch to pull them down.' : 'Finished with errors.', !ok);
  $('#p-progress').hidden = false;
  $('.spinner', $('#p-progress')).style.display = 'none';
}

/* ------------------------------------------------------------ settings --- */

function initSettings() {
  $('#profile-chip').addEventListener('click', async () => {
    const s = await api('/api/state');
    renderZones('#settings-zones', s.zones);
    $('#settings-path').textContent = `Saved at ${s.profile.path}`;
    $('#settings-dialog').showModal();
  });
  $('#st-close').addEventListener('click', () => $('#settings-dialog').close());
  $('#st-edit').addEventListener('click', () => {
    $('#settings-dialog').close();
    show('setup');
  });
}

/* ---------------------------------------------------------------- boot --- */

async function boot() {
  const s = await api('/api/state');

  if (!s.configured) { show('setup'); return; }

  state.units = s.profile.imperial ? 'imperial' : 'metric';
  state.providers = s.providers;

  const chip = $('#profile-chip');
  chip.hidden = false;
  chip.textContent = `${s.profile.name} · ${s.profile.threshold}`;

  const select = $('#c-provider');
  select.innerHTML = '';
  for (const p of s.providers) {
    const opt = document.createElement('option');
    opt.value = p.name;
    opt.textContent = p.model ? `${p.name} — ${p.model}` : p.name;
    if (p.name === s.default_provider) opt.selected = true;
    select.append(opt);
  }

  // Pre-fill setup so "Edit paces" is an edit, not a re-entry.
  $('#s-name').value = s.profile.name;
  $('#s-threshold').value = s.profile.threshold;
  if (s.profile.lthr) $('#s-lthr').value = s.profile.lthr;
  if (s.profile.hr_max) $('#s-hrmax').value = s.profile.hr_max;
  $$('.seg').forEach(b => b.classList.toggle('is-active', b.dataset.units === state.units));

  show('compose');
}

initTheme();
initSetup();
initCompose();
initRelay();
initReview();
initPush();
initSettings();
boot().catch(err => {
  document.body.insertAdjacentHTML('afterbegin',
    `<p class="error" style="padding:1rem">Could not start: ${err.message}</p>`);
});
