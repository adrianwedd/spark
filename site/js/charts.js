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

    var BAR_COUNT = 32;
    var BAR_W = 3;
    var GAP = 1;
    var MAX_H = H - 4;
    var BASE_H = 2;

    // Deterministic pseudo-random seeded from rms
    let s = Math.round(rms || 0) + 1;
    function rand() {
      s = (s * 1664525 + 1013904223) & 0xffffffff;
      return (s >>> 16) / 65535;
    }

    const amplitude = Math.min(1, Math.max(0, (rms || 0) / 1500));
    ctx.fillStyle = getComputedStyle(document.documentElement)
      .getPropertyValue('--spark-accent').trim() || '#c48b6e';

    ctx.globalAlpha = 0.6;
    for (var i = 0; i < BAR_COUNT; i++) {
      var barH = BASE_H + Math.round(rand() * MAX_H * amplitude);
      var x = i * (BAR_W + GAP);
      var y = H - barH;
      if (ctx.roundRect) {
        ctx.beginPath();
        ctx.roundRect(x, y, BAR_W, barH, [2, 2, 0, 0]);
        ctx.fill();
      } else {
        ctx.fillRect(x, y, BAR_W, barH);
      }
    }
    ctx.globalAlpha = 1.0;
  }

  // ── Sparkline ────────────────────────────────────────────────────────────
  function drawSparkline(canvas, points, field) {
    if (!canvas || !points || points.length < 2) return;
    var ctx = canvas.getContext('2d');
    var W = canvas.width, H = canvas.height;
    ctx.clearRect(0, 0, W, H);

    var vals = points.map(function (p) { return p[field]; }).filter(function (v) { return v !== null && v !== undefined; });
    if (vals.length < 2) return;

    var minV = Math.min.apply(null, vals);
    var maxV = Math.max.apply(null, vals);
    var range = maxV - minV || 1;

    function toX(i) { return (i / (points.length - 1)) * (W - 4) + 2; }
    function toY(v) { return H - 4 - ((v - minV) / range) * (H - 8); }

    var accent = getComputedStyle(document.documentElement)
      .getPropertyValue('--spark-accent').trim() || '#c48b6e';

    ctx.beginPath();
    ctx.strokeStyle = accent;
    ctx.lineWidth = 1.5;
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';

    var lastX, lastY, first = true;
    points.forEach(function (p, i) {
      var v = p[field];
      if (v === null || v === undefined) return;
      var x = toX(i), y = toY(v);
      if (first) { ctx.moveTo(x, y); first = false; }
      else ctx.lineTo(x, y);
      lastX = x; lastY = y;
    });
    ctx.stroke();

    // Terminal dot — 4px circle at the end
    if (lastX !== undefined) {
      ctx.beginPath();
      ctx.arc(lastX, lastY, 2, 0, Math.PI * 2);
      ctx.fillStyle = accent;
      ctx.fill();
    }
  }

  // ── Mood colour helper (reads from CSS custom properties in colors.css) ──
  function _moodColor(mood) {
    if (!mood) return '';  // no mood → skip (don't return grey fallback)
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
