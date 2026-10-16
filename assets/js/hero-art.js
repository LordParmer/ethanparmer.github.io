// Generative hero art. Over a portrait: one spray-paint hit on the shadow side
// of the head, plus a small echo, that drift slightly with the cursor. Without
// a portrait: a standalone generative study of data streams and paint.
(() => {
  const canvas = document.getElementById("hero-art");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const photo = canvas.previousElementSibling?.classList.contains("hero-portrait") ? canvas.previousElementSibling : null;
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const parallax = !reduceMotion && window.matchMedia("(pointer: fine)").matches;
  const TAU = Math.PI * 2;

  // Seeded RNG so the composition is the same on every visit.
  const makeRng = (seed) => () => {
    seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
  const gauss = (rnd) => (rnd() + rnd() + rnd() + rnd() - 2) / 2;
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

  let W = 0, H = 0, dpr = 1, colors = {}, streams = [], particles = [], frame = 0;
  let paintLayer, lineLayer;
  const offset = { x: 0, y: 0 }, target = { x: 0, y: 0 };

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

  const strokePath = (c, pts) => {
    c.beginPath();
    c.moveTo(pts[0][0], pts[0][1]);
    for (const p of pts) c.lineTo(p[0], p[1]);
    c.stroke();
  };

  // A spray-can hit: soft mist, a dense uneven core, flecks thrown along the
  // spray direction, and drips that run, thin out and end in a bead.
  const sprayHit = (c, rnd, x, y, r, color, { drips = 4, angle = -0.4 } = {}) => {
    c.save();
    c.fillStyle = color;
    c.strokeStyle = color;
    for (let i = 0; i < 3200; i++) {
      const a = rnd() * TAU, d = Math.abs(gauss(rnd)) * r * 3.4;
      c.globalAlpha = 0.04 + 0.3 * Math.max(0, 1 - d / (r * 3.4));
      const s = 0.5 + rnd();
      c.fillRect(x + Math.cos(a) * d, y + Math.sin(a) * d * 0.85, s, s);
    }
    c.globalAlpha = 0.2;
    for (let i = 0; i < 320; i++) {
      const a = rnd() * TAU, d = Math.abs(gauss(rnd)) * r * 0.95;
      c.beginPath();
      c.arc(x + Math.cos(a) * d, y + Math.sin(a) * d * 0.8, r * (0.06 + rnd() * 0.16), 0, TAU);
      c.fill();
    }
    c.globalAlpha = 0.85;
    for (let i = 0; i < 46; i++) {
      const a = angle + gauss(rnd) * 1.4, d = r * (1.1 + rnd() * rnd() * 3);
      const s = 0.5 + rnd() * r * 0.05;
      c.beginPath();
      c.ellipse(x + Math.cos(a) * d, y + Math.sin(a) * d, s * (1 + rnd() * 1.6), s, a, 0, TAU);
      c.fill();
    }
    c.lineCap = "round";
    for (let i = 0; i < drips; i++) {
      let dx = x + gauss(rnd) * r * 0.7;
      let dy = y + r * (0.3 + rnd() * 0.35);
      const len = r * (0.5 + rnd() * rnd() * 4.5);
      let w = 0.9 + rnd() * r * 0.07;
      c.globalAlpha = 0.9;
      const steps = Math.ceil(len / 3);
      for (let s = 0; s < steps; s++) {
        const nx = dx + (rnd() - 0.5) * 0.5, ny = dy + len / steps;
        c.lineWidth = w;
        c.beginPath();
        c.moveTo(dx, dy);
        c.lineTo(nx, ny);
        c.stroke();
        dx = nx; dy = ny;
        w = Math.max(0.7, w * 0.985);
      }
      c.beginPath();
      c.arc(dx, dy, w * 1.25, 0, TAU);
      c.fill();
    }
    c.restore();
  };

  // Map a point in the source photo (0-1 fractions) to canvas coordinates,
  // following the image's object-fit: cover and object-position.
  const photoToCanvas = () => {
    const iw = photo.naturalWidth || photo.width, ih = photo.naturalHeight || photo.height;
    const s = Math.max(W / iw, H / ih), dw = iw * s, dh = ih * s;
    const pos = getComputedStyle(photo).objectPosition.split(" ").map((p) => parseFloat(p) / 100);
    const ox = (W - dw) * (pos[0] ?? 0.5), oy = (H - dh) * (pos[1] ?? 0.5);
    return (fx, fy) => [ox + fx * dw, oy + fy * dh];
  };

  const buildPortrait = (rnd, m) => {
    // Face box in the source photo, from data-face="x0,y0,x1,y1".
    const [fx0, fy0, fx1, fy1] = (photo.dataset.face || "0.27,0.15,0.58,0.52").split(",").map(Number);
    const map = photoToCanvas();
    const [x0, y0] = map(fx0, fy0), [x1, y1] = map(fx1, fy1);
    const shadowLeft = (photo.dataset.shadow || "left") === "left";

    // One paint hit in the dark beside the shadowed side of the head, plus a
    // small echo low on the opposite side.
    const r = m * 0.085;
    const px = shadowLeft ? clamp(x0 - r * 1.9, r * 1.2, W) : clamp(x1 + r * 1.9, 0, W - r * 1.2);
    const py = y0 + (y1 - y0) * 0.22;
    const pc = paintLayer.getContext("2d");
    sprayHit(pc, rnd, px, py, r, colors.red, { drips: 5, angle: shadowLeft ? -2.6 : -0.5 });
    sprayHit(pc, rnd, shadowLeft ? W * 0.86 : W * 0.14, H * 0.84, r * 0.32, colors.red, { drips: 1 });
  };

  const buildStudy = (rnd, m) => {
    streams = [];
    for (let i = 0; i < 9; i++) {
      const y0 = H * (0.3 + rnd() * 0.55);
      streams.push({
        pts: spline([[-10, y0], [W * 0.3, y0 + gauss(rnd) * m * 0.06], [W * 0.62, H * 0.42 + gauss(rnd) * m * 0.05], [W + 10, H * (0.3 + rnd() * 0.55)]]),
        hot: i % 3 === 0,
      });
    }
    const lc = lineLayer.getContext("2d");
    for (const s of streams) {
      lc.strokeStyle = s.hot ? colors.cyan : colors.ink;
      lc.globalAlpha = s.hot ? 0.6 : 0.18;
      lc.lineWidth = s.hot ? 1.2 : 0.9;
      strokePath(lc, s.pts);
    }
    const pc = paintLayer.getContext("2d");
    sprayHit(pc, rnd, W * 0.2, H * 0.18, m * 0.07, colors.red, { drips: 5 });
    sprayHit(pc, rnd, W * 0.82, H * 0.78, m * 0.03, colors.cyan, { drips: 1 });

    particles = [];
    for (let i = 0; i < 28; i++) {
      particles.push({ s: streams[i % streams.length], t: rnd(), v: 0.0005 + rnd() * 0.001, red: rnd() < 0.15 });
    }
  };

  const layer = () => {
    const l = document.createElement("canvas");
    l.width = canvas.width;
    l.height = canvas.height;
    l.getContext("2d").scale(dpr, dpr);
    return l;
  };

  const build = () => {
    const rect = canvas.getBoundingClientRect();
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    W = rect.width; H = rect.height;
    canvas.width = Math.round(W * dpr);
    canvas.height = Math.round(H * dpr);
    readColors();
    paintLayer = layer();
    lineLayer = layer();
    streams = [];
    particles = [];

    const rnd = makeRng(1337);
    const m = Math.min(W, H);
    if (photo) buildPortrait(rnd, m);
    else buildStudy(rnd, m);
  };

  const draw = () => {
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(lineLayer, 0, 0);
    ctx.drawImage(paintLayer, offset.x * 6 * dpr, offset.y * 5 * dpr);
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

  // Runs while something is moving: the study's particles, or the paint
  // easing toward the cursor. Over a portrait it stops once the paint has
  // settled and restarts on the next pointer move, so an idle page costs nothing.
  const tick = () => {
    for (const p of particles) p.t = (p.t + p.v) % 1;
    offset.x += (target.x - offset.x) * 0.06;
    offset.y += (target.y - offset.y) * 0.06;
    draw();
    const settled = !particles.length && Math.abs(target.x - offset.x) < 0.002 && Math.abs(target.y - offset.y) < 0.002;
    frame = settled ? 0 : requestAnimationFrame(tick);
  };

  let visible = true;
  const run = () => {
    cancelAnimationFrame(frame);
    frame = 0;
    if (reduceMotion || !visible) draw();
    else tick();
  };

  if (parallax) {
    window.addEventListener("pointermove", (e) => {
      const r = canvas.getBoundingClientRect();
      target.x = clamp((e.clientX - (r.left + r.width / 2)) / r.width, -1, 1);
      target.y = clamp((e.clientY - (r.top + r.height / 2)) / r.height, -1, 1);
      if (!frame && visible && paintLayer) frame = requestAnimationFrame(tick);
    }, { passive: true });
  }

  const start = () => {
    build();
    run();
  };
  // Only animate while the art is on screen, to save battery. The observer can
  // fire before the first build; start() handles that case.
  new IntersectionObserver(([entry]) => {
    visible = entry.isIntersecting;
    if (paintLayer) run();
  }).observe(canvas);

  let resizeTimer;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(start, 150);
  });
  // Rebuild when the light/dark toggle changes the palette.
  new MutationObserver(start).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  const photoReady = !photo || photo.complete ? Promise.resolve() : new Promise((res) => photo.addEventListener("load", res, { once: true }));
  Promise.all([document.fonts ? document.fonts.ready : null, photoReady]).then(start);
})();
