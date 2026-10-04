"""desk.orb_engine — the thinking-orbs geometry, in Python.

A faithful port of thinking-orbs' engine (MIT, (c) 2026 Jakub Antalik,
https://github.com/Jakubantalik/thinking-orbs; licence in
desk/ui/src/vendor/thinking-orbs/LICENSE). The window uses the original
TypeScript; this exists for the ambient orb, which has to be drawn natively
so the desktop shows through between its dots (WebView2 cannot be made
transparent on this machine -- measured: an opaque (240,240,240) square).

Verified number for number against the library's own golden vectors
(spec/orbs-golden.json: 9 states x 2 sizes x 4 instants) in
tests/test_orb_engine.py. Keep it that way: change nothing here without the
golden test passing.

Two JavaScript details matter for exactness: Math.round rounds halves up
(Python's round() rounds them to even), and Array.prototype.sort is stable,
like sorted().
"""
from __future__ import annotations

import math
from typing import Callable, Dict, List, Optional, Tuple

PI = math.pi


def js_round(x: float) -> int:
    return int(math.floor(x + 0.5))


# ── core ─────────────────────────────────────────────────────────────────────

def lerp(a, b, f):
    return a + (b - a) * f


def frac(x):
    return x - math.floor(x)


def hash_d(a, b):
    h = math.sin(a * 12.9898 + b * 78.233) * 43758.5453
    return h - math.floor(h)


def vnoise(x, y):
    xi, yi = math.floor(x), math.floor(y)
    fx, fy = x - xi, y - yi
    fx = fx * fx * (3 - 2 * fx)
    fy = fy * fy * (3 - 2 * fy)
    a, b = hash_d(xi, yi), hash_d(xi + 1, yi)
    c, d = hash_d(xi, yi + 1), hash_d(xi + 1, yi + 1)
    return a + (b - a) * fx + (c - a) * fy + (a - b - c + d) * fx * fy


def fib_dir(i, n):
    golden = PI * (3 - math.sqrt(5))
    y = 1 - (2 * (i + 0.5)) / n
    rad = math.sqrt(1 - y * y)
    a = i * golden
    return rad * math.cos(a), y, rad * math.sin(a)


def angle_delta(a, b):
    return math.atan2(math.sin(a - b), math.cos(a - b))


def make_proj(yaw, tilt, cx, cy, scale) -> Callable:
    st, ct, sy, cyw = math.sin(tilt), math.cos(tilt), math.sin(yaw), math.cos(yaw)

    def proj(x, y, z):
        x1 = x * cyw + z * sy
        z1 = -x * sy + z * cyw
        y1 = y * ct - z1 * st
        z2 = y * st + z1 * ct
        return cx + x1 * scale, cy - y1 * scale, z2
    return proj


def radius_scale(size, pw):
    return (size / 300) ** pw


Dot = Dict[str, float]


def _dot(x, y, z, r, white, a=1.0) -> Dot:
    return {"x": x, "y": y, "z": z, "r": r, "white": white, "a": a}


def finalize(dots: List[Dot], lines: List[Dot], r_min=0.3) -> Tuple[List[Dot], List[Dot]]:
    r_min = 0.3 if r_min is None else r_min
    visible = []
    for d in dots:
        if d["a"] < 0.02:
            continue
        d["r"] = max(r_min, d["r"])
        visible.append(d)
    visible.sort(key=lambda d: d["z"])
    return visible, [l for l in lines if l["a"] >= 0.02]


# ── profiles + presets ───────────────────────────────────────────────────────

_COUNT_PAIRS = [("latRings", "lonDensity"), ("rings", "lonDensity"), ("lanes", "segs")]
_COUNT_KEYS = ["orbitN", "ghostN", "nodeN", "strandN", "signals"]
_RADIUS_KEYS = ["rBase", "rDepth", "rActive", "rDot", "ghostR", "partR", "partRDepth", "nodeR", "nodeRDepth"]

BASE_PROFILES = {
    "globe": dict(latRings=17, lonDensity=44, rBase=0.6, rDepth=1.7, rBoost=1.0, inkFar=0.62,
                  inkSpan=0.54, rsPow=0.6, rMin=0.3),
    "orbits": dict(orbitN=12, ghostN=40, ghostR=0.9, ghostA=0.5, particles=3, partR=1.2,
                   partRDepth=1.6, rsPow=0.6, rMin=0.3),
    "rubik": dict(latRings=15, lonDensity=40, moveCount=14, rBase=0.6, rDepth=1.7, rActive=0.3,
                  inkFar=0.62, inkSpan=0.54, rsPow=0.6, rMin=0.3),
    "wave": dict(rings=15, lonDensity=40, rBase=0.6, rDepth=1.7, rsPow=0.6, rMin=0.3),
    "web": dict(nodeN=30, thr=0.72, signals=5, nodeR=1.4, nodeRDepth=1.8, lineW=0.8, rsPow=0.6, rMin=0.3),
    "braid": dict(strandN=52, turns=3.0, ghostN=150, rBase=1.2, rDepth=1.8, rsPow=0.6, rMin=0.3),
    "ribbon": dict(lanes=5, segs=88, ghostN=150, rBase=1.1, rDepth=1.7, rsPow=0.6, rMin=0.3),
    "ring": dict(lanes=5, segs=88, ghostN=0, faceOn=1, rBase=1.1, rDepth=1.7, rsPow=0.6, rMin=0.3),
    "morph": dict(rDot=0.021, iconD=1, rMin=0.25),
}


def scale_counts(opts: dict, scale: float) -> dict:
    out, done, rt = dict(opts), set(), math.sqrt(scale)
    for a, b in _COUNT_PAIRS:
        if out.get(a) is not None and out.get(b) is not None and a not in done and b not in done:
            out[a] = max(2, js_round(out[a] * rt))
            out[b] = max(2, js_round(out[b] * rt))
            done.update((a, b))
    for k in _COUNT_KEYS:
        v = out.get(k)
        if v is not None and v != 0 and k not in done:
            out[k] = max(1, js_round(v * scale))
    if out.get("iconD") is not None:
        out["iconD"] = max(0.02, out["iconD"] * scale)
    return out


def scale_radii(opts: dict, scale: float) -> dict:
    out = dict(opts)
    for k in _RADIUS_KEYS:
        if out.get(k) is not None:
            out[k] = out[k] * scale
    out["rSizeMul"] = out.get("rSizeMul", 1) * scale
    return out


STATE_TO_MODE = {"working": "orbits", "searching": "globe", "solving": "rubik", "listening": "wave",
                 "connecting": "web", "weaving": "braid", "composing": "ribbon", "breathing": "ring",
                 "shaping": "morph"}

PRESETS = {
    "orbits": {64: dict(speed=1.885, count=1, size=1), 20: dict(speed=3.9, count=0.238, size=2.4)},
    "globe": {64: dict(speed=2.015, count=0.42, size=1.15, extra=dict(scanMul=4.08, dimBase=0.45)),
              20: dict(speed=2.665, count=0.105, size=1.75, extra=dict(scanMul=4.335, dimBase=0.45))},
    "rubik": {64: dict(speed=1.82, count=0.35, size=1.05), 20: dict(speed=1.95, count=0.088, size=1.9)},
    "wave": {64: dict(speed=4.388, count=0.341, size=1), 20: dict(speed=3.998, count=0.105, size=1.6)},
    "web": {64: dict(speed=3.315, count=1.35, size=0.95), 20: dict(speed=6.63, count=0.25, size=1.52)},
    "braid": {64: dict(speed=1.625, count=0.5, size=1), 20: dict(speed=2.75, count=0.1125, size=1.36)},
    "ribbon": {64: dict(speed=2.34, count=0.25, size=0.85, extra=dict(spin=0, bandMul=3.9, wobMul=1)),
               20: dict(speed=3.12, count=0.051, size=1.073, extra=dict(spin=0, bandMul=4.94, wobMul=1))},
    "ring": {64: dict(speed=3.24, count=0.25, size=0.956, extra=dict(spin=0, bandMul=3.627, wobMul=0.368)),
             20: dict(speed=3.78, count=0.028, size=1.622, extra=dict(spin=0, bandMul=3.968, wobMul=0.565))},
    "morph": {64: dict(speed=2.405, count=0.702, size=0.395, extra=dict(spread=1.45)),
              20: dict(speed=2.08, count=0.53, size=1.011, extra=dict(spread=1.45))},
}

_cache: dict = {}


def resolve_preset(state: str, size: int) -> Tuple[str, float, dict]:
    key = (state, size)
    if key in _cache:
        return _cache[key]
    mode = STATE_TO_MODE[state]
    p = PRESETS[mode][size]
    opts = dict(BASE_PROFILES[mode])
    if p["count"] != 1:
        opts = scale_counts(opts, p["count"])
    if p["size"] != 1:
        opts = scale_radii(opts, p["size"])
    opts.update(p.get("extra") or {})
    _cache[key] = (mode, p["speed"], opts)
    return _cache[key]


# ── modes ────────────────────────────────────────────────────────────────────

def frame_orbits(size, t, o):
    cx = cy = size / 2
    R = (size / 2) * 0.82
    pt = make_proj(t * 0.12, 0.3, cx, cy, 1)
    rs = radius_scale(size, o.get("rsPow", 0.6))
    dots = []
    ghost_n, particles = o.get("ghostN", 40), o.get("particles", 3)
    for orb in range(o.get("orbitN", 12)):
        h1, h2, h3 = hash_d(orb, 1.7), hash_d(orb, 5.2), hash_d(orb, 8.9)
        ro = R * (0.45 + 0.52 * h1)
        th = h1 * 2 * PI
        phi = math.acos(2 * h2 - 1)
        nx, ny, nz = math.sin(phi) * math.cos(th), math.cos(phi), math.sin(phi) * math.sin(th)
        ux, uy, uz = -ny, nx, 0
        ul = max(1e-6, math.sqrt(ux * ux + uy * uy))
        ux, uy = ux / ul, uy / ul
        vx, vy, vz = ny * uz - nz * uy, nz * ux - nx * uz, nx * uy - ny * ux
        speed = (0.25 + 0.55 * h3) * (1 if h3 > 0.5 else -1)
        for k in range(ghost_n):
            a = (k / ghost_n) * 2 * PI
            px, py, z = pt((ux * math.cos(a) + vx * math.sin(a)) * ro, (uy * math.cos(a) + vy * math.sin(a)) * ro,
                           (uz * math.cos(a) + vz * math.sin(a)) * ro)
            depth = (z / ro + 1) / 2
            dots.append(_dot(px, py, z, o.get("ghostR", 0.9) * rs, 0.72, o.get("ghostA", 0.5) * (0.4 + 0.6 * depth)))
        for m in range(particles):
            a = t * speed + (m / particles) * 2 * PI + h2 * 6
            px, py, z = pt((ux * math.cos(a) + vx * math.sin(a)) * ro, (uy * math.cos(a) + vy * math.sin(a)) * ro,
                           (uz * math.cos(a) + vz * math.sin(a)) * ro)
            depth = (z / ro + 1) / 2
            dots.append(_dot(px, py, z, (o.get("partR", 1.2) + o.get("partRDepth", 1.6) * depth) * rs,
                             0.3 - 0.22 * depth))
    return finalize(dots, [], o.get("rMin"))


def _lattice(lat_rings, lon_density):
    for li in range(lat_rings + 1):
        lat = -PI / 2 + (li / lat_rings) * PI
        cos_lat, sin_lat = math.cos(lat), math.sin(lat)
        lon_count = max(1, js_round(abs(cos_lat) * lon_density))
        for lj in range(lon_count):
            yield li, cos_lat, sin_lat, (lj / lon_count) * 2 * PI


def frame_globe(size, t, o):
    spin = 0.5
    cx = cy = size / 2
    radius = (size / 2) * 0.82
    tilt = 0.4 + 0.06 * math.sin(t * 0.35)
    pt = make_proj(t * spin, tilt, cx, cy, radius)
    scan = t * (spin + (1.7 - spin) * o.get("scanMul", 1))
    rs = radius_scale(size, o.get("rsPow", 0.6))
    dim = o.get("dimBase", 1)
    dots = []
    for _, cl, sl, lon in _lattice(o.get("latRings", 17), o.get("lonDensity", 44)):
        px, py, z = pt(cl * math.cos(lon), sl, cl * math.sin(lon))
        depth = (z + 1) / 2
        d = angle_delta(lon + t * spin, scan)
        boost = math.exp(-(d * d) / 0.18) * max(0, z)
        dots.append(_dot(px, py, z, (o.get("rBase", 0.6) + o.get("rDepth", 1.7) * depth + o.get("rBoost", 1) * boost) * rs,
                         o.get("inkFar", 0.62) - o.get("inkSpan", 0.54) * depth, dim + (1 - dim) * min(1, boost)))
    return finalize(dots, [], o.get("rMin"))


def _solve_cycle(time, count, slot_dur, rest):
    cyc = 2 * count * slot_dur + rest
    tc = math.fmod(time, cyc)
    amount, active = [0.0] * count, -1
    if tc < 2 * count * slot_dur:
        slot = math.floor(tc / slot_dur)
        p = (tc - slot * slot_dur) / slot_dur
        cl = min(1, p / 0.7)
        ep = 1 - (1 - cl) ** 3
        if slot < count:
            for i in range(slot):
                amount[i] = 1
            amount[slot], active = ep, slot
        else:
            u = 2 * count - 1 - slot
            for i in range(u):
                amount[i] = 1
            amount[u], active = 1 - ep, u
    return amount, active


def _make_moves(count):
    moves = []
    for i in range(count):
        axis = min(2, math.floor(hash_d(i, 2.3) * 3))
        lo = -1.0 + 0.5 * min(3, math.floor(hash_d(i, 5.9) * 4))
        d = 1 if hash_d(i, 7.7) < 0.5 else -1
        moves.append((axis, lo, lo + 0.5, (d * PI) / 2))
    return moves


def _apply_moves(x, y, z, moves, amount, active):
    in_active = False
    for i, (axis, lo, hi, ang) in enumerate(moves):
        if amount[i] <= 0:
            continue
        coord = x if axis == 0 else y if axis == 1 else z
        if coord < lo or coord >= hi:
            continue
        if i == active:
            in_active = True
        a = ang * amount[i]
        ca, sa = math.cos(a), math.sin(a)
        if axis == 0:
            y, z = y * ca - z * sa, y * sa + z * ca
        elif axis == 1:
            x, z = x * ca + z * sa, -x * sa + z * ca
        else:
            x, y = x * ca - y * sa, x * sa + y * ca
    return x, y, z, in_active


def frame_rubik(size, t, o):
    cx = cy = size / 2
    R = (size / 2) * 0.82
    pt = make_proj(t * 0.55, 0.35 + 0.1 * math.sin(t * 0.9), cx, cy, R)
    rs = radius_scale(size, o.get("rsPow", 0.6))
    mc = o.get("moveCount", 14)
    moves = _make_moves(mc)
    amount, active = _solve_cycle(t, mc, 0.42, 1.2)
    dots = []
    for _, cl, sl, lon in _lattice(o.get("latRings", 15), o.get("lonDensity", 40)):
        x, y, z, act = _apply_moves(cl * math.cos(lon), sl, cl * math.sin(lon), moves, amount, active)
        px, py, zr = pt(x, y, z)
        depth = (zr + 1) / 2
        dots.append(_dot(px, py, zr,
                         (o.get("rBase", 0.6) + o.get("rDepth", 1.7) * depth + (o.get("rActive", 0.3) if act else 0)) * rs,
                         o.get("inkFar", 0.62) - o.get("inkSpan", 0.54) * depth - (0.14 if act else 0)))
    return finalize(dots, [], o.get("rMin"))


def frame_wave(size, t, o):
    cx = cy = size / 2
    R = (size / 2) * 0.874
    pt = make_proj(t * 0.18, 0.38, cx, cy, 1)
    rs = radius_scale(size, o.get("rsPow", 0.6))
    dots = []
    rings = o.get("rings", 15)
    for ri, cl, sl, lon in _lattice(rings, o.get("lonDensity", 40)):
        w = 0.62 * math.sin(t * 2.1 - ri * 0.52) + 0.38 * math.sin(t * 1.27 + ri * 0.83)
        rr = R * (0.88 + 0.105 * w)
        px, py, z = pt(cl * math.cos(lon) * rr, sl * rr, cl * math.sin(lon) * rr)
        depth = (z / R + 1) / 2
        crest = max(0, w)
        dots.append(_dot(px, py, z, (o.get("rBase", 0.6) + o.get("rDepth", 1.7) * depth) * (1 + 0.4 * crest) * rs,
                         0.66 - 0.56 * depth - 0.1 * crest))
    return finalize(dots, [], o.get("rMin"))


def _ghost_sphere(dots, n, R, pt, rs):
    for i in range(n):
        d = fib_dir(i, n)
        px, py, z = pt(d[0] * R, d[1] * R, d[2] * R)
        depth = (z / R + 1) / 2
        dots.append(_dot(px, py, z, 0.8 * rs, 0.78, 0.1 + 0.22 * depth))


def frame_braid(size, t, o):
    cx = cy = size / 2
    R = (size / 2) * 0.76
    pt = make_proj(t * 0.4, 0.3, cx, cy, 1)
    rs = radius_scale(size, o.get("rsPow", 0.6))
    dots = []
    _ghost_sphere(dots, o.get("ghostN", 150), R, pt, rs)
    strand_n, turns = o.get("strandN", 52), o.get("turns", 3)
    for s in range(3):
        phase = (s / 3) * 2 * PI
        for i in range(strand_n):
            u = (frac(i / strand_n + t * 0.045) * 2 - 1) * 0.96
            surf = math.sqrt(max(0, 1 - u * u))
            end_fade = min(1, (1 - abs(u)) / 0.1)
            a = u * PI * turns + phase
            weave = 1 + 0.075 * math.sin(u * PI * turns * 2 + phase * 2 + t * 0.8)
            rr = surf * R * weave
            px, py, zr = pt(math.cos(a) * rr, u * R * weave, math.sin(a) * rr)
            depth = (zr / R + 1) / 2
            dots.append(_dot(px, py, zr, (o.get("rBase", 1.2) + o.get("rDepth", 1.8) * depth) * rs,
                             0.55 - 0.45 * depth, end_fade * (0.45 + 0.55 * depth)))
    return finalize(dots, [], o.get("rMin"))


def frame_ribbon(size, t, o):
    cx = cy = size / 2
    R = (size / 2) * 0.78
    spin = o.get("spin", 1)
    cam_tilt = 0.3
    pt = make_proj(t * 0.1 * spin, cam_tilt, cx, cy, 1)
    rs = radius_scale(size, o.get("rsPow", 0.6))
    dots = []
    _ghost_sphere(dots, o.get("ghostN", 150), R, pt, rs)
    face_on = bool(o.get("faceOn"))
    ya = t * 0.24 * spin
    ta = -cam_tilt if face_on else 0.55 + 0.3 * math.sin(t * 0.18) * spin
    ux, uy, uz = math.cos(ya), 0, math.sin(ya)
    vx, vy, vz = -uz * math.sin(ta), math.cos(ta), ux * math.sin(ta)
    nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
    wob_mul = o.get("wobMul", 1)
    base_r = R / (1 + 0.85 * 0.23 * wob_mul) if face_on else R
    segs = o.get("segs", 88)
    lanes = max(1, js_round(o.get("lanes", 5) * o.get("bandMul", 1)))
    for w in range(lanes):
        lane_off = (w - (lanes - 1) / 2) * 0.075
        edge = abs(w - (lanes - 1) / 2) / max(1, (lanes - 1) / 2)
        for k in range(segs):
            a = (k / segs) * 2 * PI
            wob = (0.16 * math.sin(a * 3 - t * 1.7 + w * 0.22) + 0.07 * math.sin(a * 5 + t * 1.1)) * wob_mul
            radial = 1 + wob if face_on else 1
            off = lane_off if face_on else lane_off + wob
            x = ux * math.cos(a) + vx * math.sin(a) + nx * off
            y = uy * math.cos(a) + vy * math.sin(a) + ny * off
            z = uz * math.cos(a) + vz * math.sin(a) + nz * off
            ln = math.sqrt(x * x + y * y + z * z)
            rr = base_r * radial
            px, py, zr = pt((x / ln) * rr, (y / ln) * rr, (z / ln) * rr)
            depth = (zr / R + 1) / 2
            dots.append(_dot(px, py, zr, (o.get("rBase", 1.1) + o.get("rDepth", 1.7) * depth) * (1 - 0.25 * edge) * rs,
                             0.52 - 0.44 * depth + 0.18 * edge, 0.4 + 0.6 * depth))
    return finalize(dots, [], o.get("rMin"))


def frame_web(size, t, o):
    cx = cy = size / 2
    R = (size / 2) * 0.8 * o.get("spread", 1)
    pt = make_proj(t * 0.12, 0.32, cx, cy, R)
    rs = radius_scale(size, o.get("rsPow", 0.6))
    node_n, thr = o.get("nodeN", 30), o.get("thr", 0.72)
    node_r, node_rd = o.get("nodeR", 1.4), o.get("nodeRDepth", 1.8)
    nodes = []
    for i in range(node_n):
        d = fib_dir(i, node_n)
        x = d[0] + 0.3 * (vnoise(i * 0.31 + 9, t * 0.24) - 0.5) * 2
        y = d[1] + 0.3 * (vnoise(i * 0.53 + 27, t * 0.21) - 0.5) * 2
        z = d[2] + 0.3 * (vnoise(i * 0.77 + 55, t * 0.27) - 0.5) * 2
        ln = math.sqrt(x * x + y * y + z * z)
        nodes.append((x / ln, y / ln, z / ln))
    lines, dots = [], []
    for i in range(node_n):
        for j in range(i + 1, node_n):
            dx, dy, dz = (nodes[i][k] - nodes[j][k] for k in range(3))
            dist = math.sqrt(dx * dx + dy * dy + dz * dz)
            if dist >= thr:
                continue
            x1, y1, z1 = pt(*nodes[i])
            x2, y2, z2 = pt(*nodes[j])
            depth = ((z1 + z2) / 2 + 1) / 2
            lines.append({"x1": x1, "y1": y1, "x2": x2, "y2": y2, "white": 0.42,
                          "a": (1 - dist / thr) * (0.3 + 0.55 * depth), "w": max(0.6, o.get("lineW", 0.8) * rs)})
    for i in range(node_n):
        px, py, z = pt(*nodes[i])
        depth = (z + 1) / 2
        pulse = 1 + 0.25 * math.sin(t * 1.4 + i * 2.7)
        dots.append(_dot(px, py, z, (node_r + node_rd * depth) * pulse * rs, 0.55 - 0.45 * depth))
    for s in range(o.get("signals", 5)):
        seg = math.floor(t * 0.55 + s * 7.31)
        a = math.floor(hash_d(seg, s * 3.1 + 1.7) * node_n)
        b = math.floor(hash_d(seg, s * 5.7 + 4.2) * node_n)
        if a == b:
            continue
        f = frac(t * 0.55 + s * 7.31)
        x, y, z = (lerp(nodes[a][k], nodes[b][k], f) for k in range(3))
        ln = max(1e-6, math.sqrt(x * x + y * y + z * z))
        px, py, zr = pt(x / ln, y / ln, z / ln)
        depth = (zr + 1) / 2
        dots.append(_dot(px, py, zr, (node_r * 1.5 + node_rd * depth) * rs, 0.05, 0.5 + 0.5 * depth))
    return finalize(dots, lines, o.get("rMin"))


def _smooth_e(x):
    return x * x * (3 - 2 * x)


def _poly_path(verts):
    V = len(verts)
    L = [math.hypot(verts[(i + 1) % V][0] - verts[i][0], verts[(i + 1) % V][1] - verts[i][1]) for i in range(V)]
    total = sum(L)

    def path(f):
        target, i = f * total, 0
        while target > L[i] and i < V - 1:
            target -= L[i]
            i += 1
        a, b = verts[i], verts[(i + 1) % V]
        ff = min(1, target / L[i]) if L[i] else 0
        return a[0] + (b[0] - a[0]) * ff, a[1] + (b[1] - a[1]) * ff
    return path


def _circle(f):
    a = -PI / 2 + f * 2 * PI
    return math.cos(a) * 0.24, math.sin(a) * 0.24


_CYCLE = [_circle, _poly_path([(0.0, -0.26), (0.24, 0.16), (-0.24, 0.16)]),
          _poly_path([(0, -0.2), (0.2, -0.2), (0.2, 0.2), (-0.2, 0.2), (-0.2, -0.2)])]
_HOLD, _MORPH = 1.4, 0.9
_SEG = _HOLD + _MORPH


def frame_morph(size, t, o):
    K = len(_CYCLE)
    tc = math.fmod(t, _SEG * K)
    k = math.floor(tc / _SEG)
    local = tc - k * _SEG
    m = _smooth_e((local - _HOLD) / _MORPH) if local > _HOLD else 0
    sprd = o.get("spread", 1)
    pa, pb = _CYCLE[k], _CYCLE[(k + 1) % K]
    M = 160
    pts = []
    for i in range(M):
        a, b = pa(i / M), pb(i / M)
        pts.append(((a[0] + (b[0] - a[0]) * m) * sprd, (a[1] + (b[1] - a[1]) * m) * sprd))
    L = [math.hypot(pts[(i + 1) % M][0] - pts[i][0], pts[(i + 1) % M][1] - pts[i][1]) for i in range(M)]
    total = sum(L)
    n = max(6, js_round(34 * o.get("iconD", 1)))
    re_ = o.get("rDot", 0.021) * 1.35 * sprd
    pulse = 1 + 0.02 * math.sin(local * 3.1)
    dots, c2, seg, acc = [], size / 2, 0, 0.0
    for k2 in range(n):
        target = (k2 / n) * total
        while acc + L[seg] < target and seg < M - 1:
            acc += L[seg]
            seg += 1
        a, b = pts[seg], pts[(seg + 1) % M]
        f = min(1, (target - acc) / L[seg]) if L[seg] else 0
        x = (a[0] + (b[0] - a[0]) * f) * pulse
        y = (a[1] + (b[1] - a[1]) * f) * pulse
        dots.append(_dot(c2 + x * size, c2 + y * size, 0, max(0.35, re_ * size), 0.1))
    return finalize(dots, [], o.get("rMin"))


MODE_FRAMES = {"orbits": frame_orbits, "globe": frame_globe, "rubik": frame_rubik, "wave": frame_wave,
               "web": frame_web, "braid": frame_braid, "ribbon": frame_ribbon, "ring": frame_ribbon,
               "morph": frame_morph}


def frame(state: str, size: int, t: float):
    """(dots, lines) for *state* at preset *size* (64 or 20), t in seconds
    of the preset's own clock (multiply wall time by its speed)."""
    mode, _, opts = resolve_preset(state, size)
    return MODE_FRAMES[mode](size, t, opts)


__all__ = ["frame", "resolve_preset", "MODE_FRAMES", "STATE_TO_MODE"]
