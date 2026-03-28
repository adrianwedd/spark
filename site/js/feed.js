/* Feed page — fetches /api/v1/public/feed and renders post cards.
   Fallback chain: live API → same-origin snapshot → GitHub raw → localStorage cache */
(function () {
  'use strict';

  var API = window.SPARK_CONFIG.API_BASE;
  var FALLBACK_LOCAL = window.SPARK_CONFIG.FALLBACK_LOCAL;
  var FALLBACK_GITHUB = window.SPARK_CONFIG.FALLBACK_GITHUB;
  var TIMEOUT_MS = 8000;
  var CACHE_KEY = 'spark_feed_cache';
  var PAGE_SIZE = 20;
  var _allPosts = [];
  var _displayedCount = 0;

  // All 12 moods have .mood-{name} classes in feed.css (plus legacy "active")
  var VALID_MOODS = ['peaceful','content','contemplative','curious','active','excited',
    'alert','playful','mischievous','bored','lonely','grumpy','anxious'];

  function fetchJSON(url) {
    var ctrl = new AbortController();
    var timer = setTimeout(function () { ctrl.abort(); }, TIMEOUT_MS);
    return fetch(url, { signal: ctrl.signal })
      .then(function (r) { clearTimeout(timer); return r.json(); })
      .catch(function (e) { clearTimeout(timer); throw e; });
  }

  function formatTime(isoStr) {
    try {
      var d = new Date(isoStr);
      return d.toLocaleString('en-AU', {
        timeZone: 'Australia/Hobart',
        day: 'numeric', month: 'short', year: 'numeric',
        hour: 'numeric', minute: '2-digit', hour12: true
      });
    } catch (e) {
      return '';
    }
  }

  function thoughtURL(post) {
    return '/thought/?ts=' + encodeURIComponent(post.ts);
  }

  function moodClass(mood) {
    var m = (mood || '').toLowerCase();
    return VALID_MOODS.indexOf(m) >= 0 ? 'mood-' + m : 'mood-content';
  }

  function renderCard(post) {
    var a = document.createElement('a');
    a.className = 'feed-card';
    a.href = thoughtURL(post);

    var quote = document.createElement('p');
    quote.className = 'feed-card-quote';
    quote.textContent = post.thought;

    var meta = document.createElement('div');
    meta.className = 'feed-card-meta';

    var badge = document.createElement('span');
    badge.className = 'mood-badge ' + moodClass(post.mood);
    badge.textContent = post.mood || 'thinking';

    var time = document.createElement('time');
    time.dateTime = post.ts;
    time.textContent = formatTime(post.ts);

    meta.appendChild(badge);
    meta.appendChild(time);
    a.appendChild(quote);
    a.appendChild(meta);
    return a;
  }

  var _lastDateLabel = '';

  function dateLabel(isoStr) {
    try {
      var d = new Date(isoStr);
      var now = new Date();
      var today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
      var postDay = new Date(d.getFullYear(), d.getMonth(), d.getDate());
      var diff = Math.round((today - postDay) / 86400000);
      if (diff === 0) return 'Today';
      if (diff === 1) return 'Yesterday';
      return d.toLocaleDateString('en-AU', {
        timeZone: 'Australia/Hobart',
        day: 'numeric', month: 'long', year: 'numeric'
      });
    } catch (e) { return ''; }
  }

  function renderPage() {
    var list = document.getElementById('feed-list');
    var batch = _allPosts.slice(_displayedCount, _displayedCount + PAGE_SIZE);
    batch.forEach(function (post) {
      var label = dateLabel(post.ts);
      if (label && label !== _lastDateLabel) {
        var div = document.createElement('div');
        div.className = 'feed-date-label';
        div.textContent = label;
        list.appendChild(div);
        _lastDateLabel = label;
      }
      list.appendChild(renderCard(post));
    });
    _displayedCount += batch.length;
    updateLoadMore();
  }

  function updateLoadMore() {
    var existing = document.getElementById('feed-load-more');
    if (existing) existing.remove();
    if (_displayedCount >= _allPosts.length) return;
    var remaining = _allPosts.length - _displayedCount;
    var btn = document.createElement('button');
    btn.id = 'feed-load-more';
    btn.className = 'feed-load-more-btn';
    btn.textContent = 'Load more (' + remaining + ' remaining)';
    btn.onclick = function () { renderPage(); };
    document.getElementById('feed-list').after(btn);
  }

  function render(data) {
    var list = document.getElementById('feed-list');
    var empty = document.getElementById('feed-empty');
    var posts = (data && data.posts) || [];

    // Newest first
    _allPosts = posts.slice().reverse();
    _displayedCount = 0;
    _lastDateLabel = '';

    // Clear existing children
    while (list.firstChild) list.removeChild(list.firstChild);

    if (_allPosts.length === 0) {
      empty.hidden = false;
      return;
    }

    empty.hidden = true;
    renderPage();

    // Update OG description with latest thought
    var ogDesc = document.querySelector('meta[property="og:description"]');
    if (ogDesc && _allPosts[0]) {
      ogDesc.content = _allPosts[0].thought.substring(0, 160);
    }
  }

  function showError() {
    var list = document.getElementById('feed-list');
    while (list.firstChild) list.removeChild(list.firstChild);
    var p = document.createElement('div');
    p.className = 'feed-empty';
    var msg = document.createElement('p');
    msg.textContent = "Could not load thoughts. SPARK's Pi might be offline.";
    p.appendChild(msg);
    list.appendChild(p);
  }

  // ── Cache helpers ──────────────────────────────────────────────────────────

  function cacheFeed(data) {
    try { localStorage.setItem(CACHE_KEY, JSON.stringify(data)); }
    catch (_) {}
  }

  function loadCachedFeed() {
    try { return JSON.parse(localStorage.getItem(CACHE_KEY)); }
    catch (_) { return null; }
  }

  // ── Offline banner ─────────────────────────────────────────────────────────

  function showOfflineBanner(source) {
    var existing = document.getElementById('feed-offline-banner');
    if (existing) return;
    var banner = document.createElement('div');
    banner.id = 'feed-offline-banner';
    banner.className = 'feed-offline-banner';
    banner.textContent = 'Showing ' + source + ' data — SPARK\'s Pi is currently offline.';
    var list = document.getElementById('feed-list');
    list.parentNode.insertBefore(banner, list);
  }

  // ── Fallback chain ─────────────────────────────────────────────────────────

  function loadFallback() {
    // 1. Try localStorage cache (instant)
    var cached = loadCachedFeed();
    if (cached && cached.posts && cached.posts.length) {
      render(cached);
      showOfflineBanner('cached');
      return;
    }

    // 2. Try same-origin static snapshot (Cloudflare Pages)
    fetchJSON(FALLBACK_LOCAL + '/feed.json')
      .then(function (data) {
        render(data);
        showOfflineBanner('snapshot');
      })
      .catch(function () {
        // 3. Try GitHub raw
        fetchJSON(FALLBACK_GITHUB + '/feed.json')
          .then(function (data) {
            render(data);
            showOfflineBanner('snapshot');
          })
          .catch(showError);
      });
  }

  function init() {
    fetchJSON(API + '/feed')
      .then(function (data) { cacheFeed(data); render(data); })
      .catch(function () {
        // Fallback: try /thoughts endpoint (still live API)
        fetchJSON(API + '/thoughts?limit=50')
          .then(function (thoughts) {
            var data = {
              posts: thoughts.map(function (t) {
                return { ts: t.ts, thought: t.thought, mood: t.mood };
              })
            };
            cacheFeed(data);
            render(data);
          })
          .catch(loadFallback);
      });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
