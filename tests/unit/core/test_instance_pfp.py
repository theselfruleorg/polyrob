"""Instance-scoped pfp (avatar) accessors on core.instance.

The pfp is the bot INSTANCE's one face — keyed by instance_id only (NOT per-user
like self_tier_root), living beside the SOUL/SELF docs under identity/{instance_id}/.
All accessors are fail-open (never raise) so a missing/corrupt avatar is a valid,
first-class state (avatar creation is optional and deferrable).
"""
import json
from pathlib import Path

from core.instance import (
    pfp_dir,
    pfp_path,
    load_pfp_meta,
    voice_signature,
    DEFAULT_INSTANCE_ID,
)


def test_pfp_dir_is_instance_scoped(tmp_path):
    # beside SOUL/SELF: <home>/identity/{instance_id}/pfp  (no user_ tier)
    assert pfp_dir(tmp_path, "rob") == tmp_path / "identity" / "rob" / "pfp"


def test_pfp_dir_unsafe_instance_falls_back_to_default(tmp_path):
    # an unsafe instance id must not traverse; it degrades to the default tenant
    got = pfp_dir(tmp_path, "../evil")
    assert got == tmp_path / "identity" / DEFAULT_INSTANCE_ID / "pfp"


def test_pfp_path_points_at_png(tmp_path):
    assert pfp_path(tmp_path, "rob") == tmp_path / "identity" / "rob" / "pfp" / "pfp.png"


def test_load_pfp_meta_none_when_absent(tmp_path):
    assert load_pfp_meta(tmp_path, "rob") is None


def test_load_pfp_meta_parses_written_json(tmp_path):
    d = tmp_path / "identity" / "rob" / "pfp"
    d.mkdir(parents=True)
    blob = {"generator": "mindprint@v2", "seed": "Rob Ottmachin",
            "voice": {"pitch": 1.1, "rate": 1.02, "timbre": 0.42}}
    (d / "pfp.json").write_text(json.dumps(blob), encoding="utf-8")
    assert load_pfp_meta(tmp_path, "rob") == blob


def test_load_pfp_meta_fail_open_on_bad_json(tmp_path):
    d = tmp_path / "identity" / "rob" / "pfp"
    d.mkdir(parents=True)
    (d / "pfp.json").write_text("{ not json", encoding="utf-8")
    assert load_pfp_meta(tmp_path, "rob") is None  # never raises


def test_voice_signature_returns_voice_block(tmp_path):
    d = tmp_path / "identity" / "rob" / "pfp"
    d.mkdir(parents=True)
    (d / "pfp.json").write_text(
        json.dumps({"voice": {"pitch": 1.1, "rate": 1.02, "timbre": 0.42}}),
        encoding="utf-8",
    )
    assert voice_signature(tmp_path, "rob") == {"pitch": 1.1, "rate": 1.02, "timbre": 0.42}


def test_voice_signature_none_when_absent(tmp_path):
    assert voice_signature(tmp_path, "rob") is None
