"""Pure-Python port of the Mindprint FIELD math (avatar/mindprint.js).

This is the ONE deliberate, parity-tested reimplementation of the engine. It exists
because the terminal is a different, low-resolution medium that cannot use canvas /
Chromium / Node, and because a live animated face needs a native render loop.

``avatar/mindprint.js`` stays the SSOT for the high-fidelity still + webview; this
module is a parity-tested twin used ONLY for terminal rendering. The deterministic
core (cyrb128/sfc32 PRNG, ``_gen_params`` incl. the tier roll, and the fbm noise) is
verified byte-for-byte against the JS engine (tests/unit/modules/pfp/test_mesh_parity.py).
The face-field formulas are ported line-for-line from ``render()``; because the
terminal samples the CONTINUOUS field (not the N×N mesh dots), terminal pixels are not
byte-identical to the canvas — but it is the SAME FACE (same traits, same geometry).

Public API:
    Mesh(config)            -> build params from a frozen config (config.render_seed).
    mesh.traits()           -> {tier,eyes,brow,mouth,antenna,aura,head,mode}
    mesh.voice()            -> {pitch,rate,timbre}
    mesh.grid(cols, rows, t=1.0, amp=0.0, still=False) -> list[list[(r,g,b)]]
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Tuple

from .config import render_seed, normalized_override

# --------------------------------------------------------------------------- #
# 32-bit integer helpers (JS bitwise ops act on int32; Math.imul is int32 mul) #
# --------------------------------------------------------------------------- #
_MASK = 0xFFFFFFFF


def _u32(x: int) -> int:
    return x & _MASK


def _i32(x: int) -> int:
    x &= _MASK
    return x - 0x100000000 if x >= 0x80000000 else x


def _imul(a: int, b: int) -> int:
    return _i32((_i32(a) * _i32(b)) & _MASK)


def cyrb128(s: str) -> List[int]:
    h1, h2, h3, h4 = 1779033703, 3144134277, 1013904242, 2773480762
    for ch in s:
        k = ord(ch)
        h1 = _i32(h2 ^ _imul(_i32(h1 ^ k), 597399067))
        h2 = _i32(h3 ^ _imul(_i32(h2 ^ k), 2869860233))
        h3 = _i32(h4 ^ _imul(_i32(h3 ^ k), 951274213))
        h4 = _i32(h1 ^ _imul(_i32(h4 ^ k), 2716044179))
    h1 = _imul(_i32(h3 ^ (_u32(h1) >> 18)), 597399067)
    h2 = _imul(_i32(h4 ^ (_u32(h2) >> 22)), 2869860233)
    h3 = _imul(_i32(h1 ^ (_u32(h3) >> 17)), 951274213)
    h4 = _imul(_i32(h2 ^ (_u32(h4) >> 19)), 2716044179)
    return [_u32(h1 ^ h2 ^ h3 ^ h4), _u32(h2 ^ h1), _u32(h3 ^ h1), _u32(h4 ^ h1)]


def sfc32(a: int, b: int, c: int, d: int):
    st = [_i32(a), _i32(b), _i32(c), _i32(d)]

    def rng() -> float:
        a, b, c, d = st
        t = _i32(_i32(a + b) + d)
        d = _i32(d + 1)
        a = _i32(b ^ (_u32(b) >> 9))
        b = _i32(c + _i32((c << 3) & _MASK))
        c = _i32(((c << 21) & _MASK) | (_u32(c) >> 11))
        c = _i32(c + t)
        st[0], st[1], st[2], st[3] = a, b, c, d
        return _u32(t) / 4294967296.0

    return rng


# --------------------------------------------------------------------------- #
# scalar helpers (mirror avatar/mindprint.js)                                  #
# --------------------------------------------------------------------------- #
def clamp(x, a, b):
    return a if x < a else b if x > b else x


def lerp(a, b, t):
    return a + (b - a) * t


def _sq(x):
    return x * x


def smooth(e0, e1, x):
    t = clamp((x - e0) / (e1 - e0), 0, 1)
    return t * t * (3 - 2 * t)


def gexp(x):
    return math.exp(-x)


def _js_round(x: float) -> int:
    return math.floor(x + 0.5)


def _tofixed2(x: float) -> float:
    # JS Number.prototype.toFixed(2): round to 2 decimals, ties away from zero.
    return math.floor(abs(x) * 100 + 0.5) / 100.0 * (1 if x >= 0 else -1)


def hsl2rgb(h, s, l) -> Tuple[int, int, int]:
    h = ((h % 360) + 360) % 360 / 360.0
    q = l * (1 + s) if l < 0.5 else l + s - l * s
    p = 2 * l - q

    def hk(t):
        t = (t % 1 + 1) % 1
        if t < 1 / 6:
            return p + (q - p) * 6 * t
        if t < 1 / 2:
            return q
        if t < 2 / 3:
            return p + (q - p) * (2 / 3 - t) * 6
        return p

    return (hk(h + 1 / 3) * 255, hk(h) * 255, hk(h - 1 / 3) * 255)


def hex2hs(hexstr: str) -> Dict[str, float]:
    n = int(hexstr[1:], 16)
    r, g, b = ((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255
    mx, mn = max(r, g, b), min(r, g, b)
    d = mx - mn
    h = 0.0
    if d:
        if mx == r:
            h = ((g - b) / d) % 6
        elif mx == g:
            h = (b - r) / d + 2
        else:
            h = (r - g) / d + 4
        h *= 60
    l = (mx + mn) / 2
    s = 0.0 if d == 0 else d / (1 - abs(2 * l - 1))
    return {"h": (h + 360) % 360, "s": clamp(s, 0.35, 1)}


# --------------------------------------------------------------------------- #
# trait genome (mirrors avatar/mindprint.js TIERS / TRAITS)                    #
# --------------------------------------------------------------------------- #
SHAPES = ("dot", "square", "scanline")
TIERS = [["basic", 68], ["uncommon", 21], ["rare", 8], ["legendary", 3]]
TRAITS = {
    "basic": {
        "eyes": [["round", 70], ["square", 30]],
        "brow": [["none", 82], ["raised", 18]],
        "mouth": [["smile", 42], ["grin", 26], ["calm", 32]],
        "ant": [["none", 54], ["single", 46]],
        "aura": [["none", 100]],
        "mode": [["solid", 88], ["neon", 12]],
    },
    "uncommon": {
        "eyes": [["round", 40], ["square", 24], ["sleepy", 16], ["visor", 12], ["wink", 8]],
        "brow": [["none", 50], ["raised", 26], ["worried", 24]],
        "mouth": [["smile", 30], ["grin", 22], ["calm", 18], ["open", 18], ["o", 12]],
        "ant": [["none", 24], ["single", 40], ["double", 36]],
        "aura": [["none", 64], ["blush", 36]],
        "mode": [["solid", 70], ["neon", 18], ["mono", 12]],
    },
    "rare": {
        "eyes": [["visor", 18], ["wink", 14], ["star", 22], ["heart", 18], ["sleepy", 14], ["square", 14]],
        "brow": [["none", 40], ["worried", 30], ["angry", 30]],
        "mouth": [["open", 20], ["o", 16], ["cat", 22], ["tongue", 18], ["zigzag", 24]],
        "ant": [["single", 18], ["double", 26], ["heart", 22], ["spiral", 20], ["sideways", 14]],
        "aura": [["blush", 22], ["sparkles", 30], ["halo", 30], ["bits", 18]],
        "mode": [["solid", 40], ["neon", 22], ["duotone", 22], ["mono", 16]],
    },
    "legendary": {
        "eyes": [["cyclops", 34], ["three", 30], ["star", 18], ["heart", 18]],
        "brow": [["angry", 44], ["worried", 30], ["none", 26]],
        "mouth": [["cat", 24], ["zigzag", 24], ["open", 22], ["tongue", 16], ["grin", 14]],
        "ant": [["double", 30], ["heart", 26], ["spiral", 26], ["sideways", 18]],
        "aura": [["halo", 34], ["thirdeye", 30], ["sparkles", 22], ["bits", 14]],
        "mode": [["holo", 40], ["duotone", 30], ["neon", 18], ["solid", 12]],
    },
}


def pick(rng, table):
    tot = sum(e[1] for e in table)
    x = rng() * tot
    for e in table:
        x -= e[1]
        if x < 0:
            return e[0]
    return table[-1][0]


# --------------------------------------------------------------------------- #
# the mesh                                                                     #
# --------------------------------------------------------------------------- #
_HEAD_NAMES = ("orb", "android", "tall")


class Mesh:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        seed = render_seed(config)
        s = cyrb128(seed or " ")
        self.hex = "0x" + format(_u32(s[0]), "08X")[:4]
        self.rng = sfc32(s[0], s[1], s[2], s[3])
        self.noise_seed = _u32(s[1])
        self._gen_params()
        self.override = normalized_override(config.get("override", {}))
        self._cfg = self._resolve_cfg()

    # -- deterministic params (EXACT draw order as _genParams in the JS) --
    def _gen_params(self):
        r = self.rng
        p: Dict[str, Any] = {}
        tier = pick(r, TIERS)
        p["tier"] = tier
        TT = TRAITS[tier]
        head_type = math.floor(r() * 3)
        p["headExp"] = [2.0, 4.0, 2.4][head_type]
        p["headType"] = head_type
        p["headRx"] = 0.33 + r() * 0.05
        p["headRy"] = (0.40 + r() * 0.04) if head_type == 2 else (0.34 + r() * 0.05)
        p["eyeSep"] = 0.150 + r() * 0.055
        p["eyeY"] = -0.02 + r() * 0.09
        p["eyeR"] = 0.055 + r() * 0.030
        p["eyeBright"] = 0.9 + r() * 0.35
        p["eyeStyle"] = pick(r, TT["eyes"])
        if p["eyeStyle"] == "cyclops":
            p["eyeR"] *= 1.7
            p["eyeSep"] = 0
        p["winkSide"] = -1 if r() < 0.5 else 1
        p["brow"] = pick(r, TT["brow"])
        p["cheekX"] = p["eyeSep"] + 0.012 + r() * 0.02
        p["cheekY"] = 0.13 + r() * 0.05
        p["cheekStr"] = 0.05 + r() * 0.08
        p["mouth"] = pick(r, TT["mouth"])
        p["mouthY"] = 0.15 + r() * 0.05
        p["mouthW"] = 0.095 + r() * 0.05
        p["smile"] = 0.030 + r() * 0.055
        p["mouthBright"] = 0.55 + r() * 0.28
        p["ant"] = pick(r, TT["ant"])
        p["antX"] = (r() - 0.5) * 0.10
        p["antTop"] = -(p["headRy"]) - 0.03 - r() * 0.03
        p["antSpread"] = 0.10 + r() * 0.05
        p["asymX"] = (r() - 0.5) * 0.05
        p["aura"] = pick(r, TT["aura"])
        self.blink_phase = r() * 8
        self.blink_every = 3.4 + r() * 3.2
        p["hue"] = math.floor(r() * 360)
        p["sat"] = 0.62 + r() * 0.34
        p["mode"] = pick(r, TT["mode"])
        p["densAuto"] = _js_round(lerp(46, 66, r()) / 2) * 2
        p["grainAuto"] = 0.12 + r() * 0.14
        p["shapeIdx"] = math.floor(r() * len(SHAPES))
        p["voice"] = {
            "pitch": _tofixed2(clamp(0.7 + r() * 0.9, 0, 2)),
            "rate": _tofixed2(clamp(0.86 + r() * 0.44, 0.5, 1.6)),
            "timbre": _tofixed2(r()),
        }
        p["spark"] = [[(r() - 0.5) * 0.9, (r() - 0.5) * 0.9, 0.02 + r() * 0.02] for _ in range(5)]
        p["bits"] = [[(r() - 0.5) * 1.0, (r() - 0.5) * 1.0, 0.4 + r() * 0.6] for _ in range(9)]
        self.p = p

    def _resolve_cfg(self) -> Dict[str, Any]:
        p, o = self.p, self.override
        hue, sat = p["hue"], p["sat"]
        color = self.config.get("override", {}).get("color")
        if color:
            hs = hex2hs(color)
            hue, sat = hs["h"], hs["s"]
        shape_idx = o["shape"] if o.get("shape") is not None else p["shapeIdx"]
        return {
            "hue": hue,
            "sat": sat,
            "mode": o["mode"] if o.get("mode") is not None else p["mode"],
            "grain": o["grain"] if o.get("grain") is not None else p["grainAuto"],
            "dens": o["dens"] if o.get("dens") is not None else p["densAuto"],
            "shape": SHAPES[shape_idx] if 0 <= shape_idx < len(SHAPES) else SHAPES[0],
        }

    @property
    def render_cfg(self) -> Dict[str, Any]:
        """The resolved render config (hue/sat/mode/grain/dens/shape) — mirrors JS ``cfg()``."""
        return dict(self._cfg)

    # -- public readouts --
    def traits(self) -> Dict[str, Any]:
        p = self.p
        return {
            "tier": p["tier"], "eyes": p["eyeStyle"], "brow": p["brow"],
            "mouth": p["mouth"], "antenna": p["ant"], "aura": p["aura"],
            "head": _HEAD_NAMES[p["headType"]], "mode": self._cfg["mode"],
        }

    def voice(self) -> Dict[str, float]:
        p = self.p
        o = self.config.get("override", {}).get("voice") or {}
        v = p["voice"]
        return {
            "pitch": o.get("pitch", v["pitch"]),
            "rate": o.get("rate", v["rate"]),
            "timbre": o.get("timbre", v["timbre"]),
        }

    # -- noise (mirrors _n2/_vnoise/_fbm) --
    def _n2(self, ix, iy):
        h = _i32(_imul(ix, 374761393) + _imul(iy, 668265263) + self.noise_seed)
        h = _imul(h ^ (_u32(h) >> 13), 1274126177)
        return _u32(h ^ (_u32(h) >> 16)) / 4294967296.0

    def _vnoise(self, x, y):
        xi, yi = math.floor(x), math.floor(y)
        xf, yf = x - xi, y - yi
        u = xf * xf * (3 - 2 * xf)
        v = yf * yf * (3 - 2 * yf)
        a = self._n2(xi, yi)
        b = self._n2(xi + 1, yi)
        c = self._n2(xi + 1, yi + 1)
        d = self._n2(xi, yi + 1)
        return lerp(lerp(a, b, u), lerp(d, c, u), v)

    def _fbm(self, x, y):
        f, amp, frq, norm = 0.0, 0.5, 1.0, 0.0
        for _ in range(3):
            f += amp * self._vnoise(x * frq, y * frq)
            norm += amp
            amp *= 0.55
            frq *= 2.1
        return f / norm

    # -- the per-pixel field (ported line-for-line from render()) --
    def _lum(self, Xb, Yb, t, still):
        p = self.p
        c = self._cfg
        grain_amt = c["grain"]
        breath = 1 if still else (1 + 0.035 * math.sin(t * 1.05))
        bw = 1 / breath
        rx, ry, nexp = p["headRx"], p["headRy"], p["headExp"]
        asym = p["asymX"] * 0.1
        eye_open = 1.0
        if not still:
            bt = (t + self.blink_phase) % self.blink_every
            if bt < 0.15:
                d = bt / 0.15
                eye_open = 0.10 + 0.90 * abs(2 * d - 1)
        spark = 1 if still else (0.85 + 0.22 * math.sin(t * 2.2 + self.blink_phase * 3))
        talk = clamp(max(0.0, 0.16 if p["mouth"] == "open" else (0.10 if p["mouth"] == "tongue" else 0.02)), 0, 1)

        X = Xb * bw
        Y = Yb * bw
        u = Xb + 0.5
        v = Yb + 0.5
        dh = (abs(X / rx) ** nexp + abs(Y / ry) ** nexp) ** (1 / nexp)
        mask = smooth(1.03, 0.80, dh)
        lum = mask * (0.24 + 0.34 * (1 - clamp(dh, 0, 1)))
        lum += 0.15 * gexp(_sq((dh - 1.0) / 0.13))
        grain = self._fbm(u * 6.0, v * 6.0) * 2 - 1
        lum += grain * grain_amt * (0.4 * mask + 0.05)
        ax = abs(X) - (1 if X > 0 else (-1 if X < 0 else 0)) * asym

        e_style = p["eyeStyle"]
        if e_style == "visor":
            if abs(Y - p["eyeY"]) < 0.045 and abs(X) < rx * 0.72:
                lum += p["eyeBright"] * 1.0 * gexp(_sq((Y - p["eyeY"]) / 0.026)) * mask
                sx = (0.3 if still else 0.6 * math.sin(t * 1.7)) * rx * 0.6
                lum += 0.7 * gexp(_sq((X - sx) / 0.04) + _sq((Y - p["eyeY"]) / 0.03)) * mask
        else:
            for e in self._eyes():
                edx = X - e["x"]
                oy = e["R"] * (eye_open if e["blink"] else 1) + 1e-4
                edy = Y - e["y"]
                st = e["style"]
                B = e["B"]
                R = e["R"]
                if st == "wink":
                    cv = e["y"] - 0.012 - 0.05 * (1 - _sq(clamp(edx / (R * 1.4), -1, 1)))
                    I = B * 0.9 * gexp(_sq((Y - cv) / 0.012)) if abs(edx) < R * 1.5 else 0.0
                elif st == "square":
                    bx = max(0.0, abs(edx) - R * 0.55) / (R * 0.5)
                    by = max(0.0, abs(edy) - oy * 0.55) / (oy * 0.5)
                    e2 = bx * bx + by * by
                    I = B * (gexp(e2 * 2.1) + 0.85 * gexp(e2 * 6.5))
                elif st == "sleepy":
                    e2 = _sq(edx / R) + _sq(edy / (oy * 0.5))
                    I = B * (gexp(e2 * 2.4) + 0.7 * gexp(e2 * 7))
                elif st == "star":
                    e2 = _sq(edx / R) + _sq(edy / oy)
                    ang = math.atan2(edy, edx)
                    star = 0.5 + 0.5 * math.cos(5 * ang)
                    I = B * (gexp(e2 * 1.6) * (0.35 + 0.65 * star * star) + 0.5 * gexp(e2 * 9))
                elif st == "heart":
                    Rh = R * 1.15
                    hl = gexp(_sq((edx + 0.34 * Rh) / (0.52 * Rh)) + _sq((edy + 0.30 * Rh) / (0.52 * Rh)))
                    hr = gexp(_sq((edx - 0.34 * Rh) / (0.52 * Rh)) + _sq((edy + 0.30 * Rh) / (0.52 * Rh)))
                    hb = gexp(_sq(edx / (0.62 * Rh)) + _sq((edy - 0.52 * Rh) / (0.72 * Rh)))
                    I = B * clamp(hl + hr + hb, 0, 1.25)
                else:  # round
                    e2 = _sq(edx / R) + _sq(edy / oy)
                    I = B * (gexp(e2 * 2.1) + 0.85 * gexp(e2 * 6.5))
                    I += spark * 0.55 * gexp(_sq((edx - R * 0.30) / (R * 0.32)) + _sq((edy + oy * 0.32) / (oy * 0.32)))
                lum += I * mask

        # brows
        if p["brow"] != "none" and e_style != "visor":
            bs = 1 if p["brow"] == "angry" else (-1 if p["brow"] == "worried" else 0)
            by0 = p["eyeY"] - p["eyeR"] - 0.055
            for sx in (-1, 1):
                bx = X - sx * p["eyeSep"]
                tilt = bs * sx * 2.6
                yline = by0 + tilt * bx + (-0.02 if p["brow"] == "raised" else 0)
                if abs(bx) < p["eyeR"] * 1.3:
                    lum += 0.5 * gexp(_sq((Y - yline) / 0.011)) * mask

        # cheeks / blush
        cheek = p["cheekStr"] * 2.4 if p["aura"] == "blush" else p["cheekStr"]
        lum += cheek * gexp(_sq((ax - p["cheekX"]) / 0.10) + _sq((Y - p["cheekY"]) / 0.08)) * mask

        # mouth
        m = p["mouth"]
        mw = p["mouthW"]
        if m == "o":
            dd = math.sqrt(_sq(ax / 1.0) + _sq((Y - p["mouthY"] - 0.01) / 1.0))
            lum += p["mouthBright"] * gexp(_sq((dd - 0.055) / 0.02)) * mask
        elif m == "cat":
            if ax < mw * 1.2:
                w = p["mouthY"] + 0.02 * math.cos(ax / mw * 6.283 * 1.5)
                lum += p["mouthBright"] * gexp(_sq((Y - w) / 0.014)) * mask
        elif m == "zigzag":
            if ax < mw * 1.1:
                zz = p["mouthY"] + 0.018 * (2 * abs(((ax / mw * 4) % 1) - 0.5) - 0.5)
                lum += p["mouthBright"] * 0.9 * gexp(_sq((Y - zz) / 0.012)) * mask
        elif m == "tongue":
            curve_y = p["mouthY"] + p["smile"] * (1 - _sq(clamp(ax / mw, 0, 1)))
            if ax < mw * 1.15:
                lum += p["mouthBright"] * gexp(_sq((Y - curve_y) / 0.016)) * mask
            lum += 0.6 * gexp(_sq(ax / (mw * 0.5)) + _sq((Y - p["mouthY"] - 0.05) / 0.03)) * mask
        else:  # smile / grin / calm / open
            if ax < mw * 1.15:
                curve_y = p["mouthY"] + p["smile"] * (1 - _sq(ax / mw))
                lum += p["mouthBright"] * gexp(_sq((Y - curve_y) / (0.016 + 0.012 * talk))) * mask * (0.65 + 0.35 * (1 - talk))
            if talk > 0.02:
                lum += talk * 0.85 * gexp(_sq(ax / (mw * 0.72)) + _sq((Y - p["mouthY"] - 0.004) / (0.014 + talk * 0.05))) * mask

        # antenna(s)
        if p["ant"] != "none":
            lum += self._antenna(X, Y, ry)

        # aura extras
        aura = p["aura"]
        if aura == "halo":
            r2 = math.sqrt(_sq(X / 1.05) + _sq((Y + ry + 0.12) / 0.5))
            lum += 0.6 * gexp(_sq((r2 - 0.30) / 0.05))
        elif aura == "thirdeye":
            e2 = _sq(X / (p["eyeR"] * 0.8)) + _sq((Y - (p["eyeY"] - 0.16)) / (p["eyeR"] * 0.8))
            lum += 0.9 * p["eyeBright"] * (gexp(e2 * 2.2) + 0.8 * gexp(e2 * 7)) * mask
        elif aura == "sparkles":
            for sp in p["spark"]:
                tw = 1 if still else (0.6 + 0.4 * math.sin(t * 3 + sp[0] * 10))
                lum += 0.6 * tw * gexp(_sq((X - sp[0]) / sp[2]) + _sq((Y - sp[1]) / sp[2]))
        elif aura == "bits":
            for bb in p["bits"]:
                lum += 0.22 * bb[2] * gexp(_sq((X - bb[0]) / 0.02) + _sq((Y - bb[1]) / 0.02))

        return lum, dh

    def _eyes(self):
        p = self.p
        st = p["eyeStyle"]
        if st == "cyclops":
            return [{"x": 0, "y": p["eyeY"], "R": p["eyeR"], "style": "round", "blink": True, "B": p["eyeBright"]}]

        def mk(sx, style):
            return {"x": sx * p["eyeSep"], "y": p["eyeY"], "R": p["eyeR"],
                    "style": style, "blink": style != "wink", "B": p["eyeBright"]}

        eyes = []
        if st == "wink":
            eyes.append(mk(-1, "wink" if p["winkSide"] < 0 else "round"))
            eyes.append(mk(1, "round" if p["winkSide"] < 0 else "wink"))
        elif st == "visor":
            pass
        else:
            eyes.append(mk(-1, st))
            eyes.append(mk(1, st))
        if st == "three":
            eyes.append({"x": 0, "y": p["eyeY"] - 0.17, "R": p["eyeR"] * 0.8,
                         "style": "round", "blink": True, "B": p["eyeBright"]})
        return eyes

    def _antenna(self, X, Y, ry):
        p = self.p
        add = 0.0

        def draw(axpos, tip):
            nonlocal add
            if tip == "heart":
                R = 0.03
                add += 0.7 * gexp(_sq((X - axpos + 0.013) / (0.6 * R)) + _sq((Y - p["antTop"] + 0.008) / (0.6 * R)))
                add += 0.7 * gexp(_sq((X - axpos - 0.013) / (0.6 * R)) + _sq((Y - p["antTop"] + 0.008) / (0.6 * R)))
                add += 0.7 * gexp(_sq((X - axpos) / (0.8 * R)) + _sq((Y - p["antTop"] - 0.02) / (0.9 * R)))
            elif tip == "spiral":
                for k in range(7):
                    a = k * 0.9
                    rr = 0.006 + k * 0.004
                    sxp = axpos + math.cos(a) * rr
                    syp = p["antTop"] + math.sin(a) * rr
                    add += 0.5 * gexp(_sq((X - sxp) / 0.014) + _sq((Y - syp) / 0.014))
            else:
                add += 0.7 * gexp(_sq((X - axpos) / 0.017) + _sq((Y - p["antTop"]) / 0.02))
            if axpos - 0.008 < X < axpos + 0.008 and Y < (-ry * 0.72) and Y > p["antTop"]:
                add += 0.22

        ant = p["ant"]
        if ant == "single":
            draw(p["antX"], "dot")
        elif ant == "heart":
            draw(p["antX"], "heart")
        elif ant == "spiral":
            draw(p["antX"], "spiral")
        elif ant == "sideways":
            add += 0.7 * gexp(_sq((X - (p["antX"] + 0.14)) / 0.02) + _sq((Y - p["antTop"] * 0.6) / 0.02))
            if (p["antTop"] * 0.6 - 0.006 < Y < p["antTop"] * 0.6 + 0.006
                    and p["antX"] < X < p["antX"] + 0.14):
                add += 0.2
        elif ant == "double":
            draw(-p["antSpread"], "dot")
            draw(p["antSpread"], "dot")
        return add

    def _color(self, lum, dh, Xb, Yb, still):
        c = self._cfg
        hue, sat, mode = c["hue"], c["sat"], c["mode"]
        v01 = clamp(lum, 0, 1)
        H, S = hue, sat
        if mode == "neon":
            S = min(1, sat * 1.35)
        elif mode == "mono":
            S = 0.06
        elif mode == "duotone":
            H = hue + 150 * smooth(-0.25, 0.30, Yb)
        elif mode == "holo":
            H = hue + 46 * math.sin(dh * 3.0 + Yb * 1.5 + Xb * 0.6)
        L = 0.05 + 0.92 * (v01 ** 0.85)
        Sc = S * (1 - 0.68 * smooth(0.60, 1.0, v01))
        r, g, b = hsl2rgb(H, Sc, L)
        return (int(r), int(g), int(b))

    def grid(self, cols: int, rows: int, t: float = 1.0, amp: float = 0.0,
             still: bool = False) -> List[List[Tuple[int, int, int]]]:
        """Sample the face field over cols×rows, returning per-cell RGB (0,0,0 = empty)."""
        glow = 1 + amp * 0.3
        span = 1.08  # a touch of margin around the head
        out: List[List[Tuple[int, int, int]]] = []
        for j in range(rows):
            Yb = ((j + 0.5) / rows - 0.5) * span
            row: List[Tuple[int, int, int]] = []
            for i in range(cols):
                Xb = ((i + 0.5) / cols - 0.5) * span
                lum, dh = self._lum(Xb, Yb, t, still)
                lum *= glow
                if lum <= 0.05:
                    row.append((0, 0, 0))
                else:
                    row.append(self._color(lum, dh, Xb, Yb, still))
            out.append(row)
        return out
