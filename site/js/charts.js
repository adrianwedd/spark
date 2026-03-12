// charts.js — Canvas/SVG drawing for the live dashboard.
// Exposes: window.SparkCharts
window.SparkCharts = (function () {
  'use strict';

  // ── Waveform bars ────────────────────────────────────────────────────────
  function drawWaveform(canvas, rms) {
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const W = canvas.width, H = canvas.height;
    ctx.clearRect(0, 0, W, H);

    const BAR_COUNT = 40;
    const BAR_W = Math.floor(W / BAR_COUNT) - 1;
    const MAX_H = H - 4;
    const BASE_H = 3;

    // Deterministic pseudo-random seeded from rms
    let s = Math.round(rms || 0) + 1;
    function rand() {
      s = (s * 1664525 + 1013904223) & 0xffffffff;
      return (s >>> 16) / 65535;
    }

    const amplitude = Math.min(1, Math.max(0, (rms || 0) / 1500));
    ctx.fillStyle = getComputedStyle(document.documentElement)
      .getPropertyValue('--warm-accent').trim() || '#e8875a';

    for (let i = 0; i < BAR_COUNT; i++) {
      const barH = BASE_H + Math.round(rand() * MAX_H * amplitude);
      ctx.fillRect(i * (BAR_W + 1), H - barH, Math.max(1, BAR_W), barH);
    }
  }

  // ── Proximity fan arc (SVG) ──────────────────────────────────────────────
  function drawProximityArc(svgEl, sonarCm) {
    if (!svgEl) return;

    let angleDeg, colorClass;
    if (sonarCm === null || sonarCm === undefined) {
      angleDeg = 0; colorClass = 'arc-unavailable';
    } else if (sonarCm < 40) {
      angleDeg = 180; colorClass = 'arc-close';
    } else if (sonarCm <= 100) {
      angleDeg = 90; colorClass = 'arc-mid';
    } else if (sonarCm <= 150) {
      angleDeg = Math.round(90 - (sonarCm - 100) * (70 / 50));
      colorClass = 'arc-far';
    } else {
      angleDeg = 20; colorClass = 'arc-far';
    }

    // Clear all child elements without innerHTML
    while (svgEl.firstChild) svgEl.removeChild(svgEl.firstChild);

    const CX = 60, CY = 60, R = 55;
    const NS = 'http://www.w3.org/2000/svg';

    // Outline arc
    const outline = document.createElementNS(NS, 'path');
    outline.setAttribute('d', _arcPath(CX, CY, R, 0, 180));
    outline.setAttribute('fill', 'none');
    outline.setAttribute('stroke', '#d1c4b8');
    outline.setAttribute('stroke-width', '2');
    svgEl.appendChild(outline);

    if (angleDeg > 0) {
      const fill = document.createElementNS(NS, 'path');
      fill.setAttribute('d', _arcPath(CX, CY, R, 0, angleDeg) + ' L ' + CX + ' ' + CY + ' Z');
      fill.setAttribute('class', 'arc-fill ' + colorClass);
      svgEl.appendChild(fill);
    }
  }

  function _arcPath(cx, cy, r, startDeg, endDeg) {
    const toRad = d => (d - 90) * Math.PI / 180;
    const sx = cx + r * Math.cos(toRad(startDeg));
    const sy = cy + r * Math.sin(toRad(startDeg));
    const ex = cx + r * Math.cos(toRad(endDeg));
    const ey = cy + r * Math.sin(toRad(endDeg));
    const large = (endDeg - startDeg) > 180 ? 1 : 0;
    return 'M ' + sx + ' ' + sy + ' A ' + r + ' ' + r + ' 0 ' + large + ' 1 ' + ex + ' ' + ey;
  }

  // ── CPU Temperature Gauge Arc (SVG) ─────────────────────────────────────
  function drawGaugeArc(svgEl, tempC) {
    if (!svgEl) return;
    while (svgEl.firstChild) svgEl.removeChild(svgEl.firstChild);

    const NS = 'http://www.w3.org/2000/svg';
    const CX = 40, CY = 42, R = 35;

    // Background track
    const track = document.createElementNS(NS, 'path');
    track.setAttribute('d', _arcPath(CX, CY, R, 0, 180));
    track.setAttribute('fill', 'none');
    track.setAttribute('stroke', '#e5e7eb');
    track.setAttribute('stroke-width', '6');
    track.setAttribute('stroke-linecap', 'round');
    svgEl.appendChild(track);

    if (tempC !== null && tempC !== undefined) {
      const pct = Math.min(1, Math.max(0, tempC / 85));
      const fillAngle = Math.round(pct * 180);

      let gaugeClass = 'gauge-ok';
      if (tempC >= 75) gaugeClass = 'gauge-crit';
      else if (tempC >= 65) gaugeClass = 'gauge-warn';
      svgEl.setAttribute('class', gaugeClass);

      const fill = document.createElementNS(NS, 'path');
      fill.setAttribute('d', _arcPath(CX, CY, R, 0, Math.max(1, fillAngle)));
      fill.setAttribute('fill', 'none');
      fill.setAttribute('class', 'gauge-fill');
      fill.setAttribute('stroke-width', '6');
      fill.setAttribute('stroke-linecap', 'round');
      svgEl.appendChild(fill);
    }
  }

  // ── Sparkline ────────────────────────────────────────────────────────────
  function drawSparkline(canvas, points, field) {
    if (!canvas || !points || points.length < 2) return;
    const ctx = canvas.getContext('2d');
    const W = canvas.width, H = canvas.height;
    ctx.clearRect(0, 0, W, H);

    const vals = points.map(p => p[field]).filter(v => v !== null && v !== undefined);
    if (vals.length < 2) return;

    const minV = Math.min(...vals);
    const maxV = Math.max(...vals);
    const range = maxV - minV || 1;

    const toX = i => (i / (points.length - 1)) * (W - 4) + 2;
    const toY = v => H - 4 - ((v - minV) / range) * (H - 8);

    ctx.beginPath();
    ctx.strokeStyle = getComputedStyle(document.documentElement)
      .getPropertyValue('--warm-accent').trim() || '#e8875a';
    ctx.lineWidth = 1.5;
    ctx.lineJoin = 'round';

    let first = true;
    points.forEach((p, i) => {
      const v = p[field];
      if (v === null || v === undefined) return;
      if (first) { ctx.moveTo(toX(i), toY(v)); first = false; }
      else ctx.lineTo(toX(i), toY(v));
    });
    ctx.stroke();

    // Range labels
    ctx.fillStyle = '#999';
    ctx.font = '9px sans-serif';
    ctx.fillText(minV.toFixed(0), 2, H - 1);
    ctx.fillText(maxV.toFixed(0), 2, 9);
  }

  return { drawWaveform, drawProximityArc, drawGaugeArc, drawSparkline };
})();
