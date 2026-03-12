// live.js — polling orchestrator for the three-band live dashboard.
// Depends on: charts.js (SparkCharts), dashboard.js (SparkDashboard).
// All fetch URLs are absolute (CSP connect-src requires https://spark-api.wedd.au).
(function () {
  'use strict';

  const API         = 'https://spark-api.wedd.au/api/v1/public';
  const CACHE_KEY   = 'spark_last_known';
  const HISTORY_KEY = 'spark_history';
  const HISTORY_MAX = 120;   // 120 × 30s = 60 min local buffer
  const POLL_MS     = 30_000;
  const TIMEOUT_MS  = 5_000;

  let state = {};
  let lastSuccessMs = null;
  let _openSparklineTile = null;

  // ── localStorage helpers ─────────────────────────────────────────────────

  function loadHistory() {
    try { return JSON.parse(localStorage.getItem(HISTORY_KEY)) || []; }
    catch (_) { return []; }
  }

  function saveHistory(arr) {
    try { localStorage.setItem(HISTORY_KEY, JSON.stringify(arr)); }
    catch (_) {}
  }

  function accumulate(reading) {
    let hist = loadHistory();
    // Dedup by ts — skip if this exact ts already in history
    if (hist.some(e => e.ts === reading.ts)) return;
    hist.push(reading);
    if (hist.length > HISTORY_MAX) hist = hist.slice(-HISTORY_MAX);
    saveHistory(hist);
  }

  // ── Fetch with timeout ───────────────────────────────────────────────────

  async function fetchWithTimeout(url) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
    try {
      const resp = await fetch(url, { signal: ctrl.signal });
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      return resp.json();
    } finally {
      clearTimeout(timer);
    }
  }

  // ── Poll cycle ────────────────────────────────────────────────────────────

  async function poll() {
    const [statusR, vitalsR, sonarR, awarenessR, servicesR] = await Promise.allSettled([
      fetchWithTimeout(API + '/status'),
      fetchWithTimeout(API + '/vitals'),
      fetchWithTimeout(API + '/sonar'),
      fetchWithTimeout(API + '/awareness'),
      fetchWithTimeout(API + '/services'),
    ]);

    let anySuccess = false;

    if (statusR.status === 'fulfilled') {
      Object.assign(state, statusR.value);
      anySuccess = true;
    }
    if (vitalsR.status === 'fulfilled') {
      Object.assign(state, vitalsR.value);
      anySuccess = true;
    }
    if (sonarR.status === 'fulfilled') {
      state.sonar_cm = sonarR.value.sonar_cm != null ? sonarR.value.sonar_cm : null;
      anySuccess = true;
    }
    if (awarenessR.status === 'fulfilled') {
      const a = awarenessR.value;
      state.obi_mode             = a.obi_mode;
      state.person_present       = a.person_present;
      state.frigate_score        = a.frigate_score;
      state.ambient_level        = a.ambient_level;
      state.ambient_rms          = a.ambient_rms;
      state.weather              = a.weather;
      state.minutes_since_speech = a.minutes_since_speech;
      state.time_period          = a.time_period;
      anySuccess = true;
    }
    if (servicesR.status === 'fulfilled') {
      state.services = servicesR.value;
      anySuccess = true;
    }

    if (anySuccess) {
      lastSuccessMs = Date.now();
      accumulate({
        ts:          state.ts || new Date().toISOString(),
        cpu_pct:     state.cpu_pct     != null ? state.cpu_pct     : null,
        cpu_temp_c:  state.cpu_temp_c  != null ? state.cpu_temp_c  : null,
        ram_pct:     state.ram_pct     != null ? state.ram_pct     : null,
        battery_pct: state.battery_pct != null ? state.battery_pct : null,
        sonar_cm:    state.sonar_cm    != null ? state.sonar_cm    : null,
        ambient_rms: state.ambient_rms != null ? state.ambient_rms : null,
      });
      try {
        localStorage.setItem(CACHE_KEY, JSON.stringify(
          Object.assign({}, state, { fetchedAt: new Date().toISOString() })
        ));
      } catch (_) {}
      SparkDashboard.setOnline(true, null);
      SparkDashboard.setLastUpdated('Updated just now');
    } else {
      // All failed — fall back to cache
      const raw = localStorage.getItem(CACHE_KEY);
      if (raw) {
        try {
          const cached = JSON.parse(raw);
          Object.assign(state, cached);
          SparkDashboard.setOnline(false, cached.fetchedAt);
          SparkDashboard.setLastUpdated('Using cached data');
        } catch (_) {}
      } else {
        SparkDashboard.setOnline(false, null);
        SparkDashboard.setLastUpdated('Pi unreachable — no cached data');
      }
    }

    _updateDot();
    renderAll();
  }

  function renderAll() {
    SparkDashboard.renderPresence(state);
    SparkDashboard.renderWorld(state);
    SparkDashboard.renderMachine(state);
  }

  // ── Status dot ───────────────────────────────────────────────────────────

  function _updateDot() {
    const dot = document.getElementById('status-dot');
    if (!dot) return;
    dot.classList.remove('green', 'amber', 'red');
    if (lastSuccessMs === null) { dot.classList.add('red'); return; }
    const age = Date.now() - lastSuccessMs;
    dot.classList.add(age < 60_000 ? 'green' : age < 300_000 ? 'amber' : 'red');
  }

  // ── Waveform 2s tick ─────────────────────────────────────────────────────

  function tickWaveform() {
    const canvas = document.getElementById('waveform-canvas');
    if (canvas) SparkCharts.drawWaveform(canvas, state.ambient_rms || 0);
  }

  // ── Sparklines ───────────────────────────────────────────────────────────

  function _mergeHistory(remote) {
    const local = loadHistory();
    const byTs = {};
    for (const e of [...local, ...remote]) byTs[e.ts] = e;
    return Object.values(byTs).sort((a, b) => a.ts < b.ts ? -1 : 1);
  }

  async function openSparkline(tile) {
    const field = tile.dataset.sparkline;
    if (!field) return;

    if (_openSparklineTile === tile.id) {
      _closeSparkline(tile);
      return;
    }
    if (_openSparklineTile) {
      const prev = document.getElementById(_openSparklineTile);
      if (prev) _closeSparkline(prev);
    }
    _openSparklineTile = tile.id;

    let remote = [];
    try { remote = await fetchWithTimeout(API + '/history'); } catch (_) {}

    const points = _mergeHistory(remote).slice(-60);

    let wrap = tile.querySelector('.sparkline-wrap');
    if (!wrap) {
      wrap = document.createElement('div');
      wrap.className = 'sparkline-wrap';

      const canvas = document.createElement('canvas');
      canvas.className = 'sparkline-canvas';
      canvas.width = (tile.offsetWidth || 160) - 20;
      canvas.height = 40;

      const lbl = document.createElement('div');
      lbl.className = 'sparkline-label';

      wrap.appendChild(canvas);
      wrap.appendChild(lbl);
      tile.appendChild(wrap);
    }

    const canvas = wrap.querySelector('canvas');
    const lbl = wrap.querySelector('.sparkline-label');

    if (points.length < 2) {
      lbl.textContent = 'no history yet';
      if (canvas) canvas.getContext('2d').clearRect(0, 0, canvas.width, canvas.height);
    } else {
      SparkCharts.drawSparkline(canvas, points, field);
      lbl.textContent = 'last ' + Math.round(points.length * 0.5) + ' min';
    }
  }

  function _closeSparkline(tile) {
    const wrap = tile.querySelector('.sparkline-wrap');
    if (wrap) wrap.parentNode.removeChild(wrap);
    if (_openSparklineTile === tile.id) _openSparklineTile = null;
  }

  function initSparklines() {
    document.querySelectorAll('[data-sparkline]').forEach(tile => {
      tile.addEventListener('click', () => openSparkline(tile));
    });
  }

  // ── Hydrate from cache (zero-flash on load) ───────────────────────────────

  function hydrateFromCache() {
    const raw = localStorage.getItem(CACHE_KEY);
    if (!raw) return;
    try { Object.assign(state, JSON.parse(raw)); renderAll(); }
    catch (_) {}
  }

  // ── Init ─────────────────────────────────────────────────────────────────

  SparkDashboard.initToggle();
  initSparklines();
  hydrateFromCache();
  poll();
  setInterval(poll, POLL_MS);
  setInterval(tickWaveform, 2_000);
  setInterval(_updateDot, 10_000);

})();
