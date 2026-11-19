// Data Logger phone app: live gauges, past drives with charts, status.
// Served by web.py from the Pi; talks to the HTTP API in PROTOCOL.md.
'use strict';

const $ = s => document.querySelector(s);
const esc = s => String(s).replace(/[&<>"']/g, c => `&#${c.charCodeAt(0)};`);

// Known readings, in display order. Thresholds use raw values (°C, V, ...).
const READINGS = [
  {key: 'rpm', label: 'RPM', dp: 0, max: 6500, warn: v => v > 6200},
  {key: 'speed_kmh', label: 'Speed', speed: true, dp: 0},
  {key: 'gear', label: 'Gear', dp: 0},
  {key: 'coolant_c', label: 'Coolant', temp: true, dp: 0, warn: v => v > 104, cold: v => v < 60},
  {key: 'afr', label: 'Air/fuel', dp: 1, warn: v => v < 10.5 || v > 16.5},
  {key: 'advance_deg', label: 'Timing', unit: '°', dp: 1},
  {key: 'knock_retard_deg', label: 'Knock retard', unit: '°', dp: 1, warn: v => v > 0},
  {key: 'dme_volts', label: 'Battery', unit: 'V', dp: 1, warn: v => v < 12.3 || v > 15},
  {key: 'intake_c', label: 'Intake air', temp: true, dp: 0},
  {key: 'injector_ms', label: 'Injector', unit: 'ms', dp: 2},
  {key: 'throttle', label: 'Throttle'},
  {key: 'o2_state', label: 'O2 sensor'},
  {key: 'idle_valve_pct', label: 'Idle valve', unit: '%', dp: 0},
  {key: 'airflow_gs', label: 'Airflow', unit: 'g/s', dp: 1},
  {key: 'lambda', label: 'Lambda', dp: 2},
  {key: 'o2_volts', label: 'O2 voltage', unit: 'V', dp: 2},
  {key: 'dwell_ms', label: 'Dwell', unit: 'ms', dp: 1},
  {key: 'baro_kpa', label: 'Barometer', unit: 'kPa', dp: 0},
  {key: 'fuel_pump', label: 'Fuel pump'},
  {key: 'ac', label: 'A/C'},
  {key: 'pi_temp_c', label: 'Pi temp', temp: true, dp: 0, warn: v => v > 75},
  // Counters and IDs: shown as numbers, not charted.
  {key: 'alt_m', label: 'Altitude', unit: 'm', dp: 0},
  {key: 'airflow_cfm', label: 'Airflow', unit: 'cfm', dp: 1, chart: false},
  {key: 'course_deg', label: 'Heading', unit: '°', dp: 0, chart: false},
  {key: 'gps_sats', label: 'GPS satellites', dp: 0, chart: false},
  {key: 'lat', label: 'Latitude', dp: 5, chart: false},
  {key: 'lon', label: 'Longitude', dp: 5, chart: false},
  {key: 'dme_resets', label: 'DME resets', dp: 0, chart: false},
  {key: 'rpm_sensor_errors', label: 'RPM sensor errors', dp: 0, chart: false},
  {key: 'mark_sensor_errors', label: 'Mark sensor errors', dp: 0, chart: false},
  {key: 'flywheel_teeth', label: 'Flywheel teeth', dp: 0, chart: false},
];
const META = Object.fromEntries(READINGS.map(r => [r.key, r]));
const ORDER = Object.fromEntries(READINGS.map((r, i) => [r.key, i]));
const LIVE_TILES = ['rpm', 'speed_kmh', 'gear', 'coolant_c', 'dme_volts', 'o2_volts', 'o2_state', 'airflow_gs',
  'advance_deg', 'injector_ms', 'intake_c', 'throttle', 'idle_valve_pct'];
const byOrder = (a, b) => (ORDER[a] ?? 999) - (ORDER[b] ?? 999) || a.localeCompare(b);

// ---- settings (per phone; fine if storage is unavailable) ----
function load(k, d) { try { const v = localStorage.getItem('dl.' + k); return v === null ? d : JSON.parse(v); } catch { return d; } }
function save(k, v) { try { localStorage.setItem('dl.' + k, JSON.stringify(v)); } catch {} }
let fahrenheit = load('fahrenheit', false);
let mph = load('mph', true);

// ---- formatting ----
const label = k => META[k]?.label || k.replace(/_/g, ' ');
const MILE_KM = 1.609344;
const unit = k => META[k]?.temp ? (fahrenheit ? '°F' : '°C') : META[k]?.speed ? (mph ? 'mph' : 'km/h') : META[k]?.unit || '';
function conv(k, v) {
  if (typeof v !== 'number') return v;
  if (META[k]?.temp && fahrenheit) return v * 9 / 5 + 32;
  if (META[k]?.speed && mph) return v / MILE_KM;
  return v;
}
function fmt(k, v) {
  if (v == null) return '–';
  if (typeof v !== 'number') return String(v);
  if (k === 'gear' && !v) return '-';   // not in a gear: stopped, clutch in, neutral
  const dp = META[k]?.dp ?? (Number.isInteger(v) ? 0 : 2);
  return conv(k, v).toFixed(dp);
}
function level(k, v) {
  const m = META[k];
  if (!m || typeof v !== 'number') return '';
  return m.warn?.(v) ? 'warn' : m.cold?.(v) ? 'cold' : '';
}
const clock = ts => new Date(ts * 1000).toLocaleTimeString([], {hour: 'numeric', minute: '2-digit', second: '2-digit'});
const day = ts => new Date(ts * 1000).toLocaleString([], {weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'});
// distance in the speed unit's miles / km (null = not worked out yet)
const dist = km => km == null ? '' : mph ? `${(km / MILE_KM).toFixed(1)} mi` : `${km.toFixed(1)} km`;

function dur(secs) {
  secs = Math.max(0, Math.round(secs));
  const h = Math.floor(secs / 3600), m = Math.floor(secs % 3600 / 60), s = secs % 60;
  return h ? `${h} h ${m} min` : m ? `${m} min` : `${s} s`;
}
const mmss = secs => `${Math.floor(secs / 60)}:${String(Math.floor(secs % 60)).padStart(2, '0')}`;

async function api(path) {
  const r = await fetch(path, {cache: 'no-store'});
  if (!r.ok) throw new Error(`${r.status} ${path}`);
  return r.json();
}

// ---- views ----
const views = ['live', 'drives', 'drive', 'status'];
let current = null;

function route() {
  const [view, arg] = (location.hash.slice(1) || 'live').split('/');
  const v = views.includes(view) ? view : 'live';
  for (const name of views) $('#v-' + name).hidden = name !== v;
  document.querySelectorAll('nav a').forEach(a =>
    a.classList.toggle('active', a.dataset.view === (v === 'drive' ? 'drives' : v)));
  current = v;
  if (v === 'drives') showDrives();
  if (v === 'drive') showDrive(Number(arg));
  if (v === 'status') showStatus();
  if (v === 'live') renderLive();
  window.scrollTo(0, 0);
}

// ---- live (server-sent events) ----
let latest = null, lastSeen = 0, source = null, connected = false;

function connect() {
  if (source) source.close();
  source = new EventSource('/api/live');
  source.onopen = () => { connected = true; header(); };
  source.onmessage = e => {
    latest = JSON.parse(e.data);
    lastSeen = Date.now();
    header();
    if (current === 'live') renderLive();
  };
  source.onerror = () => { connected = false; header(); };
}

function header() {
  const dot = $('#conn'), st = $('#state');
  dot.className = 'dot ' + (connected ? 'on' : 'off');
  if (!connected) { st.textContent = 'not connected'; return; }
  if (!latest) { st.textContent = 'connected'; return; }
  st.textContent = engineOn() ? `engine on · drive ${latest.session}` : 'engine off';
}

const engineOn = () => latest && latest.session != null && Date.now() - lastSeen < 5000;

function renderLive() {
  const banner = $('#banner'), d = latest?.data || {};
  const on = engineOn();
  if (!latest) {
    banner.hidden = false;
    banner.textContent = connected ? 'Waiting for data…' : 'Can’t reach the logger. Check you’re on the same Wi-Fi.';
  } else if (!on) {
    banner.hidden = false;
    banner.textContent = `Engine off. Last check-in ${clock(latest.ts)}. Gauges come alive when the DME starts sending data.`;
  } else {
    banner.hidden = true;
  }

  $('#tiles').innerHTML = LIVE_TILES.map(k => {
    const v = on ? d[k] : undefined;
    const cls = ['tile', k === 'rpm' ? 'big' : '', typeof v === 'string' ? 'text' : '',
      on ? level(k, v) : 'stale'].join(' ');
    const u = unit(k) && typeof v !== 'string' ? `<small>${unit(k)}</small>` : '';
    const bar = k === 'rpm' ? `<div class="bar"><i class="${level(k, v)}" style="width:${
      Math.min(100, (v || 0) / META.rpm.max * 100)}%"></i></div>` : '';
    return `<div class="${cls}"><div class="k">${label(k)}</div><div class="v">${esc(fmt(k, v))}${u}</div>${bar}</div>`;
  }).join('');

  const extra = Object.keys(d).filter(k => !LIVE_TILES.includes(k)).sort(byOrder);
  $('#extra-h').hidden = !extra.length;
  $('#extra').innerHTML = extra.map(k =>
    `<tr class="${level(k, d[k])}"><td>${esc(label(k))}</td><td>${esc(fmt(k, d[k]))} ${unit(k)}</td></tr>`).join('');
}

// ---- drives list ----
let drivesOldest = null, liveSession = null;

async function showDrives(more = false) {
  try {
    if (!more) {
      liveSession = (await api('/api/info')).session;
      drivesOldest = null;
      $('#drives').innerHTML = '';
    }
    const list = await api('/api/sessions?limit=30' + (drivesOldest ? '&before=' + drivesOldest : ''));
    if (list.length) drivesOldest = list[list.length - 1].id;
    $('#drives').insertAdjacentHTML('beforeend', list.map(s => `<li><a href="#drive/${s.id}">
      <span><span class="when">${day(s.start)}</span>${s.id === liveSession ? '<span class="live-tag">● now</span>' : ''}</span>
      <span class="meta"><b class="dist">${dist(s.km)}</b> ${dur(s.end - s.start)}<br>${s.samples.toLocaleString()} samples</span></a></li>`).join(''));
    $('#more').hidden = list.length < 30;
    $('#drives-empty').hidden = !!$('#drives').children.length;
  } catch (e) {
    $('#drives-empty').hidden = false;
    $('#drives-empty').textContent = 'Couldn’t load drives: ' + e.message;
  }
}

// ---- one drive: stats + charts ----
let charts = [], cursorT = null, driveT0 = 0;

async function showDrive(id) {
  $('#d-title').textContent = `Drive ${id}`;
  $('#d-sub').textContent = 'Loading…';
  $('#d-stats').innerHTML = $('#d-charts').innerHTML = '';
  $('#d-csv').href = `/api/samples.csv?session=${id}&limit=300000&download=1`;
  charts = []; cursorT = null;
  $('#d-cursor').textContent = 'Drag across a chart to read values';

  const rows = [];
  try {
    for (let since = 0; ; ) {
      const page = await api(`/api/samples?session=${id}&since_id=${since}&limit=5000`);
      if (!page.length || current !== 'drive') break;
      rows.push(...page);
      since = page[page.length - 1].id;
      $('#d-sub').textContent = `Loading… ${rows.length.toLocaleString()} samples`;
      if (page.length < 5000) break;
    }
  } catch (e) {
    $('#d-sub').textContent = 'Couldn’t load this drive: ' + e.message;
    return;
  }
  if (current !== 'drive') return;
  if (!rows.length) { $('#d-sub').textContent = 'No samples in this drive.'; return; }

  const t = rows.map(r => r.ts);
  driveT0 = t[0];
  $('#d-title').textContent = day(t[0]);
  $('#d-sub').textContent = `Drive ${id} · ${dur(t[t.length - 1] - t[0])} · ${rows.length.toLocaleString()} samples`;

  // numeric series per reading
  const keys = new Set();
  rows.forEach(r => Object.entries(r.data).forEach(([k, v]) => { if (typeof v === 'number') keys.add(k); }));
  const series = {};
  for (const k of keys) series[k] = Float64Array.from(rows, r => typeof r.data[k] === 'number' ? r.data[k] : NaN);

  $('#d-stats').innerHTML = driveStats(series, rows);

  const mirrored = series.knock_retard_deg && series.advance_deg &&
    series.knock_retard_deg.every((v, i) => isNaN(v) || Math.abs(v - series.advance_deg[i]) <= 0.1);
  const chartKeys = [...keys].filter(k => META[k]?.chart !== false && !(k === 'knock_retard_deg' && mirrored))
    .sort(byOrder);
  $('#d-charts').innerHTML = chartKeys.map((k, i) =>
    `<div class="chart"><div class="head"><b id="ch-v${i}">${esc(label(k))}</b><span id="ch-r${i}"></span></div><canvas id="ch-c${i}"></canvas></div>`).join('');
  charts = chartKeys.map((k, i) => {
    const y = series[k].map(v => conv(k, v));
    const c = {key: k, t, y, canvas: $('#ch-c' + i), value: $('#ch-v' + i), range: $('#ch-r' + i)};
    let lo = Infinity, hi = -Infinity;
    for (const v of y) if (!isNaN(v)) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
    c.lo = lo; c.hi = hi;
    c.range.textContent = lo === Infinity ? '' : `${fmt(k, back(k, lo))}–${fmt(k, back(k, hi))} ${unit(k)}`;
    hookScrub(c.canvas);
    return c;
  });
  drawCharts();
}

// undo °F / mph conversion so fmt() can redo it (fmt always takes raw values)
const back = (k, v) => META[k]?.temp && fahrenheit ? (v - 32) * 5 / 9 : META[k]?.speed && mph ? v * MILE_KM : v;

function driveStats(s, rows) {
  const max = k => s[k] ? s[k].reduce((a, v) => isNaN(v) ? a : Math.max(a, v), -Infinity) : null;
  const min = k => s[k] ? s[k].reduce((a, v) => isNaN(v) ? a : Math.min(a, v), Infinity) : null;
  // Drives logged before 2026-10-14's fix copied timing into knock_retard_deg on
  // cars without knock control; ignore it when it just mirrors the advance.
  const realKnock = s.knock_retard_deg && s.advance_deg &&
    s.knock_retard_deg.some((v, i) => Math.abs(v - s.advance_deg[i]) > 0.1);
  const knockCount = realKnock ? s.knock_retard_deg.filter(v => v > 0).length : null;
  const items = [
    ['Top RPM', s.rpm && fmt('rpm', max('rpm')), level('rpm', max('rpm'))],
    ['Hottest coolant', s.coolant_c && `${fmt('coolant_c', max('coolant_c'))} ${unit('coolant_c')}`, level('coolant_c', max('coolant_c'))],
    ['Air/fuel range', s.afr && `${fmt('afr', min('afr'))}–${fmt('afr', max('afr'))}`, ''],
    ['Knock retard', knockCount != null && (knockCount ? `${fmt('knock_retard_deg', max('knock_retard_deg'))}° max, ${(knockCount / rows.length * 100).toFixed(1)}%` : 'none'), knockCount ? 'warn' : ''],
    ['Lowest battery', s.dme_volts && `${fmt('dme_volts', min('dme_volts'))} V`, level('dme_volts', min('dme_volts'))],
  ];
  return items.filter(i => i[1]).map(([k, v, c]) => `<div class="${c}"><b>${esc(v)}</b><span>${k}</span></div>`).join('');
}

function hookScrub(canvas) {
  const move = e => {
    const c = charts.find(c => c.canvas === canvas);
    const r = canvas.getBoundingClientRect();
    const f = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
    cursorT = c.t[0] + f * (c.t[c.t.length - 1] - c.t[0]);
    drawCharts();
  };
  canvas.addEventListener('pointerdown', move);
  canvas.addEventListener('pointermove', e => { if (e.buttons || e.pointerType === 'touch') move(e); });
}

function nearest(t, x) {          // index of the sample closest to time x
  let lo = 0, hi = t.length - 1;
  while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (t[mid] < x) lo = mid; else hi = mid; }
  return Math.abs(t[hi] - x) < Math.abs(t[lo] - x) ? hi : lo;
}

function drawCharts() {
  if (cursorT != null)
    $('#d-cursor').textContent = `${clock(cursorT)} · ${mmss(cursorT - driveT0)} into the drive`;
  for (const c of charts) {
    drawChart(c);
    const name = label(c.key);
    if (cursorT == null) { c.value.textContent = name; continue; }
    const v = c.y[nearest(c.t, cursorT)];
    c.value.textContent = isNaN(v) ? `${name} –` : `${name} ${fmt(c.key, back(c.key, v))} ${unit(c.key)}`;
  }
}

function drawChart(c) {
  const cv = c.canvas, dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth, h = cv.clientHeight;
  if (!w) return;
  cv.width = w * dpr; cv.height = h * dpr;
  const g = cv.getContext('2d');
  g.scale(dpr, dpr);
  const css = getComputedStyle(document.documentElement);
  const color = n => css.getPropertyValue(n).trim();
  if (c.lo === Infinity) return;

  let lo = c.lo, hi = c.hi;
  if (hi - lo < 1e-9) { lo -= 1; hi += 1; }
  const pad = (hi - lo) * 0.08; lo -= pad; hi += pad;
  const t0 = c.t[0], span = (c.t[c.t.length - 1] - t0) || 1;
  const X = t => (t - t0) / span * w, Y = v => h - (v - lo) / (hi - lo) * h;

  // warning threshold band: shade where values would be flagged
  const m = META[c.key];
  if (m?.warn) {
    g.fillStyle = 'rgba(255,93,82,0.10)';
    for (let py = 0; py < h; py += 2) if (m.warn(back(c.key, lo + (h - py) / h * (hi - lo)))) g.fillRect(0, py, w, 2);
  }

  // one min/max column per pixel so long drives stay fast and spikes stay visible
  g.strokeStyle = color('--accent'); g.lineWidth = 1.5; g.lineJoin = 'round';
  g.beginPath();
  let col = -1, mn = 0, mx = 0, pen = false;
  const flush = () => {
    if (col < 0) return;
    if (pen) g.lineTo(col, Y(mn)); else { g.moveTo(col, Y(mn)); pen = true; }
    if (mx !== mn) g.lineTo(col, Y(mx));
  };
  for (let i = 0; i < c.t.length; i++) {
    const v = c.y[i];
    if (isNaN(v)) { flush(); col = -1; pen = false; continue; }
    const x = Math.round(X(c.t[i]));
    if (x !== col) { flush(); col = x; mn = mx = v; } else { mn = Math.min(mn, v); mx = Math.max(mx, v); }
  }
  flush();
  g.stroke();

  if (cursorT != null) {
    g.strokeStyle = color('--text'); g.lineWidth = 1;
    g.beginPath(); g.moveTo(X(cursorT), 0); g.lineTo(X(cursorT), h); g.stroke();
  }
}

// ---- status ----
async function showStatus() {
  $('#opt-f').checked = fahrenheit;
  $('#opt-mph').checked = mph;
  try {
    const i = await api('/api/info');
    const rows = [
      ['Logger', `${i.name} v${i.version}`],
      ['DME', i.dme_connected ? 'connected' : 'not connected'],
      ['GPS', i.gps_fix ? 'fix' : i.gps_connected ? 'no fix yet' : 'not connected'],
      ['Engine', i.engine_on ? `on (drive ${i.session})` : 'off'],
      ['Pi clock', new Date(i.time * 1000).toLocaleString()],
      ['Up for', dur(i.uptime)],
      ['Address', i.ip.join(', ') || '–'],
      ['Pi temperature', `${fmt('pi_temp_c', i.pi_temp_c)} ${unit('pi_temp_c')}`],
      ['Samples stored', i.samples.toLocaleString()],
      ['Drives', i.sessions.toLocaleString()],
      ['Sample rate', `${i.sample_hz} per second`],
    ];
    const skew = Math.abs(Date.now() / 1000 - i.time);
    if (skew > 120) rows.push(['⚠ Clock', `Pi is ${dur(skew)} off from this phone`]);
    $('#info').innerHTML = rows.map(([k, v]) => `<tr><td>${k}</td><td>${esc(v)}</td></tr>`).join('');
  } catch (e) {
    $('#info').innerHTML = `<tr><td>Couldn’t reach the logger</td><td>${esc(e.message)}</td></tr>`;
  }
}

$('#opt-f').addEventListener('change', e => {
  fahrenheit = e.target.checked;
  save('fahrenheit', fahrenheit);
  showStatus();
});
$('#opt-mph').addEventListener('change', e => {
  mph = e.target.checked;
  save('mph', mph);
  showStatus();
});
$('#more').addEventListener('click', () => showDrives(true));
window.addEventListener('hashchange', route);
window.addEventListener('resize', () => { if (current === 'drive') drawCharts(); });
// phones drop the live stream when the screen locks; reopen it on return
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && (!source || source.readyState === EventSource.CLOSED)) connect();
});
setInterval(() => { header(); if (current === 'live' && latest && !engineOn()) renderLive(); }, 2000);

connect();
route();
