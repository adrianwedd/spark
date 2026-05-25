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
 * Both routes are wired in wrangler.toml and deployed to the spark-og-rewrite
 * Worker. Cloudflare prevents recursive invocation on the same zone, so
 * fetch(request) hits the origin server, not this Worker again.
 *
 * Uses HTMLRewriter (Cloudflare Workers streaming HTML parser) rather than
 * regex to avoid breakage on `>` inside quoted attribute values.
 */

const API_BASE = 'https://spark-api.wedd.au/api/v1/public';

// Validate ts looks like an ISO timestamp (prevent open-redirect via query param)
const TS_PATTERN = /^[\d\-T:+.Z]+$/;

// Validate blog id: e.g. blog-20260325-daily, blog-2026w13-weekly, blog-202603-monthly
const BLOG_ID_PATTERN = /^blog-[\dw\-]+-[a-z_]+(-\d+)?$/;

/**
 * HTMLRewriter element handler that rewrites OG/Twitter meta tag attributes.
 * setAttribute() in HTMLRewriter HTML-encodes values automatically.
 */
class MetaRewriter {
  constructor({ imageUrl = null, title = null, description = null }) {
    this.imageUrl = imageUrl;
    this.title = title;
    this.description = description;
  }

  element(el) {
    const prop = el.getAttribute('property');
    const name = el.getAttribute('name');

    if (this.imageUrl) {
      if (prop === 'og:image') el.setAttribute('content', this.imageUrl);
      if (prop === 'og:image:width') el.setAttribute('content', '1080');
      if (prop === 'og:image:height') el.setAttribute('content', '1080');
      if (name === 'twitter:image') el.setAttribute('content', this.imageUrl);
    }
    if (this.title) {
      if (prop === 'og:title') el.setAttribute('content', this.title);
      if (name === 'twitter:title') el.setAttribute('content', this.title);
    }
    if (this.description) {
      if (prop === 'og:description') el.setAttribute('content', this.description);
      if (name === 'twitter:description') el.setAttribute('content', this.description);
    }
  }
}

export default {
  // Routes (wrangler.toml):
  //   spark.wedd.au/thought/* → Worker
  //   spark.wedd.au/blog/*    → Worker
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

      const imageUrl = `${API_BASE}/thought-image?ts=${encodeURIComponent(ts)}`;

      // Probe the image URL — if API is down, serve original HTML with default og:image
      try {
        const probe = await fetch(imageUrl, { method: 'HEAD', cf: { cacheTtl: 300 } });
        if (!probe.ok) return response;
      } catch (_) {
        return response;
      }

      return new HTMLRewriter()
        .on('meta', new MetaRewriter({ imageUrl }))
        .transform(response);
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

      return new HTMLRewriter()
        .on('meta', new MetaRewriter({ title: postTitle, description: postDesc }))
        .transform(response);
    }

    return fetch(request);
  },
};
