// live.js — polls api.spark.wedd.au every 30s; falls back to localStorage
(function () {
  const API = 'https://spark-api.wedd.au/api/v1/public';
  const CACHE_KEY = 'spark_last_known';
  const POLL_MS = 30_000;
  const TIMEOUT_MS = 5_000;

  // DOM refs
  const moodBubble   = document.getElementById('mood-bubble');
  const lastThought  = document.getElementById('last-thought');
  const liveMood     = document.getElementById('live-mood');
  const liveThought  = document.getElementById('live-thought');
  const liveTs       = document.getElementById('live-ts');
  const cpuVal       = document.getElementById('cpu-val');
  const ramVal       = document.getElementById('ram-val');
  const battVal      = document.getElementById('batt-val');
  const sonarVal     = document.getElementById('sonar-val');
  const cardCpu      = document.getElementById('card-cpu');
  const cardRam      = document.getElementById('card-ram');
  const cardBatt     = document.getElementById('card-batt');
  const lastUpdated  = document.getElementById('last-updated');
  const offlineBanner= document.getElementById('offline-banner');
  const offlineTs    = document.getElementById('offline-ts');
  const statusDot    = document.getElementById('status-dot');

  let lastSuccessMs = null;

  // ── Threshold helpers (class-based — no inline styles) ──────────────────
  function setThreshold(card, val, warnAt, critAt) {
    card.classList.remove('ok', 'warn', 'crit');
    if (val === null) return;
    if (val >= critAt) card.classList.add('crit');
    else if (val >= warnAt) card.classList.add('warn');
    else card.classList.add('ok');
  }

  // ── UI update ────────────────────────────────────────────────────────────
  function applyStatus(data) {
    const mood = data.mood || '—';
    if (moodBubble) moodBubble.textContent = mood;
    if (lastThought) lastThought.textContent = data.last_thought || 'Nothing on my mind just now…';
    if (liveMood) liveMood.textContent = mood;
    if (liveThought) liveThought.textContent = data.last_thought || '—';
    if (liveTs && data.ts) liveTs.textContent = new Date(data.ts).toLocaleString('en-AU');
  }

  function applyVitals(data) {
    const fmt = v => v !== null && v !== undefined ? v + '%' : '—';
    const fmtTemp = v => v !== null && v !== undefined ? v + '°C' : '—';

    if (cpuVal) cpuVal.textContent = fmt(data.cpu_pct);
    if (ramVal) ramVal.textContent = fmt(data.ram_pct);
    if (battVal) battVal.textContent = fmt(data.battery_pct);

    setThreshold(cardCpu,  data.cpu_pct,       70, 90);
    setThreshold(cardRam,  data.ram_pct,       75, 90);
    setThreshold(cardBatt, data.battery_pct !== null ? 100 - data.battery_pct : null, 70, 85);
  }

  function applySonar(data) {
    if (!sonarVal) return;
    if (data.source === 'unavailable' || data.sonar_cm === null) {
      sonarVal.textContent = '—';
    } else {
      sonarVal.textContent = data.sonar_cm.toFixed(0) + ' cm';
    }
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

  // ── Dot state ─────────────────────────────────────────────────────────────
  function updateDot() {
    if (!statusDot) return;
    statusDot.classList.remove('green', 'amber', 'red');
    if (lastSuccessMs === null) {
      statusDot.classList.add('red');
      return;
    }
    const age = Date.now() - lastSuccessMs;
    if (age < 60_000) statusDot.classList.add('green');
    else if (age < 300_000) statusDot.classList.add('amber');
    else statusDot.classList.add('red');
  }

  // ── Poll cycle ────────────────────────────────────────────────────────────
  async function poll() {
    try {
      const [status, vitals, sonar] = await Promise.all([
        fetchWithTimeout(API + '/status'),
        fetchWithTimeout(API + '/vitals'),
        fetchWithTimeout(API + '/sonar'),
      ]);

      applyStatus(status);
      applyVitals(vitals);
      applySonar(sonar);

      lastSuccessMs = Date.now();

      // Cache
      localStorage.setItem(CACHE_KEY, JSON.stringify({ status, vitals, sonar, fetchedAt: new Date().toISOString() }));

      // Hide offline banner
      if (offlineBanner) offlineBanner.classList.add('hidden');
      if (lastUpdated) lastUpdated.textContent = 'Updated just now';

    } catch (_err) {
      // Load from cache
      const raw = localStorage.getItem(CACHE_KEY);
      if (raw) {
        const cached = JSON.parse(raw);
        applyStatus(cached.status || {});
        applyVitals(cached.vitals || {});
        applySonar(cached.sonar || {});

        if (offlineBanner) offlineBanner.classList.remove('hidden');
        if (offlineTs) {
          offlineTs.textContent = new Date(cached.fetchedAt).toLocaleString('en-AU');
        }
        if (lastUpdated) lastUpdated.textContent = 'Using cached data';
      } else {
        if (lastUpdated) lastUpdated.textContent = 'Pi unreachable — no cached data';
      }
    }

    updateDot();
  }

  // ── Start ─────────────────────────────────────────────────────────────────
  poll();
  setInterval(poll, POLL_MS);
  setInterval(updateDot, 10_000); // keep dot fresh between polls

})();
