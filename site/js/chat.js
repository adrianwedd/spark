// chat.js — SPARK ↔ Obi two-way conversation panel.
// Authenticated (PIN → session token). SPARK can initiate; Obi can reply.
// Falls back to unauthenticated public/chat if no token stored.
(function () {
  'use strict';

  var API_ROOT    = window.SPARK_CONFIG.API_ROOT;
  var OBI_CHAT    = API_ROOT + '/obi-chat';
  var PIN_URL     = API_ROOT + '/pin/verify';
  var MAX_HISTORY = 20;
  var POLL_MS     = 10000;  // 10 s poll when panel is open
  var TOKEN_KEY   = 'spark_obi_token';

  var _token      = localStorage.getItem(TOKEN_KEY) || null;
  var _history    = [];    // [{role, text}] for display only
  var _inflight   = null;
  var _thinkEl    = null;
  var _pollTimer  = null;
  var _lastSince  = null;
  var _panelOpen  = false;
  var _unread     = 0;

  var bubble    = document.getElementById('chat-bubble');
  var panel     = document.getElementById('chat-panel');
  var closeBtn  = document.getElementById('chat-close');
  var messages  = document.getElementById('chat-messages');
  var input     = document.getElementById('chat-input');
  var sendBtn   = document.getElementById('chat-send');
  var moodWord  = document.getElementById('chat-mood-word');
  var unreadBadge = document.getElementById('chat-unread');

  if (!bubble || !panel) return;

  // ── Mood colour ──────────────────────────────────────────────
  function updateBubbleColor() {
    var word  = (window.SparkDashboard && window.SparkDashboard.currentMoodWord) || '';
    var color = (word && window.SparkDashboard && window.SparkDashboard.moodColor)
      ? window.SparkDashboard.moodColor(word) : null;
    if (color) {
      bubble.style.setProperty('--chat-bubble-color', color);
      panel.style.setProperty('--chat-bubble-color', color);
    }
    if (moodWord) moodWord.textContent = word;
  }

  // ── Unread badge ─────────────────────────────────────────────
  function setUnread(n) {
    _unread = n;
    if (!unreadBadge) return;
    if (n > 0) {
      unreadBadge.textContent = n > 9 ? '9+' : String(n);
      unreadBadge.hidden = false;
    } else {
      unreadBadge.hidden = true;
    }
  }

  // ── Message rendering ────────────────────────────────────────
  function nearBottom() {
    return messages.scrollHeight - messages.scrollTop - messages.clientHeight < 60;
  }

  function appendMsg(role, text, initiated) {
    var div = document.createElement('div');
    if (role === 'spark' || role === 'obi') {
      div.className = role === 'obi'
        ? 'chat-msg chat-msg--user'
        : (initiated ? 'chat-msg chat-msg--spark-init' : 'chat-msg chat-msg--spark');
    } else {
      div.className = 'chat-msg chat-msg--user';
    }
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
        s.textContent = '•';
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

  // ── PIN form ─────────────────────────────────────────────────
  function showPinForm() {
    // Replace message area with PIN form; hide input row
    messages.innerHTML = '';
    var form = document.createElement('div');
    form.className = 'chat-pin-form';
    form.id = 'chat-pin-form';

    var label = document.createElement('p');
    label.className = 'chat-pin-label';
    label.textContent = 'Enter your PIN to chat with SPARK.';

    var pinIn = document.createElement('input');
    pinIn.type = 'password';
    pinIn.className = 'chat-pin-input';
    pinIn.placeholder = '••••';
    pinIn.maxLength = 16;
    pinIn.autocomplete = 'current-password';
    pinIn.id = 'chat-pin-input';

    var errEl = document.createElement('p');
    errEl.className = 'chat-pin-error';
    errEl.id = 'chat-pin-error';

    var btn = document.createElement('button');
    btn.className = 'chat-pin-submit';
    btn.textContent = 'Unlock';

    form.appendChild(label);
    form.appendChild(pinIn);
    form.appendChild(errEl);
    form.appendChild(btn);
    messages.appendChild(form);

    var inputRow = panel.querySelector('.chat-input-row');
    if (inputRow) inputRow.hidden = true;

    function attemptPin() {
      var pin = pinIn.value.trim();
      if (!pin) return;
      btn.disabled = true;
      errEl.textContent = '';
      fetch(PIN_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pin: pin }),
      })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.verified && data.token) {
          _token = data.token;
          localStorage.setItem(TOKEN_KEY, _token);
          showChat();
        } else {
          errEl.textContent = data.error || 'Incorrect PIN — try again.';
          pinIn.value = '';
          btn.disabled = false;
          pinIn.focus();
        }
      })
      .catch(function () {
        errEl.textContent = 'Could not reach SPARK — try again.';
        btn.disabled = false;
      });
    }

    btn.addEventListener('click', attemptPin);
    pinIn.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); attemptPin(); }
    });
    setTimeout(function () { pinIn.focus(); }, 50);
  }

  // ── Chat view ────────────────────────────────────────────────
  function showChat() {
    // Restore input row, clear any PIN form, load history
    var inputRow = panel.querySelector('.chat-input-row');
    if (inputRow) inputRow.hidden = false;
    messages.innerHTML = '';
    // Render cached history
    _history.forEach(function (m) { appendMsg(m.role, m.text, m.initiated); });
    input.focus();
    startPolling();
  }

  // ── Open / close ─────────────────────────────────────────────
  function openPanel() {
    updateBubbleColor();
    panel.hidden = false;
    bubble.setAttribute('aria-expanded', 'true');
    _panelOpen = true;
    setUnread(0);

    if (_token) {
      showChat();
    } else {
      showPinForm();
    }
  }

  function closePanel() {
    _panelOpen = false;
    panel.hidden = true;
    bubble.setAttribute('aria-expanded', 'false');
    bubble.focus();
    stopPolling();
    if (_inflight) { _inflight.abort(); _inflight = null; }
    setThinking(false);
    setInputEnabled(true);
  }

  // ── Focus trap ───────────────────────────────────────────────
  panel.addEventListener('keydown', function (e) {
    if (e.key !== 'Tab') return;
    var focusable = Array.prototype.slice.call(
      panel.querySelectorAll('button:not([disabled]), input:not([disabled]), textarea:not([disabled])')
    );
    if (!focusable.length) return;
    var first = focusable[0];
    var last  = focusable[focusable.length - 1];
    if (e.shiftKey) {
      if (document.activeElement === first) { e.preventDefault(); last.focus(); }
    } else {
      if (document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  });

  // ── Polling (SPARK-initiated messages) ───────────────────────
  function startPolling() {
    if (_pollTimer) return;
    poll();
    _pollTimer = setInterval(poll, POLL_MS);
  }

  function stopPolling() {
    clearInterval(_pollTimer);
    _pollTimer = null;
  }

  function poll() {
    if (!_token) return;
    var url = OBI_CHAT + (_lastSince ? '?since=' + encodeURIComponent(_lastSince) : '');
    fetch(url, { headers: { Authorization: 'Bearer ' + _token } })
    .then(function (r) {
      if (r.status === 401) { handleTokenExpired(); return null; }
      return r.json();
    })
    .then(function (data) {
      if (!data || !data.messages) return;
      var msgs = data.messages;
      // Determine which are SPARK-initiated (no preceding obi msg in this batch)
      // by checking if they appear after the last known history entry
      var newUnread = 0;
      msgs.forEach(function (m) {
        // Avoid duplicates: check against last entry in _history
        var lastId = _history.length ? _history[_history.length - 1].id : null;
        if (m.id === lastId) return;
        var isSparkInit = m.role === 'spark' && !_panelOpen;
        var initiated = m.role === 'spark';
        _history.push({ id: m.id, role: m.role, text: m.text, initiated: initiated });
        if (_history.length > MAX_HISTORY * 2) {
          _history = _history.slice(-MAX_HISTORY * 2);
        }
        if (_panelOpen) {
          appendMsg(m.role, m.text, initiated);
        } else if (isSparkInit) {
          newUnread++;
        }
      });
      if (newUnread) setUnread(_unread + newUnread);
      if (data.since) _lastSince = data.since;
    })
    .catch(function () {});
  }

  function handleTokenExpired() {
    _token = null;
    localStorage.removeItem(TOKEN_KEY);
    if (_panelOpen) showPinForm();
  }

  // ── Send ─────────────────────────────────────────────────────
  function send() {
    if (!_token) return;
    var text = input.value.trim();
    if (!text || _inflight) return;
    input.value = '';
    appendMsg('obi', text, false);
    _history.push({ role: 'obi', text: text });
    setInputEnabled(false);
    setThinking(true);

    var ctrl = new AbortController();
    _inflight = ctrl;

    fetch(OBI_CHAT, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + _token,
      },
      body: JSON.stringify({ message: text }),
      signal: ctrl.signal,
    })
    .then(function (r) {
      setThinking(false);
      if (r.status === 401) { handleTokenExpired(); return null; }
      return r.json().then(function (data) { return { ok: r.ok, status: r.status, data: data }; });
    })
    .then(function (r) {
      if (!r || _inflight !== ctrl) return;
      var reply;
      if (r.ok) {
        reply = r.data.reply || "I’m here — just went quiet for a moment. Try again?";
        // Mark as seen so poll doesn't double-show it
        if (r.data.id) _lastSince = r.data.ts || _lastSince;
      } else if (r.status === 429) {
        reply = "Just a moment — give me a second.";
      } else {
        reply = "Something went quiet on my end. Try again?";
      }
      _history.push({ role: 'spark', text: reply });
      appendMsg('spark', reply, false);
    })
    .catch(function (err) {
      setThinking(false);
      if (err.name !== 'AbortError' && _inflight === ctrl) {
        appendMsg('spark', "Something went quiet on my end. Try again?", false);
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
    if (!panel.hidden && !panel.contains(e.target) && !bubble.contains(e.target)) closePanel();
  });

  sendBtn.addEventListener('click', send);
  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  });

  document.addEventListener('spark-state-updated', updateBubbleColor);
  updateBubbleColor();

  // Poll in background even when panel is closed — to catch SPARK-initiated messages
  // and show the unread badge. Start immediately if token exists.
  if (_token) {
    _pollTimer = setInterval(poll, POLL_MS);
    poll();
  }

}());
