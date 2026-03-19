/* Individual thought permalink page — fetches by ?ts= parameter.
   Fallback chain: live API → same-origin snapshot → GitHub raw */
(function () {
  'use strict';

  var API = window.SPARK_CONFIG.API_BASE;
  var FALLBACK_LOCAL = window.SPARK_CONFIG.FALLBACK_LOCAL;
  var FALLBACK_GITHUB = window.SPARK_CONFIG.FALLBACK_GITHUB;
  var TIMEOUT_MS = 8000;

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
        weekday: 'long', day: 'numeric', month: 'long', year: 'numeric',
        hour: 'numeric', minute: '2-digit', hour12: true
      });
    } catch (e) {
      return '';
    }
  }

  function moodClass(mood) {
    var m = (mood || '').toLowerCase();
    return VALID_MOODS.indexOf(m) >= 0 ? 'mood-' + m : 'mood-content';
  }

  function findByTs(posts, targetTs) {
    for (var i = 0; i < posts.length; i++) {
      if (posts[i].ts === targetTs) return posts[i];
    }
    // Fuzzy match: compare as Date objects (handles timezone differences)
    try {
      var target = new Date(targetTs).getTime();
      for (var j = 0; j < posts.length; j++) {
        if (Math.abs(new Date(posts[j].ts).getTime() - target) < 2000) {
          return posts[j];
        }
      }
    } catch (e) {}
    return null;
  }

  function showThought(post) {
    document.getElementById('thought-loading').hidden = true;
    document.getElementById('thought-not-found').hidden = true;

    var card = document.getElementById('thought-card');
    card.hidden = false;

    document.getElementById('thought-text').textContent = post.thought;

    var badge = document.getElementById('thought-mood');
    badge.className = 'mood-badge ' + moodClass(post.mood);
    badge.textContent = post.mood || 'thinking';

    var time = document.getElementById('thought-time');
    time.dateTime = post.ts;
    time.textContent = formatTime(post.ts);

    // Update page metadata
    document.title = (post.thought.substring(0, 60)) + '... \u2014 SPARK';
    var ogTitle = document.querySelector('meta[property="og:title"]');
    if (ogTitle) ogTitle.content = 'SPARK: ' + post.thought.substring(0, 80);
    var ogDesc = document.querySelector('meta[property="og:description"]');
    if (ogDesc) ogDesc.content = post.thought.substring(0, 160);
    var ogImg = document.querySelector('meta[property="og:image"]');
    if (ogImg) ogImg.content = API + '/thought-image?ts=' + encodeURIComponent(post.ts);
    var metaDesc = document.querySelector('meta[name="description"]');
    if (metaDesc) metaDesc.content = post.thought.substring(0, 160);
  }

  function showNotFound() {
    document.getElementById('thought-loading').hidden = true;
    document.getElementById('thought-not-found').hidden = false;
  }

  function showOffline() {
    var el = document.getElementById('thought-loading');
    el.textContent = "Could not reach SPARK. The Pi might be offline.";
  }

  // ── Offline banner ─────────────────────────────────────────────────────────

  function showOfflineBanner() {
    var existing = document.getElementById('thought-offline-banner');
    if (existing) return;
    var banner = document.createElement('div');
    banner.id = 'thought-offline-banner';
    banner.style.cssText = 'background:#1e293b;color:#94a3b8;text-align:center;padding:8px 16px;font-size:0.85rem;border-radius:8px;margin-bottom:16px;';
    banner.textContent = 'Showing snapshot data — SPARK\'s Pi is currently offline.';
    var card = document.getElementById('thought-card');
    card.parentNode.insertBefore(banner, card);
  }

  // ── Fallback chain ─────────────────────────────────────────────────────────

  function searchFeedData(data, ts) {
    return findByTs(data.posts || [], ts);
  }

  function loadFromFallback(ts) {
    // 1. Same-origin static snapshot
    fetchJSON(FALLBACK_LOCAL + '/feed.json')
      .then(function (data) {
        var post = searchFeedData(data, ts);
        if (post) { showThought(post); showOfflineBanner(); }
        else { return tryGithub(ts); }
      })
      .catch(function () { tryGithub(ts); });
  }

  function tryGithub(ts) {
    fetchJSON(FALLBACK_GITHUB + '/feed.json')
      .then(function (data) {
        var post = searchFeedData(data, ts);
        if (post) { showThought(post); showOfflineBanner(); }
        else showNotFound();
      })
      .catch(showOffline);
  }

  function init() {
    var params = new URLSearchParams(window.location.search);
    var ts = params.get('ts');

    if (!ts) {
      showNotFound();
      return;
    }

    // Try feed first, then thoughts endpoint
    fetchJSON(API + '/feed')
      .then(function (data) {
        var post = findByTs(data.posts || [], ts);
        if (post) {
          showThought(post);
        } else {
          // Fallback: search in thoughts endpoint
          return fetchJSON(API + '/thoughts?limit=50').then(function (thoughts) {
            var found = findByTs(thoughts, ts);
            if (found) {
              showThought(found);
            } else {
              showNotFound();
            }
          });
        }
      })
      .catch(function () {
        // API offline — try /thoughts then static fallbacks
        fetchJSON(API + '/thoughts?limit=50')
          .then(function (thoughts) {
            var found = findByTs(thoughts, ts);
            if (found) showThought(found);
            else loadFromFallback(ts);
          })
          .catch(function () { loadFromFallback(ts); });
      });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
