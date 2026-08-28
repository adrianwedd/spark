// init.js — small static-page initialisation.
// Extracted from index.html to comply with CSP script-src 'self' (no inline scripts).

document.addEventListener('DOMContentLoaded', function () {
  hljs.highlightAll();

  // The homepage roadmap is an editorial summary, not a second work ledger.
  // GitHub Issues remain canonical. Keep this deliberately short and specific
  // to the persistent robot SPARK is now.
  const roadmap = document.querySelector('#roadmap .container');
  if (roadmap) {
    roadmap.innerHTML = `
      <h2>// roadmap</h2>
      <p class="text-muted-dark mb-md">Deepen one persistent robot before building a generic robotics platform. GitHub Issues are the work ledger.</p>

      <div class="roadmap-group">
        <h3>Now — deepen the robot we already have</h3>
        <div class="roadmap-item"><span class="check">⬜</span> Separate Obi companion UI from Adrian/admin UI — <a href="https://github.com/adrianwedd/spark/issues/188">#188</a></div>
        <div class="roadmap-item"><span class="check">⬜</span> Explain “why did you do/say that?” from evidence → proposal → policy → action</div>
        <div class="roadmap-item"><span class="check">⬜</span> Retire canned pseudo-agency and mood-targeted theatrics — <a href="https://github.com/adrianwedd/spark/issues/187">#187</a></div>
        <div class="roadmap-item"><span class="check">⬜</span> Finish GPIO lease migration so normal tools stop terminating px-alive — <a href="https://github.com/adrianwedd/spark/issues/193">#193</a></div>
        <div class="roadmap-item"><span class="check">⬜</span> Make quiet state attributable/bounded and finish the audio policy boundary — <a href="https://github.com/adrianwedd/spark/issues/209">#209</a> / <a href="https://github.com/adrianwedd/spark/issues/207">#207</a></div>
        <div class="roadmap-item"><span class="check">⬜</span> Make missing perception explicit and define retention/disclosure boundaries — <a href="https://github.com/adrianwedd/spark/issues/191">#191</a> / <a href="https://github.com/adrianwedd/spark/issues/173">#173</a></div>
        <div class="roadmap-item"><span class="check">⬜</span> Reduce wake-listener memory and swap-driven I/O pressure — <a href="https://github.com/adrianwedd/spark/issues/219">#219</a> / <a href="https://github.com/adrianwedd/spark/issues/247">#247</a></div>
      </div>

      <div class="roadmap-group">
        <h3>Next — make persistence physical</h3>
        <div class="roadmap-item"><span class="check">⬜</span> Persistent spatial memory: rooms, landmarks, paths and uncertainty that survive restarts</div>
        <div class="roadmap-item"><span class="check">⬜</span> Lightweight simulation / fossil replay for awareness → policy → action regressions</div>
        <div class="roadmap-item"><span class="check">⬜</span> Predictive operational health from battery, memory, latency and service history</div>
        <div class="roadmap-item"><span class="check">⬜</span> Teach-with-SPARK: Obi learns Python by changing the physical robot — <a href="https://github.com/adrianwedd/spark/issues/32">#32</a></div>
        <div class="roadmap-item"><span class="check">⬜</span> Physical play modes built on current leases/policy: face-follow, obstacle course, custom sounds</div>
      </div>

      <div class="roadmap-group">
        <h3>Later — autonomy with somewhere to go</h3>
        <div class="roadmap-item"><span class="check">⬜</span> Autonomous docking + energy awareness</div>
        <div class="roadmap-item"><span class="check">⬜</span> Long-horizon room and landmark memory</div>
        <div class="roadmap-item"><span class="check">⬜</span> Bounded self-maintenance: detect, explain, safely recover, escalate</div>
        <div class="roadmap-item"><span class="check">⬜</span> More cognition local where acceleration improves latency, memory or cost without weakening boundaries</div>
        <div class="roadmap-item"><span class="check">⬜</span> Portable persistent self across replacement hardware</div>
      </div>

      <p class="text-muted-dark mt-md"><a href="https://github.com/adrianwedd/spark/blob/master/docs/ROADMAP.md">Full issue-linked roadmap ↗</a></p>
    `;
  }
});
