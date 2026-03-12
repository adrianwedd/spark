// init.js — initialises highlight.js after the bundle is loaded
// Extracted from index.html to comply with CSP script-src 'self' (no inline scripts).
document.addEventListener('DOMContentLoaded', function () {
  hljs.highlightAll();
});
