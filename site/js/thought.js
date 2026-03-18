/* Individual thought permalink page — fetches by ?ts= parameter */
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
        weekday: 'long', day: 'numeric', month: 'long', year: 'numeric',
        hour: 'numeric', minute: '2-digit', hour12: true
      });
    } catch (e) {
      return '';
    }
  }

  function moodClass(mood) {
    return MOOD_CLASSES[(mood || '').toLowerCase()] || 'mood-content';
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
        // API offline fallback
        fetchJSON(API + '/thoughts?limit=50')
          .then(function (thoughts) {
            var found = findByTs(thoughts, ts);
            if (found) showThought(found);
            else showNotFound();
          })
          .catch(showOffline);
      });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
