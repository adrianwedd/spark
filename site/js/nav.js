// nav.js — scroll spy + mobile hamburger toggle
(function () {
  // ── Scroll spy ────────────────────────────────────────────────────────────
  const sections = document.querySelectorAll('section[id]');
  const links = document.querySelectorAll('nav .links a');

  function onScroll() {
    let current = '';
    sections.forEach(sec => {
      const top = sec.getBoundingClientRect().top;
      if (top <= 80) current = sec.id;
    });
    links.forEach(a => {
      a.classList.toggle('active', a.getAttribute('href') === '#' + current);
    });
  }

  document.addEventListener('scroll', onScroll, { passive: true });
  onScroll();

  // ── Mobile hamburger toggle ───────────────────────────────────────────────
  const nav = document.getElementById('main-nav');
  const burger = document.getElementById('nav-burger');

  if (nav && burger) {
    burger.addEventListener('click', function () {
      const open = nav.classList.toggle('nav-open');
      burger.setAttribute('aria-expanded', String(open));
    });

    // Close menu when any nav link is tapped
    document.querySelectorAll('#nav-links a').forEach(function (a) {
      a.addEventListener('click', function () {
        nav.classList.remove('nav-open');
        burger.setAttribute('aria-expanded', 'false');
      });
    });
  }
})();
