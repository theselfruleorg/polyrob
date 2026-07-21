"""Persist the frozen avatar into the instance identity home.

``generate_pfp`` writes ``<home>/identity/{instance_id}/pfp/{pfp.png,pfp.json}``. It
is idempotent ("created once" — a no-op if the PNG exists unless ``force``). Render
chain: exact JS engine via Chromium (``renderer``) → native Pillow mesh renderer
(``still`` — same face, no browser) → committed reference PNG (stock identity ONLY;
for any other config the reference pixels would contradict the recorded traits).
Trait/voice/seed_hex meta is computed by the pure-Python mesh (no browser), so the
identity record is complete on every path.

**One-time setup contract:** a freshly minted identity is a DRAFT
(``meta["locked"] = False``) that setup verbs (randomize/pick) may re-roll. Accepting
it (``pfp keep`` / pick's save) locks it — ``locked: True`` — and from then on the
identity is IMMUTABLE: any forced write with a *different* identity raises
:class:`PfpLockedError` (re-rendering the SAME identity's pixels stays allowed, e.g.
after installing the browser extra). A meta without the ``locked`` key (legacy) or a
PNG without meta is treated as LOCKED — created-once is the conservative default.
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


class PfpLockedError(RuntimeError):
    """The avatar identity has been kept (locked) and cannot be changed.

    Setup happens ONCE: re-roll during the draft phase, then ``keep``. The only
    escape hatch is deliberately manual — delete the instance ``pfp/`` directory."""


def _identity_of(cfg: Dict[str, Any]) -> tuple:
    """The identity triple — what makes two configs the SAME face + voice."""
    return (cfg.get("seed"), cfg.get("variant") or "", cfg.get("override") or {})


def is_locked(meta: Optional[Dict[str, Any]]) -> bool:
    """Locked unless the meta explicitly says draft — legacy/absent meta = locked."""
    if not isinstance(meta, dict):
        return True
    return bool(meta.get("locked", True))


def _is_stock_identity(cfg: Dict[str, Any]) -> bool:
    """True when ``cfg`` is the committed default identity — the ONLY config for
    which the committed reference PNG shows the right face."""
    try:
        stock = load_frozen_config(_DEFAULT_CONFIG)
    except Exception:
        return False
    return (cfg.get("seed") == stock.get("seed")
            and (cfg.get("variant") or "") == (stock.get("variant") or "")
            and (cfg.get("override") or {}) == (stock.get("override") or {}))


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
    locked: Optional[bool] = None,
) -> Dict[str, Any]:
    """Render + persist the instance avatar; return the ``pfp.json`` meta.

    Idempotent unless ``force``. A forced write over a LOCKED (kept) avatar is allowed
    only when the identity is unchanged (a pixels-only re-render) — a different
    identity raises :class:`PfpLockedError`. ``locked``: ``None`` inherits the stored
    state (fresh mint → draft/``False``); ``True`` marks the write as an acceptance.
    Raises :class:`PfpRenderUnavailable` only if BOTH the headless render fails AND no
    reference PNG is available.
    """
    cfg = config if config is not None else load_frozen_config(config_path or _DEFAULT_CONFIG)

    target = pfp_path(home_dir, instance_id)
    meta_path = pfp_dir(home_dir, instance_id) / "pfp.json"

    existing = load_pfp_meta(home_dir, instance_id) if target.is_file() else None

    if target.is_file() and not force:
        if existing is not None:
            return existing  # created once — no rewrite

    if target.is_file() and force and is_locked(existing):
        # The identity is kept (or unverifiable: legacy/missing meta) — only a
        # pixels-only re-render of the SAME identity may proceed.
        if existing is None or _identity_of(cfg) != _identity_of(existing):
            raise PfpLockedError(
                f"the {instance_id!r} avatar identity is kept and cannot be changed "
                "(setup happens once). To start over deliberately, delete "
                f"{pfp_dir(home_dir, instance_id)}"
            )

    if locked is None:
        locked = is_locked(existing) if existing is not None else False  # fresh mint = draft

    mesh = Mesh(cfg)  # traits/voice/seed_hex — pure Python, no browser

    try:
        render_still(cfg, target, size=size)
        rendered_by = "playwright-chromium"
    except PfpRenderUnavailable as chromium_err:
        try:
            from .still import render_still_mesh
            render_still_mesh(cfg, target, size=size)
            rendered_by = "pillow-mesh"
        except Exception:
            # Last resort: the committed reference PNG — but ONLY for the stock
            # identity. For any other (e.g. randomized) config those pixels would
            # contradict the traits/voice we record, so we fail honestly instead.
            ref = Path(reference_png) if reference_png else _DEFAULT_REFERENCE
            if ref.is_file() and (reference_png is not None or _is_stock_identity(cfg)):
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ref, target)
                rendered_by = "committed-reference"
            else:
                raise chromium_err

    meta: Dict[str, Any] = {
        **cfg,
        "instance_id": instance_id,
        "seed_hex": mesh.hex,
        "traits": mesh.traits(),
        "voice": mesh.voice(),
        "config_ref": str(config_path) if config_path else (
            "avatar/config/rob.json" if config is None else "inline"),
        "rendered_by": rendered_by,
        "created_at": _now_iso(),
        "locked": bool(locked),
    }
    if existing is not None and existing.get("kept_at"):
        meta["kept_at"] = existing["kept_at"]
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta


def keep_pfp(home_dir, instance_id: str = DEFAULT_INSTANCE_ID) -> Dict[str, Any]:
    """Accept the draft identity: set ``locked: True`` (one-way; idempotent).

    Raises :class:`FileNotFoundError` when no avatar exists yet."""
    meta = load_pfp_meta(home_dir, instance_id)
    if meta is None or not pfp_path(home_dir, instance_id).is_file():
        raise FileNotFoundError("no avatar to keep — generate one first")
    if not is_locked(meta):
        meta["locked"] = True
        meta["kept_at"] = _now_iso()
        (pfp_dir(home_dir, instance_id) / "pfp.json").write_text(
            json.dumps(meta, indent=2), encoding="utf-8")
    return meta
