// scroll-animate.js — IntersectionObserver entrance animations
(function () {
  'use strict';
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

  var observer = new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      if (entry.isIntersecting) {
        entry.target.classList.add('animate-in');
        observer.unobserve(entry.target);
      }
    });
  }, { threshold: 0.1, rootMargin: '0px 0px -40px 0px' });

  function init() {
    document.querySelectorAll('.feed-card, .thought-centrepiece, .warm-card, .band, section > .container, section > .container-hero, section > .container-narrow').forEach(function (el, i) {
      el.classList.add('animate-ready');
      if (el.classList.contains('feed-card')) {
        el.style.transitionDelay = Math.min(i * 50, 250) + 'ms';
      }
      observer.observe(el);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
