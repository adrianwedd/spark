/* status-dot.js — mood-coloured status dot for all pages */
(function () {
  'use strict';
  var API = window.SPARK_CONFIG.API_BASE + '/status';
  var dot = document.getElementById('status-dot');
  if (!dot) return;

  function _moodColor(mood) {
    return getComputedStyle(document.documentElement)
      .getPropertyValue('--mood-' + mood).trim() || '#888';
  }

  function check() {
    var ctrl = new AbortController();
    var timer = setTimeout(function () { ctrl.abort(); }, 5000);
    fetch(API, { signal: ctrl.signal })
      .then(function (r) { clearTimeout(timer); return r.json(); })
      .then(function (data) {
        var mood = (data.mood || '').toLowerCase();
        var color = (mood ? _moodColor(mood) : null) || '#4ade80';
        dot.style.background = color;
        dot.title = mood ? ('SPARK is feeling ' + mood) : 'SPARK is online';
      })
      .catch(function () {
        clearTimeout(timer);
        dot.style.background = '#ef4444';
        dot.title = 'SPARK is offline';
      });
  }

  check();
  setInterval(check, 30000);
})();
