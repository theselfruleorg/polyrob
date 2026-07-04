"""Persist the frozen avatar into the instance identity home.

``generate_pfp`` writes ``<home>/identity/{instance_id}/pfp/{pfp.png,pfp.json}``. It
is idempotent ("created once" — a no-op if the PNG exists unless ``force``), and it
falls back to a committed reference PNG when Chromium/Playwright is unavailable, so the
avatar always materializes even headless / in CI. Trait/voice/seed_hex meta is computed
by the pure-Python mesh (no browser), so the identity record is complete on either path.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from core.instance import pfp_path, pfp_dir, load_pfp_meta, DEFAULT_INSTANCE_ID
from .config import load_frozen_config
from .mesh import Mesh
from .renderer import render_still, PfpRenderUnavailable

_AVATAR = Path(__file__).resolve().parents[2] / "avatar"
_DEFAULT_CONFIG = _AVATAR / "config" / "rob.json"
_DEFAULT_REFERENCE = _AVATAR / "renders" / "rob.png"


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def generate_pfp(
    home_dir,
    instance_id: str = DEFAULT_INSTANCE_ID,
    *,
    config: Optional[Dict[str, Any]] = None,
    config_path=None,
    force: bool = False,
    reference_png=None,
    size: Optional[int] = None,
) -> Dict[str, Any]:
    """Render + persist the instance avatar; return the ``pfp.json`` meta.

    Idempotent unless ``force``. Raises :class:`PfpRenderUnavailable` only if BOTH the
    headless render fails AND no reference PNG is available.
    """
    cfg = config if config is not None else load_frozen_config(config_path or _DEFAULT_CONFIG)

    target = pfp_path(home_dir, instance_id)
    meta_path = pfp_dir(home_dir, instance_id) / "pfp.json"

    if target.is_file() and not force:
        existing = load_pfp_meta(home_dir, instance_id)
        if existing is not None:
            return existing  # created once — no rewrite

    mesh = Mesh(cfg)  # traits/voice/seed_hex — pure Python, no browser

    try:
        render_still(cfg, target, size=size)
        rendered_by = "playwright-chromium"
    except PfpRenderUnavailable:
        ref = Path(reference_png) if reference_png else _DEFAULT_REFERENCE
        if ref.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ref, target)
            rendered_by = "committed-reference"
        else:
            raise

    meta: Dict[str, Any] = {
        **cfg,
        "instance_id": instance_id,
        "seed_hex": mesh.hex,
        "traits": mesh.traits(),
        "voice": mesh.voice(),
        "config_ref": "avatar/config/rob.json",
        "rendered_by": rendered_by,
        "created_at": _now_iso(),
    }
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta
