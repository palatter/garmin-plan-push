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
  plan: null,           // last describe() payload; state.plan.json is the plan itself
  baseline: null,       // the plan as generated / last pushed, for the diff
  baselineLabel: null,
  history: [],          // previous plan JSONs, for undo
  lastAdaptation: null, // reasons from the last pause / replan
  view: 'list',         // list | calendar
  recaps: {},           // 'date|name' -> recap row, from synced runs
  heat: null,           // /api/heat result while a hot day is set
  mileage: null,        // /api/history weeks, fetched once per page load
  education: {},        // session type -> explanation, from the server
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
  const activeTab = $('.tab[data-tab].is-active').dataset.tab;
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

/* A radiogroup or tablist promises arrow-key navigation and a single tab stop.
   Declaring the role without the behaviour is worse than declaring nothing:
   a screen reader tells the user to press arrows and nothing happens. */
function rovingGroup(items, selectedAttr, onSelect) {
  const select = (item, focus = true) => {
    items.forEach(el => {
      const on = el === item;
      el.classList.toggle('is-active', on);
      el.setAttribute(selectedAttr, String(on));
      el.tabIndex = on ? 0 : -1;
    });
    if (focus) item.focus();
    onSelect(item);
  };

  items.forEach((item, index) => {
    item.addEventListener('click', () => select(item, false));
    item.addEventListener('keydown', e => {
      const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key];
      let target = null;
      if (step) target = items[(index + step + items.length) % items.length];
      else if (e.key === 'Home') target = items[0];
      else if (e.key === 'End') target = items[items.length - 1];
      if (!target) return;
      e.preventDefault();
      select(target);
    });
  });
  return select;
}

function initSetup() {
  initAthleteFields();
  rovingGroup($$('.seg[data-units]'), 'aria-checked', btn => {
    state.units = btn.dataset.units;
    scheduleZonePreview();
  });

  rovingGroup($$('.tab[data-tab]'), 'aria-selected', tab => {
    $$('.tab-panel').forEach(p => { p.hidden = p.dataset.panel !== tab.dataset.tab; });
    scheduleZonePreview();
  });

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
        ...collectAthlete(),
      });
      await boot();
    } catch (err) {
      setError('#setup-error', err.message);
      btn.disabled = false;
    }
  });
}

/* The onboarding answers -- goal race, availability, injuries, constraints.
   They ride along with every profile save and are pre-filled on return. */

function initAthleteFields() {
  $$('.day').forEach(b => b.addEventListener('click', () => {
    b.setAttribute('aria-pressed', String(b.getAttribute('aria-pressed') !== 'true'));
  }));
}

function collectAthlete() {
  const num = sel => { const v = $(sel).value.trim(); return v ? Number(v) : null; };
  return {
    injuries: $('#s-injuries').value,
    constraints: $('#s-constraints').value,
    instructions: $('#s-instructions').value,
    longest_recent_run_km: num('#s-longest'),
    recent_weekly_km: num('#s-weekly'),
    availability: {
      days: $$('.day[aria-pressed="true"]').map(b => b.dataset.day),
      sessions_per_week: num('#s-sessions'),
      weekday_max_minutes: num('#s-weekday-max'),
      weekend_max_minutes: num('#s-weekend-max'),
      long_run_day: $('#s-long-day').value || null,
    },
    goal_race: {
      name: $('#s-goal-name').value.trim(),
      date: $('#s-goal-date').value,
      distance: $('#s-goal-dist').value,
      goal_time: $('#s-goal-time').value.trim(),
    },
  };
}

function fillAthlete(a) {
  if (!a) return;
  const av = a.availability || {}, goal = a.goal_race || {};
  $('#s-injuries').value = (a.injuries || []).join('\n');
  $('#s-constraints').value = (a.constraints || []).join('\n');
  $('#s-instructions').value = a.instructions || '';
  $('#s-longest').value = a.longest_recent_run_km ?? '';
  $('#s-weekly').value = a.recent_weekly_km ?? '';
  $$('.day').forEach(b => b.setAttribute('aria-pressed', String((av.days || []).includes(b.dataset.day))));
  $('#s-sessions').value = av.sessions_per_week ?? '';
  $('#s-weekday-max').value = av.weekday_max_minutes ?? '';
  $('#s-weekend-max').value = av.weekend_max_minutes ?? '';
  $('#s-long-day').value = av.long_run_day || '';
  $('#s-goal-name').value = goal.name || '';
  $('#s-goal-date').value = goal.date || '';
  $('#s-goal-dist').value = goal.distance || '';
  $('#s-goal-time').value = goal.goal_time || '';
  const answered = (a.injuries || []).length || (a.constraints || []).length || goal.date || (av.days || []).length;
  if (answered) $('#s-about').open = true;
}

/* Recent plans and the first-run checklist on the compose screen (#165, #168). */

function renderRecent(list) {
  const card = $('#c-recent');
  const host = $('#c-recent-list');
  host.innerHTML = '';
  card.hidden = !list.length;
  for (const r of list.slice(0, 6)) {
    const open = document.createElement('button');
    open.type = 'button';
    open.className = 'btn sm';
    open.textContent = 'Open';
    open.addEventListener('click', async () => {
      try {
        const described = await api('/api/recent', { path: r.path });
        openPlan(described, true, 'reopened');
      } catch (err) {
        setError('#compose-error', err.message);
      }
    });
    const li = document.createElement('li');
    const name = document.createElement('span');
    name.className = 'l-name';
    const span = r.first ? `${r.first} to ${r.last}` : 'no dates';
    name.textContent = `${r.name} — ${r.sessions} sessions, ${span}`;
    const tag = document.createElement('span');
    tag.className = 'tag';
    tag.textContent = r.label || 'saved';
    li.append(name, tag, open);
    host.append(li);
  }
}

function renderChecklist(s) {
  $('#c-checklist').hidden = Boolean((s.recent || []).length);
  $('#ck-profile').onclick = () => show('setup');
  $('#ck-template').onclick = () => openLibrary();
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
  $('#c-provider').addEventListener('change', updateProviderHint);
}

/* Warn BEFORE a doomed generate: a provider that needs a key it can't find
   would otherwise fail after the spinner. Paste is always offered as the way
   out because it needs nothing. */
function updateProviderHint() {
  const name = $('#c-provider').value;
  const p = state.providers.find(x => x.name === name);
  const hint = $('#c-provider-hint');
  if (!p) { hint.hidden = true; return; }
  if (p.key_present === false) {
    hint.textContent =
      `${p.key_env} isn't set in the shell that launched this app, so ${name} can't be called. ` +
      `Set it and restart, or choose "paste" — no key needed.`;
    hint.hidden = false;
  } else if (p.kind === 'manual' || p.kind === 'paste') {
    hint.textContent = 'You’ll copy the prompt into any assistant you already use and paste its reply back.';
    hint.hidden = false;
  } else {
    hint.hidden = true;
  }
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
    openPlan(done.result, true, 'generated');
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
      // From here on, "Changes" means changes since this push.
      state.baseline = structuredClone(state.plan.json);
      state.baselineLabel = 'the last push';
      renderChanges();
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
    const suffix = p.key_present === false ? ' (no key set)' : '';
    opt.textContent = (p.model ? `${p.name} — ${p.model}` : p.name) + suffix;
    if (p.name === s.default_provider) opt.selected = true;
    select.append(opt);
  }
  updateProviderHint();
  state.education = s.education || {};
  renderRecent(s.recent || []);
  renderChecklist(s);

  // Pre-fill setup so "Edit paces" is an edit, not a re-entry.
  $('#s-name').value = s.profile.name;
  $('#s-threshold').value = s.profile.threshold;
  if (s.profile.lthr) $('#s-lthr').value = s.profile.lthr;
  if (s.profile.hr_max) $('#s-hrmax').value = s.profile.hr_max;
  fillAthlete(s.athlete);
  $$('.seg[data-units]').forEach(b => {
    const on = b.dataset.units === state.units;
    b.classList.toggle('is-active', on);
    b.setAttribute('aria-checked', String(on));
    b.tabIndex = on ? 0 : -1;
  });
  // The threshold is known now, so "Edit profile" lands on a form that can be
  // saved as it is rather than one waiting for a race time.
  $('#tab-known').click();

  show('compose');
}

if ('serviceWorker' in navigator) {
  // Installable (#162). The worker caches static assets only; every number still comes from the server.
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}
window.addEventListener('beforeprint', () => $$('details.w-steps').forEach(d => { d.open = true; }));
initTheme();
initSetup();
initCompose();
initRelay();
initReview();
initPalette();
initPush();
initSettings();
boot().catch(err => {
  document.body.insertAdjacentHTML('afterbegin',
    `<p class="error" style="padding:1rem">Could not start: ${err.message}</p>`);
});
