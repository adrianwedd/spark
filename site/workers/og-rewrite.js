/**
 * Cloudflare Worker: rewrite OG meta tags for:
 *   - /thought/?ts=<timestamp>  — per-thought card image
 *   - /blog/?id=<id>            — blog post title + description
 *
 * Social crawlers (Bluesky, Twitter, Facebook) don't execute JS, so the
 * client-side OG updates in thought.js / blog.js are invisible to them.
 * This worker intercepts matching requests and injects correct meta tags
 * into the HTML response before it reaches the crawler.
 *
 * NOTE: Both routes require zone-deployed Cloudflare routing to this Worker.
 * The /blog/* route must be added alongside the existing /thought/* route.
 */

const API_BASE = 'https://spark-api.wedd.au/api/v1/public';

// Validate ts looks like an ISO timestamp (prevent XSS injection into HTML attributes)
const TS_PATTERN = /^[\d\-T:+.Z]+$/;

// Validate blog id: e.g. blog-2026-03-25-daily, blog-2026-03-25-essay-2
const BLOG_ID_PATTERN = /^blog-[\d\-]+-[a-z_]+(-\d+)?$/;

function escapeHtmlAttr(s) {
  return s.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

/**
 * Rewrite og:image, og:image:width, og:image:height, and twitter:image tags.
 */
function rewriteOgImage(html, imageUrl) {
  html = html.replace(
    /<meta property="og:image" content="[^"]*">/,
    `<meta property="og:image" content="${imageUrl}">`
  );
  html = html.replace(
    /<meta property="og:image:width" content="[^"]*">/,
    '<meta property="og:image:width" content="1080">'
  );
  html = html.replace(
    /<meta property="og:image:height" content="[^"]*">/,
    '<meta property="og:image:height" content="1080">'
  );
  html = html.replace(
    /<meta name="twitter:image" content="[^"]*">/,
    `<meta name="twitter:image" content="${imageUrl}">`
  );
  return html;
}

/**
 * Rewrite og:title and og:description tags.
 */
function rewriteOgText(html, title, description) {
  if (title) {
    const safeTitle = escapeHtmlAttr(title);
    html = html.replace(
      /<meta property="og:title" content="[^"]*">/,
      `<meta property="og:title" content="${safeTitle}">`
    );
    html = html.replace(
      /<meta name="twitter:title" content="[^"]*">/,
      `<meta name="twitter:title" content="${safeTitle}">`
    );
  }
  if (description) {
    const safeDesc = escapeHtmlAttr(description);
    html = html.replace(
      /<meta property="og:description" content="[^"]*">/,
      `<meta property="og:description" content="${safeDesc}">`
    );
    html = html.replace(
      /<meta name="twitter:description" content="[^"]*">/,
      `<meta name="twitter:description" content="${safeDesc}">`
    );
  }
  return html;
}

export default {
  // NOTE: This Worker relies on zone-deployed routing:
  //   spark.wedd.au/thought/* → Worker  (existing)
  //   spark.wedd.au/blog/*    → Worker  (add this route in Cloudflare dashboard)
  // Cloudflare prevents recursive Worker invocation on the same zone, so fetch(request)
  // hits the origin server, not this Worker again.
  async fetch(request) {
    const url = new URL(request.url);
    const isThought = url.pathname.startsWith('/thought/') || url.pathname === '/thought';
    const isBlog = url.pathname.startsWith('/blog/') || url.pathname === '/blog';

    if (!isThought && !isBlog) {
      return fetch(request);
    }

    // ── /thought/?ts=<iso> ────────────────────────────────────────────────
    if (isThought) {
      const ts = url.searchParams.get('ts');
      if (!ts || ts.length > 200 || !TS_PATTERN.test(ts)) {
        return fetch(request);
      }

      const response = await fetch(request);
      const contentType = response.headers.get('content-type') || '';
      if (!contentType.includes('text/html')) return response;

      const imageUrl = escapeHtmlAttr(`${API_BASE}/thought-image?ts=${encodeURIComponent(ts)}`);

      // Probe the image URL — if API is down, serve original HTML with default og:image
      try {
        const probe = await fetch(imageUrl, { method: 'HEAD', cf: { cacheTtl: 300 } });
        if (!probe.ok) return response;
      } catch (_) {
        return response;
      }

      let html = await response.text();
      html = rewriteOgImage(html, imageUrl);

      return new Response(html, {
        status: response.status,
        headers: {
          ...Object.fromEntries(response.headers),
          'content-type': 'text/html; charset=utf-8',
        },
      });
    }

    // ── /blog/?id=<id> ────────────────────────────────────────────────────
    if (isBlog) {
      const id = url.searchParams.get('id');
      if (!id || id.length > 100 || !BLOG_ID_PATTERN.test(id)) {
        return fetch(request);
      }

      // Fetch blog post data from API
      let postTitle = null;
      let postDesc = null;
      try {
        const apiResp = await fetch(`${API_BASE}/blog`, { cf: { cacheTtl: 60 } });
        if (apiResp.ok) {
          const data = await apiResp.json();
          // data.posts is an array; find matching post by id
          const posts = Array.isArray(data.posts) ? data.posts : [];
          const post = posts.find(p => p.id === id);
          if (post) {
            postTitle = post.title ? String(post.title).slice(0, 200) : null;
            const body = post.body ? String(post.body) : '';
            postDesc = body.slice(0, 160) + (body.length > 160 ? '…' : '');
          }
        }
      } catch (_) {
        // API unavailable — fall through to unmodified page
      }

      const response = await fetch(request);
      const contentType = response.headers.get('content-type') || '';
      if (!contentType.includes('text/html')) return response;

      // If we couldn't find post data, serve original HTML unchanged
      if (!postTitle && !postDesc) return response;

      let html = await response.text();
      html = rewriteOgText(html, postTitle, postDesc);

      return new Response(html, {
        status: response.status,
        headers: {
          ...Object.fromEntries(response.headers),
          'content-type': 'text/html; charset=utf-8',
        },
      });
    }

    return fetch(request);
  },
};
