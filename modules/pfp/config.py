"""Frozen avatar-config load + normalization.

The frozen config (``avatar/config/rob.json`` and the runtime ``pfp.json``) is the
reproducibility SSOT for an agent's face + voice::

    {generator, seed, variant, size, override:{color?,mode?,dens?,grain?,shape?,voice?},
     traits, voice}

This module is pure Python (no browser) so it is fully CI-testable. It owns two
engine-fidelity rules the rest of the pipeline depends on:

1. **Render seed = ``seed + variant``** (concatenated), then hashed by the engine.
2. **``override.shape`` is stored as a NAME but the engine consumes an INDEX**
   (``avatar/mindprint.js`` ``SHAPES``), so it must be mapped on the way in.

``traits`` are RECORDED for display/search/NFT but are NOT authoritative for pixels —
the engine re-derives them from ``seed + variant + override``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

# Mirrors avatar/mindprint.js `SHAPES` (the index order the engine expects).
SHAPES = ("dot", "square", "scanline")

# Engine versions this pipeline knows how to render. A future engine change bumps
# the generator (e.g. mindprint@v3) and must be added here EXPLICITLY, so a stale or
# foreign blob can never silently render a different face.
SUPPORTED_GENERATORS = frozenset({"mindprint@v2"})

# override keys the engine understands (besides `shape`, handled specially).
_PASSTHROUGH_KEYS = frozenset({"color", "mode", "dens", "grain", "voice"})


class FrozenConfigError(ValueError):
    """Raised when a frozen config is malformed or from an unsupported engine."""


def load_frozen_config(source: Any) -> Dict[str, Any]:
    """Load + validate a frozen config from a dict or a JSON file path.

    Raises :class:`FrozenConfigError` on a non-dict, an unsupported ``generator``,
    or a missing/empty ``seed``.
    """
    if isinstance(source, dict):
        cfg = source
    elif isinstance(source, (str, Path)):
        try:
            cfg = json.loads(Path(source).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise FrozenConfigError(f"could not read config {source!r}: {e}") from e
    else:
        raise FrozenConfigError(f"config must be a dict or path, got {type(source).__name__}")

    if not isinstance(cfg, dict):
        raise FrozenConfigError("config must be a JSON object")

    gen = cfg.get("generator")
    if not (isinstance(gen, str) and gen in SUPPORTED_GENERATORS):
        raise FrozenConfigError(
            f"unsupported generator {gen!r} (supported: {sorted(SUPPORTED_GENERATORS)})"
        )

    seed = cfg.get("seed")
    if not (isinstance(seed, str) and seed.strip()):
        raise FrozenConfigError("config.seed must be a non-empty string")

    return cfg


def render_seed(config: Dict[str, Any]) -> str:
    """The exact string fed to the engine PRNG: ``seed + variant`` (variant may be empty)."""
    return f"{config.get('seed', '')}{config.get('variant') or ''}"


def normalized_override(override: Dict[str, Any]) -> Dict[str, Any]:
    """Return an override dict the engine can consume directly.

    Maps ``shape`` name -> index (invalid names are dropped -> engine auto), passes
    through the known keys, and drops anything unrecognized.
    """
    out: Dict[str, Any] = {}
    for k, v in (override or {}).items():
        if k == "shape":
            if v in SHAPES:
                out["shape"] = SHAPES.index(v)
            # unknown shape name -> omit (engine falls back to the seeded shape)
        elif k in _PASSTHROUGH_KEYS:
            out[k] = v
    return out
