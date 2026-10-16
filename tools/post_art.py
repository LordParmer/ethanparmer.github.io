#!/usr/bin/env python3
"""Draws the artwork for each post: one 1200x630 PNG per post in
assets/images/art/, in the visual language of the homepage hero
(assets/js/hero-art.js): spray-paint hits over flowing data streams.

Hugo turns each PNG into the post's list thumbnail, its header image and its
share image (layouts/_partials/art/). The PNGs are committed, so a Hugo build
needs nothing but Hugo; the deploy workflow also runs this script first, so a
post pushed without its art still gets it.

A post's picture is seeded by its slug (front matter `slug`, else the file or
bundle name), so editing the title never changes it. The paint colour comes
from the post's tags (see TOPICS). Front matter can override both:

    art:
      seed: "any text"     # a different composition
      color: "#4fd6ec"     # paint colour
      accent: "#f2c14e"    # colour of the highlighted streams

Posts with `cover.image` keep their own image and get no art.

    python tools/post_art.py              draw art that is missing or out of date
    python tools/post_art.py --force      redraw everything
    python tools/post_art.py --preview "A title" --tags finance -o out.png

Standard library only (Python 3.9+), so no Pillow or numpy is needed.
"""

import argparse
import json
import math
import re
import struct
import sys
import time
import zlib
from array import array
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTENT = ROOT / "content" / "posts"
OUT = ROOT / "assets" / "images" / "art"
MANIFEST = OUT / "manifest.json"

# Bump after changing the drawing code to redraw every post.
VERSION = 1

W, H = 1200, 630
BACKGROUND = "#0b0c0f"
INK = "#eceae4"
RED, CYAN, GOLD = "#ff3b2b", "#4fd6ec", "#f2c14e"

# Paint colour and highlighted-stream colour per topic. The first topic that
# shares a tag with the post wins; posts with none of these tags get DEFAULT.
# `label` is the line above the title on the share image.
TOPICS = [
    {"name": "gxp", "label": "GxP · Databricks", "color": RED, "accent": CYAN,
     "tags": {"gxp", "data-integrity", "alcoa", "part-11", "validation", "csv",
              "compliance", "healthcare", "life-sciences", "hcls"}},
    {"name": "finance", "label": "Finance · Databricks", "color": GOLD, "accent": CYAN,
     "tags": {"finance", "banking", "reconciliation", "sox", "risk", "regulatory-reporting"}},
    {"name": "lakehouse", "label": "Lakehouse · Databricks", "color": CYAN, "accent": GOLD,
     "tags": {"delta-lake", "delta", "lakehouse", "medallion", "unity-catalog", "lakeflow",
              "spark", "databricks"}},
]
DEFAULT = {"name": "default", "label": "Databricks", "color": RED, "accent": CYAN}

TAU = math.tau
M32 = 0xFFFFFFFF


# ---------------------------------------------------------------- randomness

def make_rng(text):
    """mulberry32, as in hero-art.js, seeded with the FNV-1a hash of `text`."""
    state = 0x811C9DC5
    for byte in text.encode("utf-8"):
        state = ((state ^ byte) * 0x01000193) & M32

    def rnd():
        nonlocal state
        state = (state + 0x6D2B79F5) & M32
        t = ((state ^ (state >> 15)) * (1 | state)) & M32
        t = ((t + ((t ^ (t >> 7)) * (61 | t))) & M32) ^ t
        return ((t ^ (t >> 14)) & M32) / 4294967296

    return rnd


def gauss(rnd):
    return (rnd() + rnd() + rnd() + rnd() - 2) / 2


def lerp(a, b, t):
    return a + (b - a) * t


def clamp(v, lo, hi):
    return min(hi, max(lo, v))


def hex_rgb(value):
    h = value.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


# ----------------------------------------------------------------- rendering

class Mask(dict):
    """Anti-aliased coverage of one colour, keyed by pixel index, so a shape
    built from many pieces is composited once. `add` compounds overlapping
    alpha like repeated paint; `union` keeps the strongest, so a stroke drawn
    as many short segments has no darker joints."""

    def __init__(self, w, h):
        super().__init__()
        self.w, self.h = w, h

    def add(self, i, a):
        m = self.get(i, 0.0)
        self[i] = m + a - m * a

    def union(self, i, a):
        if a > self.get(i, 0.0):
            self[i] = a

    def square(self, x, y, s, a):
        """fillRect(x, y, s, s), weighted by how much of each pixel it covers."""
        w, h = self.w, self.h
        for py in range(max(0, int(y)), min(h, int(math.ceil(y + s)))):
            cy = min(py + 1, y + s) - max(py, y)
            if cy <= 0:
                continue
            row = py * w
            for px in range(max(0, int(x)), min(w, int(math.ceil(x + s)))):
                cx = min(px + 1, x + s) - max(px, x)
                if cx > 0:
                    self.add(row + px, a * cx * cy)

    def disc(self, cx, cy, rad, a, union=False, soft=1.0):
        """A filled circle whose edge fades over `soft` pixels."""
        put = self.union if union else self.add
        if rad < 0.7:
            return self.square(cx - rad, cy - rad, rad * 2, a * math.pi / 4)
        w, h = self.w, self.h
        outer = rad + soft / 2
        for py in range(max(0, int(cy - outer)), min(h, int(cy + outer) + 1)):
            dy = py + 0.5 - cy
            span = outer * outer - dy * dy
            if span <= 0:
                continue
            span = math.sqrt(span)
            row = py * w
            for px in range(max(0, int(cx - span)), min(w, int(cx + span) + 1)):
                dx = px + 0.5 - cx
                cov = (outer - math.sqrt(dx * dx + dy * dy)) / soft
                if cov > 0:
                    put(row + px, a * (cov if cov < 1 else 1.0))

    def ellipse(self, cx, cy, rx, ry, rot, a):
        if rx < 0.7 and ry < 0.7:
            return self.disc(cx, cy, math.sqrt(rx * ry), a)
        w, h = self.w, self.h
        c, s = math.cos(rot), math.sin(rot)
        reach = max(rx, ry) + 1
        thin = min(rx, ry)
        for py in range(max(0, int(cy - reach)), min(h, int(cy + reach) + 1)):
            dy = py + 0.5 - cy
            row = py * w
            for px in range(max(0, int(cx - reach)), min(w, int(cx + reach) + 1)):
                dx = px + 0.5 - cx
                u, v = (dx * c + dy * s) / rx, (dy * c - dx * s) / ry
                cov = (1 - math.sqrt(u * u + v * v)) * thin + 0.5
                if cov > 0:
                    self.add(row + px, a * (cov if cov < 1 else 1.0))

    def segment(self, ax, ay, bx, by, width, a):
        """A line from a to b with round ends. Lines thinner than a pixel get
        proportionally fainter instead of thinner."""
        w, h = self.w, self.h
        half = max(width, 1.0) / 2
        fade = min(width, 1.0)
        vx, vy = bx - ax, by - ay
        length2 = vx * vx + vy * vy or 1e-9
        reach = half + 1
        for py in range(max(0, int(min(ay, by) - reach)), min(h, int(max(ay, by) + reach) + 1)):
            qy = py + 0.5 - ay
            row = py * w
            for px in range(max(0, int(min(ax, bx) - reach)), min(w, int(max(ax, bx) + reach) + 1)):
                qx = px + 0.5 - ax
                t = (qx * vx + qy * vy) / length2
                t = 0.0 if t < 0 else 1.0 if t > 1 else t
                dx, dy = qx - t * vx, qy - t * vy
                cov = half + 0.5 - math.sqrt(dx * dx + dy * dy)
                if cov > 0:
                    self.union(row + px, a * fade * (cov if cov < 1 else 1.0))

    def polyline(self, pts, width, a=1.0, alphas=None, widths=None):
        for k in range(len(pts) - 1):
            self.segment(pts[k][0], pts[k][1], pts[k + 1][0], pts[k + 1][1],
                         widths[k] if widths else width, alphas[k] if alphas else a)


class Canvas:
    def __init__(self, w, h, background):
        self.w, self.h = w, h
        self.channels = [array("f", [float(c)]) * (w * h) for c in hex_rgb(background)]

    def mask(self):
        return Mask(self.w, self.h)

    def paint(self, mask, color, alpha=1.0):
        r, g, b = self.channels
        cr, cg, cb = hex_rgb(color)
        for i, a in mask.items():
            a *= alpha
            r[i] += (cr - r[i]) * a
            g[i] += (cg - g[i]) * a
            b[i] += (cb - b[i]) * a

    def png(self):
        w, h = self.w, self.h
        rgb = bytearray(w * h * 3)
        for c, channel in enumerate(self.channels):
            rgb[c::3] = bytes(int(v + 0.5) for v in channel)
        stride = w * 3
        raw = b"".join(b"\x00" + rgb[y * stride:(y + 1) * stride] for y in range(h))

        def chunk(tag, data):
            return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & M32)

        return (b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 9))
                + chunk(b"IEND", b""))


def spline(pts, steps=24):
    """Catmull-Rom spline through the points, sampled into a polyline."""
    out = []
    for i in range(len(pts) - 1):
        p0, p1, p2 = pts[max(i - 1, 0)], pts[i], pts[i + 1]
        p3 = pts[i + 2] if i + 2 < len(pts) else p2
        for s in range(steps):
            t = s / steps
            t2, t3 = t * t, t * t * t
            out.append(tuple(
                0.5 * (2 * p1[k] + (-p0[k] + p2[k]) * t
                       + (2 * p0[k] - 5 * p1[k] + 4 * p2[k] - p3[k]) * t2
                       + (-p0[k] + 3 * p1[k] - 3 * p2[k] + p3[k]) * t3)
                for k in (0, 1)))
    out.append(tuple(pts[-1]))
    return out


# ------------------------------------------------------------------- drawing

def spray_hit(canvas, rnd, x, y, r, color, drips=4, angle=-0.4):
    """sprayHit from hero-art.js, scaled from its ~45px radius: soft mist, a
    dense uneven core, flecks thrown along the spray direction, and drips that
    run, thin out and end in a bead."""
    k = r / 45
    m = canvas.mask()
    # The mist keeps the hero's fine grain at this size, so it needs more dots;
    # the core blobs get soft edges so they read as spray, not circles.
    for _ in range(int(3200 * k * k)):
        a, d = rnd() * TAU, abs(gauss(rnd)) * r * 3.4
        s = (0.5 + rnd()) * 0.9
        m.square(x + math.cos(a) * d, y + math.sin(a) * d * 0.85, s, 0.04 + 0.3 * max(0, 1 - d / (r * 3.4)))
    for _ in range(320):
        a, d = rnd() * TAU, abs(gauss(rnd)) * r * 0.95
        rad = r * (0.06 + rnd() * 0.16)
        m.disc(x + math.cos(a) * d, y + math.sin(a) * d * 0.8, rad, 0.22, soft=max(1.0, rad * 0.8))
    for _ in range(46):
        a = angle + gauss(rnd) * 1.4
        d = r * (1.1 + rnd() * rnd() * 3)
        s = 0.5 * k + rnd() * r * 0.035
        m.ellipse(x + math.cos(a) * d, y + math.sin(a) * d, s * (1 + rnd() * 1.6), s, a, 0.85)
    for _ in range(drips):
        dx = x + gauss(rnd) * r * 0.7
        dy = y + r * (0.3 + rnd() * 0.35)
        length = r * (0.5 + rnd() * rnd() * 4.5)
        w = 0.9 * k + rnd() * r * 0.07
        steps = max(1, math.ceil(length / 3))
        drip = canvas.mask()
        for _ in range(steps):
            nx, ny = dx + (rnd() - 0.5) * 0.5, dy + length / steps
            drip.segment(dx, dy, nx, ny, w, 1.0)
            dx, dy = nx, ny
            w = max(0.7 * k, w * 0.985)
        drip.disc(dx, dy, w * 1.25, 1.0, union=True)
        for i, a in drip.items():
            m.add(i, a * 0.9)
    canvas.paint(m, color)


def streams_for(mode, rnd, w, h, hit):
    """Control points for each data stream. Every stream runs left to right
    and stays calm under the text side of the share image; the shape happens
    on the right, around the paint."""
    n = 7 + int(rnd() * 5)
    hx, hy, hr = hit
    out = []
    if mode == "funnel":
        # Spread out, squeezed through one waist, spread out again.
        wx, wy = w * (0.6 + rnd() * 0.16), h * (0.4 + rnd() * 0.22)
        waist = h * (0.02 + rnd() * 0.04)
        cross = rnd() < 0.5
        for i in range(n):
            f = i / (n - 1) - 0.5
            y0 = h * (0.5 + f * 0.82) + gauss(rnd) * h * 0.04
            y1 = h * (0.5 + (-f if cross else f) * 0.9) + gauss(rnd) * h * 0.05
            out.append([(-10, y0), (w * 0.3, lerp(y0, wy, 0.25) + gauss(rnd) * h * 0.03),
                        (wx, wy + f * waist * 2), (w * 0.9, lerp(wy, y1, 0.7)), (w + 10, y1)])
    elif mode == "gather":
        # Streams run into the paint and leave it as a tighter bundle.
        spread = h * (0.12 + rnd() * 0.2)
        for i in range(n):
            f = i / (n - 1) - 0.5
            y0 = h * (0.5 + f * 0.85) + gauss(rnd) * h * 0.04
            out.append([(-10, y0), (w * 0.32, lerp(y0, hy, 0.3) + gauss(rnd) * h * 0.03),
                        (hx - hr * 1.6, hy + f * hr * 0.9), (hx + hr * 0.9, hy + f * hr * 0.5),
                        (w + 10, clamp(hy + f * spread * 2, h * 0.05, h * 0.95))])
    elif mode == "fan":
        # One tight bundle that opens out across the right side.
        sy = h * (0.35 + rnd() * 0.3)
        opens = w * (0.42 + rnd() * 0.16)
        for i in range(n):
            f = i / (n - 1) - 0.5
            y1 = h * (0.5 + f * 0.96) + gauss(rnd) * h * 0.03
            out.append([(-10, sy + f * h * 0.03), (opens, sy + f * h * 0.06 + gauss(rnd) * h * 0.01),
                        (lerp(opens, w, 0.55), lerp(sy, y1, 0.62)), (w + 10, y1)])
    else:  # "braid"
        # Waves that start nearly flat and cross more and more to the right.
        cycles = 1.2 + rnd() * 1.4
        amp = h * (0.08 + rnd() * 0.08)
        for i in range(n):
            f = i / (n - 1) - 0.5
            base = h * (0.5 + f * 0.6)
            phase = rnd() * TAU
            out.append([(x, base + math.sin(phase + x / w * cycles * TAU) * amp * (0.15 + 0.85 * max(0, x / w) ** 1.5))
                        for x in (w * j / 10 - 10 for j in range(12))])
    return out


def compose(seed, color, accent, w=W, h=H):
    rnd = make_rng(seed)
    canvas = Canvas(w, h, BACKGROUND)

    # The main hit sits on the right, clear of the share image's text column.
    r = h * (0.1 + rnd() * 0.04)
    hx, hy = w * (0.68 + rnd() * 0.18), h * (0.22 + rnd() * 0.22)
    mode = ("funnel", "gather", "fan", "braid")[int(rnd() * 4)]
    every = 3 + int(rnd() * 2)

    streams = []
    for i, pts in enumerate(streams_for(mode, rnd, w, h, (hx, hy, r))):
        line = spline(pts)
        hot = i % every == 1
        m = canvas.mask()
        m.polyline(line, 2.2 if hot else 1.5)
        canvas.paint(m, accent if hot else INK, 0.6 if hot else 0.17)
        streams.append(line)

    spray_hit(canvas, rnd, hx, hy, r, color, drips=3 + int(rnd() * 4), angle=-0.2 - rnd() * 1.9)

    # A small echo in the accent colour, low on the right and away from the hit.
    for _ in range(20):
        ex, ey = w * (0.5 + rnd() * 0.45), h * (0.66 + rnd() * 0.2)
        if math.hypot(ex - hx, ey - hy) > r * 3:
            break
    spray_hit(canvas, rnd, ex, ey, r * (0.22 + rnd() * 0.14), accent, drips=1 + int(rnd() * 2))

    # Packets moving along the streams, each with a fading trail behind it.
    for _ in range(18 + int(rnd() * 16)):
        line = streams[int(rnd() * len(streams))]
        j = 8 + int(rnd() * (len(line) - 9))
        red = rnd() < 0.15
        tail = line[max(0, j - 7):j + 1]
        trail = canvas.mask()
        trail.polyline(tail, 1.6, alphas=[0.5 * (k + 1) / len(tail) for k in range(len(tail))])
        canvas.paint(trail, color if red else accent, 0.8)
        dot = canvas.mask()
        dot.disc(line[j][0], line[j][1], 3.4 if red else 2.6, 1.0)
        canvas.paint(dot, color if red else accent, 0.9)

    return canvas


# --------------------------------------------------------------- front matter

def parse_yaml(src):
    """Just enough YAML for front matter: nested maps, block and inline
    lists, quoted and plain scalars, block scalars (| and >)."""
    lines = src.splitlines()
    pos = 0

    def scalar(s):
        s = s.strip()
        if s[:1] in "\"'":
            q = s[0]
            end = s.find(q, 1)
            while q == "'" and end != -1 and s[end + 1:end + 2] == "'":
                end = s.find(q, end + 2)
            body = s[1:end if end != -1 else None]
            return json.loads(f'"{body}"') if q == '"' else body.replace("''", "'")
        s = re.sub(r"\s+#.*$", "", s)
        if s.startswith("[") and s.endswith("]"):
            items = re.findall(r'"[^"]*"|\'[^\']*\'|[^,]+', s[1:-1])
            return [scalar(x) for x in items if x.strip()]
        return {"true": True, "false": False, "null": None, "~": None}.get(s.lower(), s)

    def indent_of(line):
        return len(line) - len(line.lstrip(" "))

    def next_content():
        i = pos
        while i < len(lines) and (not lines[i].strip() or lines[i].lstrip().startswith("#")):
            i += 1
        return i

    def block(indent):
        nonlocal pos
        result = None
        while True:
            pos = next_content()
            if pos >= len(lines):
                break
            line = lines[pos]
            ind, text = indent_of(line), line.strip()
            if ind < indent:
                break
            if text == "-" or text.startswith("- "):
                if result is None:
                    result = []
                if not isinstance(result, list):
                    break
                item = text[1:].strip()
                if not item:
                    pos += 1
                    result.append(block(ind + 1))
                elif re.match(r"^[^\s\"'\[{][^:]*:(\s|$)", item):
                    lines[pos] = " " * (ind + 2) + item
                    result.append(block(ind + 2))
                else:
                    pos += 1
                    result.append(scalar(item))
                continue
            match = re.match(r"^(\"[^\"]+\"|'[^']+'|[^:]+?)\s*:(?:\s+(.*))?$", text)
            if result is None:
                result = {}
            if not match or not isinstance(result, dict):
                pos += 1
                continue
            key, rest = match.group(1).strip("\"'"), (match.group(2) or "").strip()
            pos += 1
            if rest[:1] in ("|", ">"):
                body = []
                while pos < len(lines) and (not lines[pos].strip() or indent_of(lines[pos]) > ind):
                    body.append(lines[pos].strip())
                    pos += 1
                result[key] = ("\n" if rest[0] == "|" else " ").join(body).strip()
            elif rest and not rest.startswith("#"):
                result[key] = scalar(rest)
            else:
                nxt = next_content()
                if nxt < len(lines) and indent_of(lines[nxt]) > ind:
                    result[key] = block(indent_of(lines[nxt]))
                elif nxt < len(lines) and indent_of(lines[nxt]) == ind and lines[nxt].strip().startswith("-"):
                    result[key] = block(ind)
                else:
                    result[key] = None
        return result

    return block(0) or {}


def front_matter(path):
    text = path.read_text(encoding="utf-8-sig")
    if text.startswith("---"):
        end = text.find("\n---", 3)
        return parse_yaml(text[3:end])
    if text.startswith("+++"):
        import tomllib  # Python 3.11+
        end = text.find("\n+++", 3)
        return tomllib.loads(text[3:end])
    if text.lstrip().startswith("{"):
        return json.JSONDecoder().raw_decode(text.lstrip())[0]
    return {}


# ----------------------------------------------------------------- the posts

def topic_for(tags):
    if isinstance(tags, str):
        tags = [tags]
    tags = {str(t).lower() for t in tags or []}
    return next((t for t in TOPICS if t["tags"] & tags), DEFAULT)


def check_color(value, where):
    if not re.fullmatch(r"#(?:[0-9a-fA-F]{3}){1,2}", str(value)):
        sys.exit(f"{where}: art colour {value!r} is not a hex colour like \"#4fd6ec\"")
    return "#" + "".join(f"{c:02x}" for c in hex_rgb(str(value)))


def spec_for(key, matter, where):
    """Everything the picture depends on. A post is redrawn when this changes."""
    art = matter.get("art") if isinstance(matter.get("art"), dict) else {}
    topic = topic_for(matter.get("tags"))
    color = check_color(art.get("color") or topic["color"], where)
    accent = art.get("accent")
    if accent:
        accent = check_color(accent, where)
    else:
        # Default accent, unless the paint colour was overridden to something
        # close to it; then the site colour furthest from the paint.
        accent = topic["accent"]
        if sum((a - b) ** 2 for a, b in zip(hex_rgb(color), hex_rgb(accent))) < 90 ** 2:
            accent = max((RED, CYAN, GOLD), key=lambda c: sum((a - b) ** 2 for a, b in zip(hex_rgb(color), hex_rgb(c))))
    return {"seed": str(art.get("seed") or key), "color": color, "accent": accent,
            "topic": topic["name"], "label": topic["label"], "version": VERSION}


def posts():
    for path in sorted(CONTENT.rglob("*.md")):
        if path.name.startswith("_index"):
            continue
        matter = front_matter(path)
        name = path.parent.name if path.stem == "index" else path.stem
        yield path, str(matter.get("slug") or name), matter


def render(spec, out):
    started = time.time()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(compose(spec["seed"], spec["color"], spec["accent"]).png())
    return time.time() - started


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--force", action="store_true", help="redraw every post")
    parser.add_argument("--preview", metavar="TITLE", help="draw art for a title without a post")
    parser.add_argument("--tags", default="", help="comma-separated tags for --preview")
    parser.add_argument("-o", "--out", type=Path, help="output PNG for --preview")
    args = parser.parse_args()

    if args.preview:
        key = re.sub(r"[^a-z0-9]+", "-", args.preview.lower()).strip("-")
        spec = spec_for(key, {"tags": [t for t in args.tags.split(",") if t]}, "--preview")
        out = args.out or Path(f"{key}.png")
        print(f"{out}  ({spec['topic']}, {render(spec, out):.1f}s)")
        return

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else {}
    entries = manifest.get("posts", {})
    seen, changed = set(), False
    for path, key, matter in posts():
        where = path.relative_to(ROOT).as_posix()
        cover = matter.get("cover")
        if isinstance(cover, dict) and cover.get("image"):
            continue
        if key in seen:
            sys.exit(f"{where}: another post already uses the slug {key!r}")
        seen.add(key)
        spec = spec_for(key, matter, where)
        png = OUT / f"{key}.png"
        if not args.force and entries.get(key) == spec and png.exists():
            continue
        print(f"drawing {png.relative_to(ROOT).as_posix()}  ({spec['topic']}) ...", end=" ", flush=True)
        print(f"{render(spec, png):.1f}s")
        entries[key] = spec
        changed = True

    for key in sorted(set(entries) - seen):
        (OUT / f"{key}.png").unlink(missing_ok=True)
        del entries[key]
        print(f"removed art for {key} (no post needs it any more)")
        changed = True

    if changed or not MANIFEST.exists():
        OUT.mkdir(parents=True, exist_ok=True)
        with open(MANIFEST, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"posts": dict(sorted(entries.items()))}, indent=2, ensure_ascii=False) + "\n")
    else:
        print("post art is up to date")


if __name__ == "__main__":
    main()
