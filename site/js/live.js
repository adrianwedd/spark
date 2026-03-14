// live.js — polling orchestrator for the three-band live dashboard.
// Depends on: charts.js (SparkCharts), dashboard.js (SparkDashboard).
// All fetch URLs are absolute (CSP connect-src requires https://spark-api.wedd.au).
(function () {
  'use strict';

  const API              = 'https://spark-api.wedd.au/api/v1/public';
  const CACHE_KEY        = 'spark_last_known';
  const HISTORY_KEY      = 'spark_history';
  const HISTORY_MAX      = 2880;  // 2880 × 30s = 24 h local buffer
  const POLL_MS          = 30_000;
  const TIMEOUT_MS       = 5_000;
  const THOUGHTS_POLL_MS = 5 * 60_000;  // refresh carousel every 5 min

  // mood → numeric value for sparkline charting
  const MOOD_VAL = { peaceful: 1, content: 2, contemplative: 2, curious: 3, active: 4, excited: 5 };

  let state = {};
  let lastSuccessMs = null;

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
      state.detections           = a.detections;
      state.ha_presence          = a.ha_presence;
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
        ts:              state.ts || new Date().toISOString(),
        cpu_pct:         state.cpu_pct         != null ? state.cpu_pct         : null,
        cpu_temp_c:      state.cpu_temp_c      != null ? state.cpu_temp_c      : null,
        ram_pct:         state.ram_pct         != null ? state.ram_pct         : null,
        disk_pct:        state.disk_pct        != null ? state.disk_pct        : null,
        battery_pct:     state.battery_pct     != null ? state.battery_pct     : null,
        sonar_cm:        state.sonar_cm        != null ? state.sonar_cm        : null,
        ambient_rms:     state.ambient_rms     != null ? state.ambient_rms     : null,
        tokens_in:       state.tokens_in       != null ? state.tokens_in       : null,
        tokens_out:      state.tokens_out      != null ? state.tokens_out      : null,
        weather_temp_c:  state.weather?.temp_c  != null ? state.weather.temp_c  : null,
        wind_kmh:        state.weather?.wind_kmh != null ? state.weather.wind_kmh : null,
        humidity_pct:    state.weather?.humidity_pct != null ? state.weather.humidity_pct : null,
        salience:        state.salience      != null ? state.salience      : null,
        mood_val:        state.mood ? (MOOD_VAL[(state.mood || '').toLowerCase()] || null) : null,
        mood:            state.mood ? state.mood.toLowerCase() : null,
        wifi_dbm:        state.wifi_dbm      != null ? state.wifi_dbm      : null,
        rain_24h_mm:     state.weather?.rain_24h_mm != null ? state.weather.rain_24h_mm : null,
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
    SparkDashboard.renderSparklines(loadHistory());
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
    if (!canvas) return;
    // Re-measure width from CSS layout on each tick (handles resize/orientation)
    var cssW = canvas.offsetWidth || canvas.parentElement?.offsetWidth || 160;
    if (cssW > 10) canvas.width = cssW;
    SparkCharts.drawWaveform(canvas, state.ambient_rms || 0);
  }

  // ── Hydrate from cache (zero-flash on load) ───────────────────────────────

  function hydrateFromCache() {
    const raw = localStorage.getItem(CACHE_KEY);
    if (!raw) return;
    try { Object.assign(state, JSON.parse(raw)); renderAll(); }
    catch (_) {}
  }

  // ── Prefetch history on load (spec: once on page load + on sparkline open) ─

  async function prefetchHistory() {
    try {
      const remote = await fetchWithTimeout(API + '/history');
      if (Array.isArray(remote)) remote.forEach(accumulate);
    } catch (_) {}
  }

  // ── Thoughts carousel ────────────────────────────────────────────────────

  let _carouselTimer = null;
  let _carouselIdx   = 0;

  async function fetchThoughts() {
    try {
      const thoughts = await fetchWithTimeout(API + '/thoughts?limit=20');
      if (Array.isArray(thoughts) && thoughts.length) _buildCarousel(thoughts);
    } catch (_) {}
  }

  function _buildCarousel(thoughts) {
    const container = document.getElementById('thought-carousel');
    if (!container) return;
    // API returns newest-first — keep that order
    while (container.firstChild) container.removeChild(container.firstChild);

    const MOOD_COLOR = SparkDashboard.MOOD_FAVICON_COLOR || {};

    thoughts.forEach((t, i) => {
      const slide = document.createElement('div');
      slide.className = 'carousel-slide' + (i === 0 ? ' active' : '');

      // Meta row ABOVE quote: time · mood badge · salience dots
      const meta = document.createElement('p');
      meta.className = 'carousel-meta';

      if (t.ts) {
        const d = new Date(t.ts);
        const timeStr = d.toLocaleTimeString('en-AU', {
          hour: '2-digit', minute: '2-digit', timeZone: 'Australia/Hobart',
        });
        meta.appendChild(document.createTextNode(timeStr));
      }

      if (t.mood) {
        const badge = document.createElement('span');
        badge.className = 'carousel-mood-badge';
        badge.dataset.mood = t.mood.toLowerCase();
        badge.textContent = t.mood;
        meta.appendChild(badge);
      }

      if (typeof t.salience === 'number') {
        const dots = document.createElement('span');
        dots.className = 'carousel-salience';
        const tenths = Math.round(t.salience * 10);  // 0–10
        const full = Math.floor(tenths / 2);
        const half = tenths % 2;
        const empty = 5 - full - half;
        dots.textContent = '●'.repeat(full) + (half ? '◐' : '') + '○'.repeat(empty);
        meta.appendChild(dots);
      }

      slide.appendChild(meta);

      const q = document.createElement('blockquote');
      q.className = 'carousel-quote';
      q.textContent = t.thought || '';
      slide.appendChild(q);
      container.appendChild(slide);
    });

    // Rebuild dots
    const dots = document.getElementById('carousel-dots');
    if (dots) {
      while (dots.firstChild) dots.removeChild(dots.firstChild);
      thoughts.forEach((_, i) => {
        const d = document.createElement('button');
        d.className = 'carousel-dot' + (i === 0 ? ' active' : '');
        d.setAttribute('aria-label', 'Thought ' + (i + 1));
        d.addEventListener('click', () => { _carouselIdx = i; _showSlide(i); });
        dots.appendChild(d);
      });
    }

    _carouselIdx = 0;
    _attachCarouselInteraction(container, thoughts.length);
    _startCarousel(thoughts.length);
  }

  function _showSlide(idx) {
    const container = document.getElementById('thought-carousel');
    if (!container) return;
    container.querySelectorAll('.carousel-slide').forEach((s, i) =>
      s.classList.toggle('active', i === idx));
    const dots = document.getElementById('carousel-dots');
    if (dots) dots.querySelectorAll('.carousel-dot').forEach((d, i) =>
      d.classList.toggle('active', i === idx));
  }

  let _swipeStartX = null;

  function _attachCarouselInteraction(container, count) {
    // Remove previous listeners by replacing the node with a clone
    const fresh = container.cloneNode(true);
    container.parentNode.replaceChild(fresh, container);

    // Re-attach dot click listeners on the rebuilt dots (still in DOM)
    const dots = document.getElementById('carousel-dots');
    if (dots) {
      dots.querySelectorAll('.carousel-dot').forEach((d, i) => {
        d.addEventListener('click', (e) => { e.stopPropagation(); _carouselIdx = i; _showSlide(i); _resetCarousel(count); });
      });
    }

    // Click anywhere on carousel to advance
    fresh.addEventListener('click', () => {
      _carouselIdx = (_carouselIdx + 1) % count;
      _showSlide(_carouselIdx);
      _resetCarousel(count);
    });

    // Touch swipe: left = next, right = prev
    fresh.addEventListener('touchstart', (e) => {
      _swipeStartX = e.changedTouches[0].clientX;
    }, { passive: true });
    fresh.addEventListener('touchend', (e) => {
      if (_swipeStartX == null) return;
      const dx = e.changedTouches[0].clientX - _swipeStartX;
      _swipeStartX = null;
      if (Math.abs(dx) < 30) return; // too small — treat as tap
      if (dx < 0) { _carouselIdx = (_carouselIdx + 1) % count; }
      else         { _carouselIdx = (_carouselIdx - 1 + count) % count; }
      _showSlide(_carouselIdx);
      _resetCarousel(count);
    }, { passive: true });
  }

  function _resetCarousel(count) {
    if (_carouselTimer) clearInterval(_carouselTimer);
    _startCarousel(count);
  }

  function _startCarousel(count) {
    if (_carouselTimer) clearInterval(_carouselTimer);
    if (count > 1) {
      _carouselTimer = setInterval(() => {
        _carouselIdx = (_carouselIdx + 1) % count;
        _showSlide(_carouselIdx);
      }, 10_000);
    }
  }

  // ── Init ─────────────────────────────────────────────────────────────────

  hydrateFromCache();
  prefetchHistory();
  poll();
  fetchThoughts();
  setInterval(poll, POLL_MS);
  setInterval(tickWaveform, 2_000);
  setInterval(_updateDot, 10_000);
  setInterval(fetchThoughts, THOUGHTS_POLL_MS);

})();
