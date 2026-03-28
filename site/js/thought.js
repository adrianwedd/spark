/* Individual thought permalink page — fetches by ?ts= parameter.
   Fallback chain: static snapshot → live API → GitHub raw → localStorage */
(function () {
  'use strict';

  var API = window.SPARK_CONFIG.API_BASE;
  var FALLBACK_LOCAL = window.SPARK_CONFIG.FALLBACK_LOCAL;
  var FALLBACK_GITHUB = window.SPARK_CONFIG.FALLBACK_GITHUB;
  var TIMEOUT_MS = 8000;

  var VALID_MOODS = ['peaceful','content','contemplative','curious','active','excited',
    'alert','playful','mischievous','bored','lonely','grumpy','anxious'];

  var _allPosts = [];

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
    } catch (e) { return ''; }
  }

  function moodClass(mood) {
    var m = (mood || '').toLowerCase();
    return VALID_MOODS.indexOf(m) >= 0 ? 'mood-' + m : 'mood-content';
  }

  function _moodCSSColor(mood) {
    if (!mood) return '';
    return getComputedStyle(document.documentElement)
      .getPropertyValue('--mood-' + mood.toLowerCase()).trim() || '';
  }

  function findByTs(posts, targetTs) {
    for (var i = 0; i < posts.length; i++) {
      if (posts[i].ts === targetTs) return posts[i];
    }
    try {
      var target = new Date(targetTs).getTime();
      for (var j = 0; j < posts.length; j++) {
        if (Math.abs(new Date(posts[j].ts).getTime() - target) < 2000) return posts[j];
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

    // Mood-colored rule
    var moodColor = _moodCSSColor(post.mood);
    var rule = document.querySelector('.thought-mood-rule');
    if (rule && moodColor) rule.style.borderTopColor = moodColor;

    // Mood wash background
    var page = document.querySelector('.thought-page');
    if (page && moodColor && /^#[0-9a-fA-F]{6}$/.test(moodColor)) {
      var r = parseInt(moodColor.slice(1,3),16);
      var g = parseInt(moodColor.slice(3,5),16);
      var b = parseInt(moodColor.slice(5,7),16);
      page.style.background = 'radial-gradient(ellipse at 50% 0%, rgba(' + r + ',' + g + ',' + b + ',0.25) 0%, transparent 50%)';
    }

    // Prev/next navigation
    var navEl = document.getElementById('thought-nav');
    var prevLink = document.getElementById('thought-prev');
    var nextLink = document.getElementById('thought-next');
    if (navEl && _allPosts.length > 1) {
      var idx = -1;
      for (var i = 0; i < _allPosts.length; i++) {
        if (_allPosts[i].ts === post.ts) { idx = i; break; }
      }
      if (idx < 0) {
        try {
          var target = new Date(post.ts).getTime();
          for (var j = 0; j < _allPosts.length; j++) {
            if (Math.abs(new Date(_allPosts[j].ts).getTime() - target) < 2000) { idx = j; break; }
          }
        } catch (e) {}
      }
      if (idx >= 0) {
        if (idx > 0) {
          prevLink.href = '/thought/?ts=' + encodeURIComponent(_allPosts[idx - 1].ts);
          prevLink.hidden = false;
        } else {
          prevLink.hidden = true;
        }
        if (idx < _allPosts.length - 1) {
          nextLink.href = '/thought/?ts=' + encodeURIComponent(_allPosts[idx + 1].ts);
          nextLink.hidden = false;
        } else {
          nextLink.hidden = true;
        }
        navEl.hidden = false;
      }
    }

    // Share links
    var shareEl = document.getElementById('thought-share');
    var bskyLink = document.getElementById('thought-share-bsky');
    var copyBtn = document.getElementById('thought-copy-link');
    if (shareEl) {
      var permalink = 'https://spark.wedd.au/thought/?ts=' + encodeURIComponent(post.ts);
      var shareText = '\u201c' + (post.thought || '').substring(0, 200) + '\u201d \u2014 SPARK ' + permalink;
      if (bskyLink) bskyLink.href = 'https://bsky.app/intent/compose?text=' + encodeURIComponent(shareText);
      if (copyBtn) {
        copyBtn.onclick = function () {
          navigator.clipboard.writeText(permalink).then(function () {
            copyBtn.textContent = 'Copied!';
            setTimeout(function () { copyBtn.textContent = 'Copy link'; }, 2000);
          });
        };
      }
      shareEl.hidden = false;
    }

    // Update page metadata
    var titleText = post.thought.length > 60 ? post.thought.substring(0, 60) + '\u2026' : post.thought;
    document.title = titleText + ' \u2014 SPARK';
    var ogTitle = document.querySelector('meta[property="og:title"]');
    if (ogTitle) ogTitle.content = 'SPARK: ' + post.thought.substring(0, 80);
    var ogDesc = document.querySelector('meta[property="og:description"]');
    if (ogDesc) ogDesc.content = post.thought.substring(0, 160);
    var canonical = document.querySelector('link[rel="canonical"]');
    if (canonical && post.ts) canonical.href = 'https://spark.wedd.au/thought/?ts=' + encodeURIComponent(post.ts);
  }

  function showNotFound() {
    document.getElementById('thought-loading').hidden = true;
    document.getElementById('thought-not-found').hidden = false;
  }

  function showOfflineBanner() {
    var existing = document.getElementById('thought-offline-banner');
    if (existing) return;
    var banner = document.createElement('div');
    banner.id = 'thought-offline-banner';
    banner.className = 'feed-offline-banner';
    banner.textContent = 'Showing snapshot data \u2014 SPARK\u2019s Pi is currently offline.';
    var card = document.getElementById('thought-card');
    card.parentNode.insertBefore(banner, card);
  }

  function handleFeedData(data, ts, isOffline) {
    var posts = (data && data.posts) || [];
    _allPosts = posts.slice().reverse();
    var post = findByTs(posts, ts);
    if (post) {
      showThought(post);
      if (isOffline) showOfflineBanner();
      return true;
    }
    return false;
  }

  function init() {
    var params = new URLSearchParams(window.location.search);
    var ts = params.get('ts');
    if (!ts) { showNotFound(); return; }

    // Static-first fallback chain
    fetchJSON(FALLBACK_LOCAL + '/feed.json')
      .then(function (data) {
        if (handleFeedData(data, ts, false)) {
          fetchJSON(API + '/feed')
            .then(function (liveData) { handleFeedData(liveData, ts, false); })
            .catch(function () {});
        } else {
          return tryLiveAPI(ts);
        }
      })
      .catch(function () {
        tryLiveAPI(ts);
      });
  }

  function tryLiveAPI(ts) {
    fetchJSON(API + '/feed')
      .then(function (data) {
        if (!handleFeedData(data, ts, false)) {
          fetchJSON(API + '/thoughts?limit=50')
            .then(function (thoughts) {
              var found = findByTs(thoughts, ts);
              if (found) showThought(found);
              else tryGithub(ts);
            })
            .catch(function () { tryGithub(ts); });
        }
      })
      .catch(function () { tryGithub(ts); });
  }

  function tryGithub(ts) {
    fetchJSON(FALLBACK_GITHUB + '/feed.json')
      .then(function (data) {
        if (!handleFeedData(data, ts, true)) showNotFound();
      })
      .catch(function () {
        try {
          var cached = JSON.parse(localStorage.getItem('spark_feed_cache'));
          if (cached && !handleFeedData(cached, ts, true)) showNotFound();
          else if (!cached) showNotFound();
        } catch (e) { showNotFound(); }
      });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
