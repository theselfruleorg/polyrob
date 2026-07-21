"""Identity-config helpers — mint, shuffle, and normalize frozen avatar configs.

Pure dict operations shared by every setup surface (the `polyrob pfp` CLI, the
REPL `/pfp`, and the webview /identity setup routes). Shuffle semantics mirror the
studio (`avatar/studio.html`): a face shuffle re-rolls the variant (everything
seeded) while PRESERVING a pinned voice; a voice shuffle pins a fresh voice
override and leaves the face untouched.
"""
from __future__ import annotations

import copy
import random
import string
from typing import Any, Dict

DEFAULT_SEED = "Rob Ottmachin"
GENERATOR = "mindprint@v2"


def b36_variant() -> str:
    """A shuffle variant string, matching the studio's ``"#"+base36(random)`` format."""
    n = random.randint(1, 10 ** 9)
    digits = string.digits + string.ascii_lowercase
    s = ""
    while n:
        n, r = divmod(n, 36)
        s = digits[r] + s
    return "#" + s


def default_config(seed: str = DEFAULT_SEED) -> Dict[str, Any]:
    return {"generator": GENERATOR, "seed": seed, "variant": "", "size": 768, "override": {}}


def random_config(seed: str = DEFAULT_SEED) -> Dict[str, Any]:
    """A fresh random identity: the seed (name) plus a random shuffle variant.

    The variant re-rolls EVERYTHING seeded — face geometry, traits, palette, and the
    voice signature — exactly like the studio's shuffle."""
    cfg = default_config(seed)
    cfg["variant"] = b36_variant()
    return cfg


def shuffle_face(config: Dict[str, Any]) -> Dict[str, Any]:
    """New look (fresh variant), PRESERVING a pinned voice (§ independent shuffle)."""
    out = copy.deepcopy(config)
    out["variant"] = b36_variant()
    voice = (config.get("override") or {}).get("voice")
    out["override"] = {"voice": voice} if voice else {}
    return out


def shuffle_voice(config: Dict[str, Any]) -> Dict[str, Any]:
    """New voice signature ONLY; the face (seed+variant+look) is untouched."""
    out = copy.deepcopy(config)
    out.setdefault("override", {})
    out["override"]["voice"] = {
        "pitch": round(0.75 + random.random() * 0.70, 2),   # 0.75–1.45
        "rate": round(0.90 + random.random() * 0.35, 2),    # 0.90–1.25
        "timbre": round(random.random(), 2),                # 0–1
    }
    return out


def core_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Strip a config (or a pfp.json meta superset) down to the frozen-config keys."""
    return {
        "generator": cfg.get("generator", GENERATOR),
        "seed": cfg.get("seed", DEFAULT_SEED),
        "variant": cfg.get("variant") or "",
        "size": cfg.get("size", 768),
        "override": copy.deepcopy(cfg.get("override") or {}),
    }
