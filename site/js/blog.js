/* Blog page — fetches /api/v1/public/blog and renders post cards.
   Supports individual post view via ?id= query param.
   Fallback chain: live API → same-origin snapshot → localStorage cache */
(function () {
  'use strict';

  var API = window.SPARK_CONFIG.API_BASE;
  var FALLBACK_LOCAL = window.SPARK_CONFIG.FALLBACK_LOCAL;
  var FALLBACK_GITHUB = window.SPARK_CONFIG.FALLBACK_GITHUB;
  var TIMEOUT_MS = 8000;
  var CACHE_KEY = 'spark_blog_cache';
  var PAGE_SIZE = 10;
  var _allPosts = [];
  var _displayedCount = 0;

  // All 12 moods have .mood-{name} classes in feed.css (plus legacy "active")
  var VALID_MOODS = ['peaceful','content','contemplative','curious','active','excited',
    'alert','playful','mischievous','bored','lonely','grumpy','anxious'];

  // Post type display labels and CSS slugs
  var TYPE_LABELS = {
    daily:   'daily',
    weekly:  'weekly',
    monthly: 'monthly',
    yearly:  'yearly',
    essay:   'essay'
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

  function moodClass(mood) {
    var m = (mood || '').toLowerCase();
    return VALID_MOODS.indexOf(m) >= 0 ? 'mood-' + m : 'mood-content';
  }

  function typeLabel(type) {
    return TYPE_LABELS[type] || type || 'post';
  }

  function postURL(post) {
    return '/blog/?id=' + encodeURIComponent(post.id || post.ts);
  }

  // ── Single post view ────────────────────────────────────────────────────────

  function renderSinglePost(post) {
    var list = document.getElementById('feed-list');
    while (list.firstChild) list.removeChild(list.firstChild);

    var article = document.createElement('article');
    article.className = 'feed-card blog-post-full';

    // Breadcrumb back link
    var back = document.createElement('a');
    back.href = '/blog/';
    back.className = 'blog-back-link';
    back.textContent = '← Back to Blog';
    article.appendChild(back);

    // Title
    if (post.title) {
      var h2 = document.createElement('h2');
      h2.className = 'blog-post-title';
      h2.textContent = post.title;
      article.appendChild(h2);
    }

    // Meta row: type badge + mood badge + time
    var meta = document.createElement('div');
    meta.className = 'feed-card-meta';

    if (post.type) {
      var typeBadge = document.createElement('span');
      typeBadge.className = 'blog-type-badge blog-type-' + (post.type || 'post');
      typeBadge.textContent = typeLabel(post.type);
      meta.appendChild(typeBadge);
    }

    var moodBadge = document.createElement('span');
    moodBadge.className = 'mood-badge ' + moodClass(post.mood || post.mood_summary);
    moodBadge.textContent = post.mood || post.mood_summary || 'thinking';
    meta.appendChild(moodBadge);

    var time = document.createElement('time');
    time.dateTime = post.ts || post.created_at || '';
    time.textContent = formatTime(post.ts || post.created_at || '');
    meta.appendChild(time);

    article.appendChild(meta);

    // Full body
    var body = document.createElement('div');
    body.className = 'blog-post-body';
    // Render newlines as paragraphs
    var text = post.body || post.thought || '';
    text.split(/\n{2,}/).forEach(function (para) {
      var p = document.createElement('p');
      p.textContent = para.trim();
      if (p.textContent) body.appendChild(p);
    });
    article.appendChild(body);

    list.appendChild(article);
  }

  // ── Card rendering ──────────────────────────────────────────────────────────

  function renderCard(post) {
    var a = document.createElement('a');
    a.className = 'feed-card';
    a.href = postURL(post);

    // Title (bold) if present
    if (post.title) {
      var title = document.createElement('p');
      title.className = 'blog-card-title';
      title.textContent = post.title;
      a.appendChild(title);
    }

    // Body excerpt
    var bodyText = post.body || post.thought || '';
    var excerpt = bodyText.length > 150 ? bodyText.substring(0, 150) + '\u2026' : bodyText;
    var quote = document.createElement('p');
    quote.className = 'feed-card-quote';
    quote.textContent = excerpt;
    a.appendChild(quote);

    // Meta: type badge + mood badge + time
    var meta = document.createElement('div');
    meta.className = 'feed-card-meta';

    if (post.type) {
      var typeBadge = document.createElement('span');
      typeBadge.className = 'blog-type-badge blog-type-' + (post.type || 'post');
      typeBadge.textContent = typeLabel(post.type);
      meta.appendChild(typeBadge);
    }

    var moodBadge = document.createElement('span');
    moodBadge.className = 'mood-badge ' + moodClass(post.mood || post.mood_summary);
    moodBadge.textContent = post.mood || post.mood_summary || 'thinking';
    meta.appendChild(moodBadge);

    var time = document.createElement('time');
    time.dateTime = post.ts || post.created_at || '';
    time.textContent = formatTime(post.ts || post.created_at || '');
    meta.appendChild(time);

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
      var ts = post.ts || post.created_at || '';
      var label = dateLabel(ts);
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

    // Update OG description with latest post title or excerpt
    var ogDesc = document.querySelector('meta[property="og:description"]');
    if (ogDesc && _allPosts[0]) {
      var latest = _allPosts[0];
      var desc = latest.title || (latest.body || latest.thought || '').substring(0, 160);
      ogDesc.content = desc;
    }
  }

  function showError() {
    var list = document.getElementById('feed-list');
    while (list.firstChild) list.removeChild(list.firstChild);
    var p = document.createElement('div');
    p.className = 'feed-empty';
    var msg = document.createElement('p');
    msg.textContent = "Could not load blog posts. SPARK's Pi might be offline.";
    p.appendChild(msg);
    list.appendChild(p);
  }

  // ── Cache helpers ──────────────────────────────────────────────────────────

  function cacheBlog(data) {
    try { localStorage.setItem(CACHE_KEY, JSON.stringify(data)); }
    catch (_) {}
  }

  function loadCachedBlog() {
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
    var cached = loadCachedBlog();
    if (cached && cached.posts && cached.posts.length) {
      render(cached);
      showOfflineBanner('cached');
      return;
    }

    // 2. Try same-origin static snapshot (Cloudflare Pages)
    fetchJSON(FALLBACK_LOCAL + '/blog.json')
      .then(function (data) {
        render(data);
        showOfflineBanner('snapshot');
      })
      .catch(function () {
        // 3. Try GitHub raw
        fetchJSON(FALLBACK_GITHUB + '/blog.json')
          .then(function (data) {
            render(data);
            showOfflineBanner('snapshot');
          })
          .catch(showError);
      });
  }

  // ── Single post mode ───────────────────────────────────────────────────────

  function initSinglePost(id) {
    function findAndRender(data, bannerSource) {
      var posts = (data && data.posts) || [];
      for (var i = 0; i < posts.length; i++) {
        if ((posts[i].id && String(posts[i].id) === id) ||
            (posts[i].ts && posts[i].ts === id)) {
          renderSinglePost(posts[i]);
          if (posts[i].title) {
            document.title = posts[i].title + ' — SPARK Blog';
            var ogTitle = document.querySelector('meta[property="og:title"]');
            if (ogTitle) ogTitle.content = posts[i].title;
          }
          // Update canonical to the actual permalink
          var canonical = document.querySelector('link[rel="canonical"]');
          if (canonical) canonical.href = 'https://spark.wedd.au/blog/?id=' + encodeURIComponent(id);
          var empty = document.getElementById('feed-empty');
          if (empty) empty.hidden = true;
          if (bannerSource) showOfflineBanner(bannerSource);
          return true;
        }
      }
      return false;
    }

    fetchJSON(API + '/blog')
      .then(function (data) {
        cacheBlog(data);
        if (!findAndRender(data)) showError();
      })
      .catch(function () {
        // 1. localStorage cache (instant)
        var cached = loadCachedBlog();
        if (cached && findAndRender(cached, 'cached')) return;
        // 2. Static snapshot on Cloudflare Pages
        fetchJSON(FALLBACK_LOCAL + '/blog.json')
          .then(function (data) {
            if (!findAndRender(data, 'snapshot')) showError();
          })
          .catch(function () {
            // 3. GitHub raw mirror
            fetchJSON(FALLBACK_GITHUB + '/blog.json')
              .then(function (data) {
                if (!findAndRender(data, 'snapshot')) showError();
              })
              .catch(showError);
          });
      });
  }

  // ── Init ───────────────────────────────────────────────────────────────────

  function init() {
    // Check for single-post view
    var params = new URLSearchParams(window.location.search);
    var id = params.get('id');
    if (id) {
      initSinglePost(id);
      return;
    }

    // List view
    fetchJSON(API + '/blog')
      .then(function (data) { cacheBlog(data); render(data); })
      .catch(loadFallback);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
