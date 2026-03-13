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
    const moodColor = MOOD_FAVICON_COLOR[mood] || '#e8875a';
    const pulse = $('mood-pulse');
    if (pulse) {
      pulse.classList.remove('pulse-slow', 'pulse-mid', 'pulse-fast', 'pulse-offline');
      if (state.mood) {
        pulse.classList.add(PULSE_CLASSES[mood] || 'pulse-mid');
        pulse.style.background = moodColor;
      } else {
        pulse.classList.add('pulse-offline');
        pulse.style.background = '';
      }
      const word = $('mood-word');
      if (word) word.textContent = state.mood || '—';
    }

    _drawFavicon(moodColor);

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

    // Speech bubble card — last spoken
    const spokenEl    = $('last-spoken-text');
    const spokenAgeEl = $('last-spoken-age');
    if (spokenEl) spokenEl.textContent = state.last_spoken || '—';
    if (spokenAgeEl) {
      if (state.last_spoken_ts) {
        const mins = Math.round((Date.now() - new Date(state.last_spoken_ts).getTime()) / 60000);
        spokenAgeEl.textContent = mins <= 1 ? 'just now' : (mins + ' min ago');
      } else if (typeof state.minutes_since_speech === 'number') {
        const m = Math.round(state.minutes_since_speech);
        spokenAgeEl.textContent = m > 120 ? 'a while ago' : (m + ' min ago');
      } else {
        spokenAgeEl.textContent = '';
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

    // Detections card (always visible, show empty state when nothing detected)
    const detectionsList  = $('detections-list');
    const detectionsEmpty = $('detections-empty');
    if (detectionsList) {
      const dets = Array.isArray(state.detections) ? state.detections : [];
      while (detectionsList.firstChild) detectionsList.removeChild(detectionsList.firstChild);
      if (dets.length === 0) {
        if (detectionsEmpty) detectionsEmpty.style.display = '';
      } else {
        if (detectionsEmpty) detectionsEmpty.style.display = 'none';
        dets.forEach(d => {
          const li = document.createElement('li');
          li.className = 'detection-item';
          const pct = Math.round(d.score * 100);
          const bar = document.createElement('div');
          bar.className = 'detection-bar-wrap';
          const fill = document.createElement('div');
          fill.className = 'detection-bar-fill';
          fill.style.width = pct + '%';
          bar.appendChild(fill);
          const label = document.createElement('span');
          label.className = 'detection-label';
          label.textContent = d.label;
          const conf = document.createElement('span');
          conf.className = 'detection-conf';
          conf.textContent = pct + '%' + (d.count > 1 ? ' ×' + d.count : '');
          li.appendChild(label);
          li.appendChild(bar);
          li.appendChild(conf);
          detectionsList.appendChild(li);
        });
      }
    }

    // Who's home card (always visible)
    const haList = $('ha-presence-list');
    if (haList) {
      const ha = state.ha_presence;
      while (haList.firstChild) haList.removeChild(haList.firstChild);
      const people = (ha && Array.isArray(ha.people)) ? ha.people : [];
      if (people.length === 0) {
        const li = document.createElement('li');
        li.className = 'ha-person-item ha-person-unknown';
        li.textContent = 'No data';
        haList.appendChild(li);
      } else {
        people.forEach(p => {
          const li = document.createElement('li');
          li.className = 'ha-person-item';
          const dot = document.createElement('span');
          dot.className = 'ha-dot ' + (p.home ? 'ha-dot-home' : (p.state === 'unknown' ? 'ha-dot-unknown' : 'ha-dot-away'));
          dot.textContent = '●';
          const name = document.createElement('span');
          name.className = 'ha-person-name';
          name.textContent = p.name;
          const st = document.createElement('span');
          st.className = 'ha-person-state';
          st.textContent = p.state;
          li.appendChild(dot);
          li.appendChild(name);
          li.appendChild(st);
          haList.appendChild(li);
        });
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

        // Temp card
        const temp = $('weather-temp');
        if (temp) temp.textContent = w.temp_c != null ? (w.temp_c + '°C') : '—';
        const sym = $('weather-symbol');
        if (sym) sym.textContent = _weatherSymbol(w.summary);

        // Summary: strip BOM location prefix, show condition only
        const sumEl = $('weather-summary');
        if (sumEl) {
          const sentences = (w.summary || '').split(/\.\s+/);
          const desc = sentences.find(s => !/^At\s/i.test(s.trim())) || sentences[0] || '';
          sumEl.textContent = desc.trim().replace(/\.$/, '');
        }

        // Wind card: show full description "SW at 17 km/h, gusting to 26 km/h"
        const wind = $('weather-wind');
        if (wind) {
          if (w.wind_dir || w.wind_kmh != null) {
            const dir = w.wind_dir || '';
            const spd = w.wind_kmh != null ? w.wind_kmh + ' km/h' : '';
            let line = dir && spd ? dir + ' at ' + spd : (dir || spd);
            if (w.gust_kmh != null) line += ', gusting to ' + w.gust_kmh + ' km/h';
            wind.textContent = line;
          } else {
            wind.textContent = '—';
          }
        }

        const hum = $('weather-humidity');
        if (hum) hum.textContent = w.humidity_pct != null ? (w.humidity_pct + '%') : '—';

        const rain = $('weather-rain');
        if (rain) {
          const r = w.rain_24h_mm;
          if (r == null) {
            rain.textContent = '—';
          } else if (typeof r === 'string' && /tce/i.test(r)) {
            rain.textContent = 'trace';
          } else {
            rain.textContent = (parseFloat(r) || 0) + ' mm';
          }
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

    // WiFi signal — inverted dBm: -100 = 0%, 0 = 100%
    const wifiBar = $('bar-wifi');
    if (wifiBar) {
      wifiBar.classList.remove('warn', 'crit');
      if (state.wifi_dbm == null) { wifiBar.style.width = '0%'; }
      else {
        const wifiPct = Math.max(0, Math.min(100, (state.wifi_dbm + 100) * 2));
        wifiBar.style.width = wifiPct + '%';
        if (wifiPct < 20) wifiBar.classList.add('crit');
        else if (wifiPct < 40) wifiBar.classList.add('warn');
      }
    }
    const valWifi = $('val-wifi');
    if (valWifi) valWifi.textContent = state.wifi_dbm != null ? (state.wifi_dbm + ' dBm') : '—';

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
