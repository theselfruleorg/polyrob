"""M1: `polyrob update` snapshot must cover <data_home>/wallet.

`wallet/meta.json` (the write-once derivation record) and `wallet/audit.jsonl` (the
append-only spend audit) previously were NOT captured by `resolve_update_context` —
only `identity`/`skills` were. A restore would resurrect the seed's `.env`/config
copy but silently drop `meta.json`, so `resolve_scheme` falls back to `legacy`
(H1/H2 chain: silent derivation-scheme flip) and the spend/replay caps reset. This
pins that `<data_home>/wallet` is included when present and omitted when absent
(mirroring the identity/skills `is_dir()` guard, Task 10).
"""
from pathlib import Path

from cli.update.context import resolve_update_context


def test_snapshot_includes_wallet_dir_when_present(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    (tmp_path / "identity").mkdir()
    (tmp_path / "wallet").mkdir()
    (tmp_path / "wallet" / "meta.json").write_text("{}")
    (tmp_path / "wallet" / "audit.jsonl").write_text("")
    ctx = resolve_update_context()
    assert (tmp_path / "wallet") in ctx.dir_paths, "wallet dir must be snapshotted (M1)"
    assert (tmp_path / "identity") in ctx.dir_paths, "identity dir still snapshotted"


def test_snapshot_omits_wallet_dir_when_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    (tmp_path / "identity").mkdir()
    # no wallet dir created (wallet feature never used on this box)
    ctx = resolve_update_context()
    assert (tmp_path / "wallet") not in ctx.dir_paths
