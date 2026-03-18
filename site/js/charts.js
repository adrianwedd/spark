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

  // ── Mood colour helper (reads from CSS custom properties in colors.css) ──
  function _moodColor(mood) {
    return getComputedStyle(document.documentElement)
      .getPropertyValue('--mood-' + mood).trim() || '';
  }

  // ── Mood colour strip ─────────────────────────────────────────────────────

  function drawMoodStrip(canvas, points) {
    if (!canvas || !points || points.length < 1) return;
    const ctx = canvas.getContext('2d');
    const W = canvas.width, H = canvas.height;
    ctx.clearRect(0, 0, W, H);

    const n = points.length;
    const segW = W / n;
    const gap = segW > 4 ? 1 : 0;

    // Forward-fill: carry the last known colour through null/unknown segments
    let lastColor = null;
    points.forEach((p, i) => {
      const color = _moodColor((p.mood || '').toLowerCase());
      if (color) lastColor = color;
      if (!lastColor) return;  // no colour seen yet — leave blank
      ctx.fillStyle = lastColor;
      ctx.fillRect(i * segW + gap, 0, Math.max(1, segW - gap), H);
    });
  }

  return { drawWaveform, drawSparkline, drawMoodStrip };
})();
