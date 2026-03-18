// chat.js — SPARK chat bubble + panel for the public dashboard.
// Reads SparkDashboard.MOOD_FAVICON_COLOR (map) and currentMoodWord from live state.
(function () {
  'use strict';

  const API_URL = window.SPARK_CONFIG.API_BASE + '/chat';
  const MAX_HISTORY = 20;

  var _history  = [];
  var _inflight = null;
  var _thinkEl  = null;

  var bubble   = document.getElementById('chat-bubble');
  var panel    = document.getElementById('chat-panel');
  var closeBtn = document.getElementById('chat-close');
  var messages = document.getElementById('chat-messages');
  var input    = document.getElementById('chat-input');
  var sendBtn  = document.getElementById('chat-send');
  var moodWord = document.getElementById('chat-mood-word');

  if (!bubble || !panel) return;

  // ── Mood colour ──────────────────────────────────────────────
  function updateBubbleColor() {
    var word = (window.SparkDashboard && window.SparkDashboard.currentMoodWord) || '';
    var color = (word && window.SparkDashboard && window.SparkDashboard.moodColor)
      ? window.SparkDashboard.moodColor(word) : null;
    if (color) {
      bubble.style.setProperty('--chat-bubble-color', color);
      panel.style.setProperty('--chat-bubble-color', color);
    }
    if (moodWord) moodWord.textContent = word;
  }

  // ── Open / close ─────────────────────────────────────────────
  function openPanel() {
    updateBubbleColor();
    panel.hidden = false;
    bubble.setAttribute('aria-expanded', 'true');
    input.focus();
  }

  function closePanel() {
    panel.hidden = true;
    bubble.setAttribute('aria-expanded', 'false');
    bubble.focus();
    if (_inflight) { _inflight.abort(); _inflight = null; }
    if (_thinkEl && _thinkEl.parentNode) _thinkEl.parentNode.removeChild(_thinkEl);
    _thinkEl = null;
    setInputEnabled(true);
  }

  // ── Focus trap ───────────────────────────────────────────────
  var focusable = [input, sendBtn, closeBtn];
  function trapFocus(e) {
    if (e.key !== 'Tab') return;
    var first = focusable[0];
    var last  = focusable[focusable.length - 1];
    if (e.shiftKey) {
      if (document.activeElement === first) { e.preventDefault(); last.focus(); }
    } else {
      if (document.activeElement === last)  { e.preventDefault(); first.focus(); }
    }
  }

  // ── Scroll helpers ───────────────────────────────────────────
  function nearBottom() {
    return messages.scrollHeight - messages.scrollTop - messages.clientHeight < 60;
  }

  // ── Message rendering ────────────────────────────────────────
  function appendMsg(role, text) {
    var div = document.createElement('div');
    div.className = 'chat-msg chat-msg--' + (role === 'spark' ? 'spark' : 'user');
    div.textContent = text;
    var nb = nearBottom();
    messages.appendChild(div);
    if (nb) messages.scrollTop = messages.scrollHeight;
    return div;
  }

  function setThinking(on) {
    if (on) {
      _thinkEl = document.createElement('div');
      _thinkEl.className = 'chat-msg chat-msg--spark';
      _thinkEl.setAttribute('aria-label', 'SPARK is thinking');
      var dots = document.createElement('span');
      dots.className = 'chat-thinking-dots';
      for (var d = 0; d < 3; d++) {
        var s = document.createElement('span');
        s.setAttribute('aria-hidden', 'true');
        s.textContent = '\u2022';
        dots.appendChild(s);
      }
      _thinkEl.appendChild(dots);
      var nb = nearBottom();
      messages.appendChild(_thinkEl);
      if (nb) messages.scrollTop = messages.scrollHeight;
    } else {
      if (_thinkEl && _thinkEl.parentNode) _thinkEl.parentNode.removeChild(_thinkEl);
      _thinkEl = null;
    }
  }

  function setInputEnabled(on) {
    input.disabled   = !on;
    sendBtn.disabled = !on;
    bubble.classList.toggle('chat-thinking', !on);
  }

  // ── Send ─────────────────────────────────────────────────────
  function send() {
    var text = input.value.trim();
    if (!text || _inflight) return;
    input.value = '';
    appendMsg('user', text);
    setInputEnabled(false);
    setThinking(true);

    var ctrl = new AbortController();
    _inflight = ctrl;

    fetch(API_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        message: text,
        history: _history.slice(-MAX_HISTORY),
      }),
      signal: ctrl.signal,
    })
    .then(function (res) {
      setThinking(false);
      return res.json().then(function (data) {
        return { ok: res.ok, status: res.status, data: data };
      });
    })
    .then(function (r) {
      if (_inflight !== ctrl) return;
      var reply;
      if (r.ok) {
        reply = r.data.reply || "I\u2019m here \u2014 I just went quiet for a moment. Try again?";
      } else if (r.status === 429) {
        reply = "I\u2019m still here \u2014 just need a moment before we keep going.";
      } else {
        reply = "Something went quiet on my end. Try again?";
      }
      _history.push({ role: 'user',  text: text  });
      _history.push({ role: 'spark', text: reply });
      if (_history.length > MAX_HISTORY * 2) {
        _history = _history.slice(-MAX_HISTORY * 2);
      }
      appendMsg('spark', reply);
    })
    .catch(function (err) {
      setThinking(false);
      if (err.name !== 'AbortError' && _inflight === ctrl) {
        appendMsg('spark', "Something went quiet on my end. Try again?");
      }
    })
    .finally(function () {
      if (_inflight === ctrl) {
        _inflight = null;
        setInputEnabled(true);
        input.focus();
      }
    });
  }

  // ── Events ───────────────────────────────────────────────────
  bubble.addEventListener('click', openPanel);
  closeBtn.addEventListener('click', closePanel);

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && !panel.hidden) closePanel();
  });
  document.addEventListener('click', function (e) {
    // Use .contains() not identity check — clicking the SVG inside the bubble
    // sets e.target to the SVG child, not the button itself
    if (!panel.hidden && !panel.contains(e.target) && !bubble.contains(e.target)) closePanel();
  });

  panel.addEventListener('keydown', trapFocus);
  sendBtn.addEventListener('click', send);
  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  });

  document.addEventListener('spark-state-updated', updateBubbleColor);
  updateBubbleColor();

}());
