/* status-dot.js — mood-coloured status dot for all pages */
(function () {
  'use strict';
  var API = 'https://spark-api.wedd.au/api/v1/public/status';
  var dot = document.getElementById('status-dot');
  if (!dot) return;

  var MOOD_COLORS = {
    peaceful: '#4a9d8f',
    content: '#6b8e5e',
    contemplative: '#7b6fa0',
    curious: '#c48a3f',
    active: '#d46b4a',
    excited: '#d44a6b'
  };

  function check() {
    var ctrl = new AbortController();
    var timer = setTimeout(function () { ctrl.abort(); }, 5000);
    fetch(API, { signal: ctrl.signal })
      .then(function (r) { clearTimeout(timer); return r.json(); })
      .then(function (data) {
        var mood = (data.mood || '').toLowerCase();
        var color = MOOD_COLORS[mood] || '#4ade80';
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
