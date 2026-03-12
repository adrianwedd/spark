// dashboard.js — DOM update functions for the three-band live dashboard.
// Depends on: charts.js (SparkCharts). Exposes: window.SparkDashboard.
window.SparkDashboard = (function () {
  'use strict';

  const $ = id => document.getElementById(id);

  // ── Safe inline markdown ──────────────────────────────────────────────────
  // Renders *italic* spans via DOM construction — never innerHTML with server data.
  function _renderInline(el, text) {
    if (!el) return;
    while (el.firstChild) el.removeChild(el.firstChild);
    if (!text) return;
    const parts = text.split(/\*([^*]+)\*/);
    parts.forEach((part, i) => {
      if (i % 2 === 1) {
        const em = document.createElement('em');
        em.textContent = part;
        el.appendChild(em);
      } else if (part) {
        el.appendChild(document.createTextNode(part));
      }
    });
  }

  // ── Presence band ────────────────────────────────────────────────────────

  const PULSE_CLASSES = {
    peaceful: 'pulse-slow', content: 'pulse-slow',
    curious: 'pulse-mid', contemplative: 'pulse-mid',
    excited: 'pulse-fast', active: 'pulse-fast',
  };
  const OBI_MODE_TEXT = {
    absent: "Obi's probably asleep",
    calm: 'Obi seems nearby',
    active: 'Obi is around',
    'possibly-overloaded': 'Things seem busy',
  };

  function renderPresence(state) {
    const mood = (state.mood || '').toLowerCase();
    const pulse = $('mood-pulse');
    if (pulse) {
      pulse.classList.remove('pulse-slow', 'pulse-mid', 'pulse-fast', 'pulse-offline');
      pulse.classList.add(state.mood ? (PULSE_CLASSES[mood] || 'pulse-mid') : 'pulse-offline');
      const word = $('mood-word');
      if (word) word.textContent = state.mood || '—';
    }

    _drawFavicon(MOOD_FAVICON_COLOR[mood] || '#e8875a');

    // Local time + period badge (moved into presence-mood column)
    const timeEl = $('local-time');
    if (timeEl) {
      timeEl.textContent = new Date().toLocaleTimeString('en-AU', {
        hour: '2-digit', minute: '2-digit', timeZone: 'Australia/Hobart',
      });
    }
    const badge = $('time-period-badge');
    if (badge) {
      badge.classList.remove('period-morning', 'period-afternoon', 'period-evening', 'period-night');
      if (state.time_period) {
        badge.classList.add('period-' + state.time_period);
        badge.textContent = state.time_period;
      } else {
        badge.textContent = '';
      }
    }

    const modeLine = $('obi-mode-line');
    if (modeLine) modeLine.textContent = OBI_MODE_TEXT[state.obi_mode] || '';

    // "hasn't spoken since [time]" using last_spoken_ts
    const lastSpoke = $('last-spoke');
    if (lastSpoke) {
      if (state.last_spoken_ts) {
        const spokenAt = new Date(state.last_spoken_ts);
        const spokenTime = spokenAt.toLocaleTimeString('en-AU', {
          hour: '2-digit', minute: '2-digit', timeZone: 'Australia/Hobart',
        });
        const minsAgo = Math.round((Date.now() - spokenAt.getTime()) / 60000);
        if (minsAgo < 2) {
          lastSpoke.textContent = 'just spoke';
        } else if (minsAgo < 60) {
          lastSpoke.textContent = 'last spoken ' + minsAgo + ' min ago';
        } else {
          lastSpoke.textContent = 'last spoken at ' + spokenTime;
        }
      } else if (typeof state.minutes_since_speech === 'number') {
        const m = Math.round(state.minutes_since_speech);
        lastSpoke.textContent = m > 120 ? 'hasn\'t spoken in a while' : ('last spoken ' + m + ' min ago');
      } else {
        lastSpoke.textContent = '';
      }
    }

    // Last spoken text (what SPARK actually said)
    _renderInline($('last-spoken-text'), state.last_spoken || 'Nothing spoken yet…');
    _renderInline($('mood-bubble'), state.mood || '…');

    const salienceDots = $('thought-salience');
    if (salienceDots && typeof state.salience === 'number') {
      const filled = Math.round(state.salience * 5);
      salienceDots.textContent = '●'.repeat(filled) + '○'.repeat(5 - filled);
    } else if (salienceDots) {
      salienceDots.textContent = '';
    }

    const ageEl = $('thought-age');
    if (ageEl) {
      const tsStr = state.last_spoken_ts || state.ts;
      if (tsStr) {
        const mins = Math.round((Date.now() - new Date(tsStr).getTime()) / 60000);
        ageEl.textContent = mins <= 1 ? 'just now' : (mins + ' min ago');
      }
    }

    // Proximity: number + colour-coded bar (full = close, empty = far; 200 cm = scale max)
    const proxCm = $('proximity-cm');
    if (proxCm) proxCm.textContent = state.sonar_cm != null ? Math.round(state.sonar_cm) : '—';
    const proxBar = $('proximity-bar');
    if (proxBar) {
      proxBar.classList.remove('prox-close', 'prox-mid', 'prox-far');
      if (state.sonar_cm != null) {
        const pct = Math.max(0, Math.min(100, (1 - state.sonar_cm / 200) * 100));
        proxBar.style.width = pct + '%';
        if (state.sonar_cm < 40)       proxBar.classList.add('prox-close');
        else if (state.sonar_cm < 100) proxBar.classList.add('prox-mid');
        else                            proxBar.classList.add('prox-far');
      } else {
        proxBar.style.width = '0%';
      }
    }

    const frigateRow = $('frigate-indicator');
    if (frigateRow) {
      if (state.person_present === null || state.person_present === undefined) {
        frigateRow.classList.add('hidden');
      } else {
        frigateRow.classList.remove('hidden');
        const icon = $('frigate-icon');
        if (icon) icon.textContent = state.person_present ? '\uD83D\uDC64' : '\uD83D\uDC65';
        const flabel = $('frigate-label');
        if (flabel) flabel.textContent = state.person_present ? 'detected' : 'not detected';
        const conf = $('frigate-confidence');
        if (conf) {
          conf.textContent = (state.person_present && state.frigate_score != null)
            ? Math.round(state.frigate_score * 100) + '%' : '';
        }
      }
    }
  }

  // ── World band ───────────────────────────────────────────────────────────

  const WEATHER_SYMBOL_MAP = [
    ['sunny', '☀'], ['clear', '☀'], ['cloudy', '☁'], ['overcast', '☁'],
    ['rain', '🌧'], ['shower', '🌧'], ['drizzle', '🌧'],
    ['snow', '❄'], ['frost', '❄'], ['fog', '🌫'],
  ];

  function _weatherSymbol(summary) {
    if (!summary) return '';
    const s = summary.toLowerCase();
    for (const [key, sym] of WEATHER_SYMBOL_MAP) {
      if (s.includes(key)) return sym;
    }
    return '';
  }

  function renderWorld(state) {
    const label = $('ambient-level-label');
    if (label) label.textContent = state.ambient_level || '—';

    const weatherStrip = $('world-weather-strip');
    if (weatherStrip) {
      if (!state.weather) {
        weatherStrip.classList.add('hidden');
      } else {
        weatherStrip.classList.remove('hidden');
        const w = state.weather;
        const temp = $('weather-temp');
        if (temp) temp.textContent = w.temp_c != null ? (w.temp_c + '°C') : '';
        const sym = $('weather-symbol');
        if (sym) sym.textContent = _weatherSymbol(w.summary);
        const wind = $('weather-wind');
        if (wind) wind.textContent = w.wind_kmh != null ? (w.wind_kmh + ' km/h') : '';
        const hum = $('weather-humidity');
        if (hum) hum.textContent = w.humidity_pct != null ? (w.humidity_pct + '%') : '';
        const sumEl = $('weather-summary');
        if (sumEl) {
          // BOM summaries often start "At Location, it's X°C. Description."
          // Split on ". " and pick the first sentence that isn't a location+temp sentence.
          const sentences = (w.summary || '').split(/\.\s+/);
          const desc = sentences.find(s => !/^At\s/i.test(s.trim())) || sentences[0] || '';
          sumEl.textContent = desc.trim().replace(/\.$/, '');
        }
      }
    }

  }

  // ── Machine band ─────────────────────────────────────────────────────────

  function _setBar(barId, pct, warnAt, critAt) {
    const bar = $(barId);
    if (!bar) return;
    bar.classList.remove('warn', 'crit');
    if (pct == null) { bar.style.width = '0%'; return; }
    bar.style.width = Math.min(100, Math.max(0, pct)) + '%';
    if (pct >= critAt) bar.classList.add('crit');
    else if (pct >= warnAt) bar.classList.add('warn');
  }

  function renderMachine(state) {
    _setBar('bar-cpu', state.cpu_pct, 70, 90);
    const valCpu = $('val-cpu');
    if (valCpu) valCpu.textContent = state.cpu_pct != null ? (state.cpu_pct + '%') : '—';

    _setBar('bar-temp', state.cpu_temp_c != null ? Math.round(state.cpu_temp_c / 85 * 100) : null, 76, 88);
    const valTemp = $('val-temp');
    if (valTemp) valTemp.textContent = state.cpu_temp_c != null ? (state.cpu_temp_c + '°C') : '—';

    _setBar('bar-ram', state.ram_pct, 75, 90);
    const valRam = $('val-ram');
    if (valRam) valRam.textContent = state.ram_pct != null ? (state.ram_pct + '%') : '—';

    _setBar('bar-disk', state.disk_pct, 80, 90);
    const valDisk = $('val-disk');
    if (valDisk) valDisk.textContent = state.disk_pct != null ? (state.disk_pct + '%') : '—';
    const tileDisk = $('tile-disk');
    if (tileDisk) {
      tileDisk.classList.remove('disk-warn', 'disk-crit');
      if (state.disk_pct >= 90) tileDisk.classList.add('disk-crit');
      else if (state.disk_pct >= 80) tileDisk.classList.add('disk-warn');
    }

    // Battery: inverted thresholds — warn when LOW, not high
    const battBar = $('bar-battery');
    if (battBar) {
      battBar.classList.remove('warn', 'crit');
      if (state.battery_pct == null) { battBar.style.width = '0%'; }
      else {
        battBar.style.width = Math.min(100, Math.max(0, state.battery_pct)) + '%';
        if (state.battery_pct <= 10) battBar.classList.add('crit');
        else if (state.battery_pct <= 20) battBar.classList.add('warn');
      }
    }
    const valBattery = $('val-battery');
    if (valBattery) {
      const pct = state.battery_pct != null ? (state.battery_pct + '%') : '—';
      valBattery.textContent = pct + (state.charging ? ' ⚡' : '');
    }

    const valTokens = $('val-tokens');
    if (valTokens) {
      const tin = state.tokens_in, tout = state.tokens_out;
      if (tin == null) {
        valTokens.textContent = '—';
      } else {
        const fmt = n => n >= 1000 ? (n / 1000).toFixed(1) + 'k' : String(n);
        valTokens.textContent = fmt(tin) + ' in / ' + fmt(tout != null ? tout : 0) + ' out';
      }
    }

    // Services dots — built with createElement, not innerHTML
    const dotsContainer = $('services-dots');
    if (dotsContainer && state.services) {
      // Remove existing children
      while (dotsContainer.firstChild) dotsContainer.removeChild(dotsContainer.firstChild);

      const DOT_CLASS  = { active: 'dot-ok', activating: 'dot-warn', failed: 'dot-err',
                           inactive: 'dot-warn', unknown: 'dot-warn' };
      const DOT_SYMBOL = { active: '●', activating: '◐', failed: '●',
                           inactive: '○', unknown: '○' };

      for (const [svc, status] of Object.entries(state.services)) {
        const row = document.createElement('div');
        row.className = 'service-dot-row';

        const dotSpan = document.createElement('span');
        dotSpan.className = DOT_CLASS[status] || 'dot-warn';
        dotSpan.textContent = DOT_SYMBOL[status] || '○';

        const nameSpan = document.createElement('span');
        nameSpan.textContent = svc.replace('px-', '');

        row.appendChild(dotSpan);
        row.appendChild(nameSpan);
        dotsContainer.appendChild(row);
      }
    }
  }

  // ── Dynamic favicon ──────────────────────────────────────────────────────

  const MOOD_FAVICON_COLOR = {
    peaceful:      '#6aab6b',   // sage green — restful, low-energy positive
    content:       '#6aab6b',   // same as peaceful
    contemplative: '#7c6fcf',   // soft indigo — introspective, inner-focused
    curious:       '#e6a817',   // warm gold — searching, exploratory
    active:        '#2ea8e0',   // sky blue — busy, dynamic
    excited:       '#e05c3a',   // coral — bright high-energy
  };

  function _drawFavicon(color) {
    const c = document.createElement('canvas');
    c.width = 32; c.height = 32;
    const ctx = c.getContext('2d');
    ctx.beginPath();
    ctx.arc(16, 16, 14, 0, 2 * Math.PI);
    ctx.fillStyle = color;
    ctx.fill();
    const link = document.getElementById('dynamic-favicon');
    if (link) link.href = c.toDataURL('image/png');
  }

  // ── Shared helpers ───────────────────────────────────────────────────────

  function setOnline(online, cachedAt) {
    if (!online) _drawFavicon('#94a3b8');  // gray when offline
    const banner = $('offline-banner');
    if (!banner) return;
    banner.classList.toggle('hidden', online);
    if (!online && cachedAt) {
      const offlineTs = $('offline-ts');
      if (offlineTs) offlineTs.textContent = new Date(cachedAt).toLocaleString('en-AU');
    }
  }

  function setLastUpdated(text) {
    const el = $('last-updated');
    if (el) el.textContent = text;
  }

  // ── Sparklines (always-on) ────────────────────────────────────────────────

  function renderSparklines(points) {
    if (!points || points.length < 2) return;
    document.querySelectorAll('canvas[data-field]').forEach(canvas => {
      // Set canvas width from its rendered width so it fills the container
      if (!canvas.width || canvas.width < 10) canvas.width = canvas.offsetWidth || 160;
      SparkCharts.drawSparkline(canvas, points, canvas.dataset.field);
    });
  }

  // Draw initial connecting state
  _drawFavicon('#d1c4b8');

  return { renderPresence, renderWorld, renderMachine, renderSparklines, setOnline, setLastUpdated, MOOD_FAVICON_COLOR };
})();
