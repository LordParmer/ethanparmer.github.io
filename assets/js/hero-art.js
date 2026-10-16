// Generative hero study, shown only when the hero has no portrait: flowing
// data streams with packets moving along them. No paint splatter: it reads
// as blood. Loaded by extend_footer.html on the homepage.
(() => {
  const canvas = document.getElementById("hero-art");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const TAU = Math.PI * 2;

  // Seeded RNG so the composition is the same on every visit.
  const makeRng = (seed) => () => {
    seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
  const gauss = (rnd) => (rnd() + rnd() + rnd() + rnd() - 2) / 2;

  let W = 0, H = 0, dpr = 1, colors = {}, streams = [], particles = [], lineLayer, frame = 0;

  const readColors = () => {
    const s = getComputedStyle(document.documentElement);
    const v = (name) => s.getPropertyValue(name).trim();
    colors = { ink: v("--primary"), red: v("--accent"), cyan: v("--accent-2") };
  };

  // Catmull-Rom spline through points, sampled into a dense polyline.
  const spline = (pts, steps = 40) => {
    const out = [];
    for (let i = 0; i < pts.length - 1; i++) {
      const p0 = pts[i - 1] || pts[i], p1 = pts[i], p2 = pts[i + 1], p3 = pts[i + 2] || p2;
      for (let s = 0; s < steps; s++) {
        const t = s / steps, t2 = t * t, t3 = t2 * t;
        out.push([
          0.5 * (2 * p1[0] + (-p0[0] + p2[0]) * t + (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * t2 + (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * t3),
          0.5 * (2 * p1[1] + (-p0[1] + p2[1]) * t + (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t2 + (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t3),
        ]);
      }
    }
    out.push(pts[pts.length - 1]);
    return out;
  };

  const build = () => {
    const rect = canvas.getBoundingClientRect();
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    W = rect.width; H = rect.height;
    canvas.width = Math.round(W * dpr);
    canvas.height = Math.round(H * dpr);
    readColors();

    const rnd = makeRng(1337);
    const m = Math.min(W, H);
    streams = [];
    for (let i = 0; i < 9; i++) {
      const y0 = H * (0.3 + rnd() * 0.55);
      streams.push({
        pts: spline([[-10, y0], [W * 0.3, y0 + gauss(rnd) * m * 0.06], [W * 0.62, H * 0.42 + gauss(rnd) * m * 0.05], [W + 10, H * (0.3 + rnd() * 0.55)]]),
        hot: i % 3 === 0,
      });
    }

    lineLayer = document.createElement("canvas");
    lineLayer.width = canvas.width;
    lineLayer.height = canvas.height;
    const lc = lineLayer.getContext("2d");
    lc.scale(dpr, dpr);
    for (const s of streams) {
      lc.strokeStyle = s.hot ? colors.cyan : colors.ink;
      lc.globalAlpha = s.hot ? 0.6 : 0.18;
      lc.lineWidth = s.hot ? 1.2 : 0.9;
      lc.beginPath();
      lc.moveTo(s.pts[0][0], s.pts[0][1]);
      for (const p of s.pts) lc.lineTo(p[0], p[1]);
      lc.stroke();
    }

    particles = [];
    for (let i = 0; i < 28; i++) {
      particles.push({ s: streams[i % streams.length], t: rnd(), v: 0.0005 + rnd() * 0.001, red: rnd() < 0.15 });
    }
  };

  const draw = () => {
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(lineLayer, 0, 0);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    for (const p of particles) {
      const pts = p.s.pts, pt = pts[Math.floor(p.t * (pts.length - 1))];
      ctx.fillStyle = p.red ? colors.red : colors.cyan;
      ctx.globalAlpha = 0.85;
      ctx.beginPath();
      ctx.arc(pt[0], pt[1], p.red ? 1.9 : 1.4, 0, TAU);
      ctx.fill();
    }
    ctx.globalAlpha = 1;
  };

  const tick = () => {
    for (const p of particles) p.t = (p.t + p.v) % 1;
    draw();
    frame = requestAnimationFrame(tick);
  };

  // Only animate while the art is on screen, to save battery.
  let visible = true;
  const run = () => {
    cancelAnimationFrame(frame);
    if (reduceMotion || !visible) draw();
    else tick();
  };
  const start = () => {
    build();
    run();
  };
  // The observer can fire before the first build; start() handles that case.
  new IntersectionObserver(([entry]) => {
    visible = entry.isIntersecting;
    if (lineLayer) run();
  }).observe(canvas);

  let resizeTimer;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(start, 150);
  });
  // Rebuild when the light/dark toggle changes the palette.
  new MutationObserver(start).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  (document.fonts ? document.fonts.ready : Promise.resolve()).then(start);
})();
