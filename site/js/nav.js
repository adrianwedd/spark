// nav.js — scroll spy: marks nav links active as user scrolls
(function () {
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
})();
