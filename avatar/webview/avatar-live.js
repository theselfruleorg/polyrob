/* Read-only live avatar embed for the POLYROB console.
 * Loads AFTER avatar/mindprint.js (which set window.Mindprint), fetches the
 * instance's frozen /pfp.json, and animates the EXACT engine on a <canvas>.
 * Progressive enhancement: if there is no avatar / no engine / reduced motion,
 * it leaves the <img src="/pfp.png"> fallback in place. */
(async function () {
  "use strict";
  var cv = document.getElementById("agent-avatar");
  if (!cv || typeof window.Mindprint === "undefined") return;

  var cfg;
  try {
    var res = await fetch("/pfp.json");
    if (!res.ok) return;            // 404 -> no avatar yet, keep the fallback
    cfg = await res.json();
  } catch (e) { return; }
  if (!cfg || !cfg.seed) return;

  var SHAPES = window.SHAPES || ["dot", "square", "scanline"];
  var mp = new window.Mindprint((cfg.seed || "") + (cfg.variant || ""));
  var ov = Object.assign({}, cfg.override || {});
  if (typeof ov.shape === "string") ov.shape = SHAPES.indexOf(ov.shape); // name -> index
  Object.assign(mp.override, ov);

  var ctx = cv.getContext("2d");
  var size = cv.width;

  // the live canvas is up: reveal it, hide the <img> fallback
  var img = document.getElementById("agent-avatar-fallback");
  if (img) img.style.display = "none";
  cv.style.display = "block";

  var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduce) { mp.N = 0; mp.render(ctx, size, 1.0, 0, { still: true }); mp.N = 0; return; }

  var t0 = null, running = true;
  document.addEventListener("visibilitychange", function () { running = !document.hidden; });
  function loop(ts) {
    if (t0 === null) t0 = ts;
    if (running) mp.render(ctx, size, (ts - t0) / 1000, 0, { still: false });
    requestAnimationFrame(loop);
  }
  requestAnimationFrame(loop);
})();
