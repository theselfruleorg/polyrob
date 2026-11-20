"""The holdings watch's state file — its path and its reads (split out of
``core/wallet/nft_holdings.py`` so the guard's ERC-6551 rules can read which accounts the agent
holds without importing the watch).

State: ``<self tier>/agent_nft/holdings.json`` (``tracked``, ``scanned`` per collection, the
last ``lost`` rows).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

STATE_FILE = ("agent_nft", "holdings.json")


def state_path(home_dir=None, user_id: Optional[str] = None, instance_id: Optional[str] = None) -> Path:
    from core.instance import resolve_instance_id, resolve_owner_user_id, self_tier_root
    from core.runtime_paths import resolve_data_home
    home_dir = home_dir if home_dir is not None else resolve_data_home()
    root = Path(self_tier_root(home_dir, user_id or resolve_owner_user_id(),
                              instance_id or resolve_instance_id()))
    return root.joinpath(*STATE_FILE)


def load_state(path: Path) -> Dict[str, Any]:
    """The state; no file = empty. An unreadable file RAISES (never "holds nothing")."""
    p = Path(path)
    if not p.exists():
        return {"version": 1, "tracked": {}, "scanned": {}, "lost": []}
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("tracked"), dict):
        raise ValueError(f"{p} is not a holdings state file")
    data.setdefault("scanned", {})
    data.setdefault("lost", [])
    return data


def save_state(path: Path, state: Dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(p)


def tracked(home_dir=None, user_id=None, instance_id=None, *, chain: Optional[str] = None) -> List[Dict[str, Any]]:
    """The tracked NFTs (optionally on one chain), oldest first. Raises when unreadable."""
    rows = list(load_state(state_path(home_dir, user_id, instance_id))["tracked"].values())
    if chain:
        rows = [r for r in rows if r.get("chain") == chain]
    return sorted(rows, key=lambda r: (r.get("since") or 0, r.get("token_id") or 0))
