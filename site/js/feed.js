/* Feed page — fetches /api/v1/public/feed and renders post cards */
(function () {
  'use strict';

  var API = window.SPARK_CONFIG.API_BASE;
  var TIMEOUT_MS = 8000;

  var MOOD_CLASSES = {
    peaceful: 'mood-peaceful',
    content: 'mood-content',
    contemplative: 'mood-contemplative',
    curious: 'mood-curious',
    active: 'mood-active',
    excited: 'mood-excited'
  };

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
    return MOOD_CLASSES[(mood || '').toLowerCase()] || 'mood-content';
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

  function render(data) {
    var list = document.getElementById('feed-list');
    var empty = document.getElementById('feed-empty');
    var posts = (data && data.posts) || [];

    // Newest first
    posts = posts.slice().reverse();

    // Clear existing children
    while (list.firstChild) list.removeChild(list.firstChild);

    if (posts.length === 0) {
      empty.hidden = false;
      return;
    }

    empty.hidden = true;
    posts.forEach(function (post) {
      list.appendChild(renderCard(post));
    });

    // Update OG description with latest thought
    var ogDesc = document.querySelector('meta[property="og:description"]');
    if (ogDesc && posts[0]) {
      ogDesc.content = posts[0].thought.substring(0, 160);
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

  function init() {
    fetchJSON(API + '/feed')
      .then(render)
      .catch(function () {
        // Fallback: try /thoughts endpoint
        fetchJSON(API + '/thoughts?limit=50')
          .then(function (thoughts) {
            render({
              posts: thoughts.map(function (t) {
                return { ts: t.ts, thought: t.thought, mood: t.mood };
              })
            });
          })
          .catch(showError);
      });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
