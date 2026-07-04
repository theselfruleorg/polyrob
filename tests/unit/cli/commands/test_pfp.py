"""`polyrob pfp` command group: show (live render) + pick helpers.

Avatar creation is optional/deferrable, so `pfp show` must work with no stored
avatar (renders from a seed). The pick flow shuffles FACE and VOICE independently.
"""
import json

from click.testing import CliRunner

from cli.commands.pfp import (
    pfp,
    default_config,
    shuffle_face,
    shuffle_voice,
    save_config,
)
from modules.pfp.config import load_frozen_config


def test_default_config_is_valid():
    cfg = default_config("Ada Nine")
    assert load_frozen_config(cfg)["seed"] == "Ada Nine"
    assert cfg["variant"] == "" and cfg["override"] == {}


def test_shuffle_face_changes_variant_and_preserves_voice():
    cfg = default_config("Ada Nine")
    cfg["override"] = {"voice": {"pitch": 1.4, "rate": 1.0, "timbre": 0.2}, "color": "#56e2c2"}
    out = shuffle_face(cfg)
    assert out["variant"] and out["variant"] != cfg["variant"]      # new face
    assert out["override"].get("voice") == cfg["override"]["voice"]  # voice preserved
    assert "color" not in out["override"]                            # look re-rolled (color reset)


def test_shuffle_voice_changes_voice_and_keeps_face():
    cfg = default_config("Ada Nine")
    cfg["variant"] = "#abc"
    out = shuffle_voice(cfg)
    assert out["variant"] == "#abc"                     # face untouched
    v = out["override"]["voice"]
    assert 0.75 <= v["pitch"] <= 1.45
    assert 0.90 <= v["rate"] <= 1.25
    assert 0.0 <= v["timbre"] <= 1.0


def test_save_config_roundtrips(tmp_path):
    cfg = default_config("Ada Nine")
    p = tmp_path / "sub" / "rob.json"
    save_config(cfg, p)
    assert load_frozen_config(p)["seed"] == "Ada Nine"


def test_show_renders_text_line_without_truecolor():
    # CliRunner is non-tty -> the universal text fallback (no PNG, no crash)
    res = CliRunner().invoke(pfp, ["show", "--seed", "Ada Nine"], env={"COLORTERM": ""})
    assert res.exit_code == 0, res.output
    assert "seed 0x" in res.output
    assert "eyes" in res.output


def test_show_works_with_no_stored_avatar():
    # deferrable: never errors when there is no avatar yet
    res = CliRunner().invoke(pfp, ["show"], env={"COLORTERM": ""})
    assert res.exit_code == 0, res.output


def test_generate_invokes_store_and_reports(monkeypatch):
    import cli.commands.pfp as pfpmod

    monkeypatch.setattr(pfpmod, "_instance_home", lambda: ("/tmp/home", "rob"))

    def fake_generate(home, instance_id, **kw):
        assert (home, instance_id) == ("/tmp/home", "rob")
        return {"rendered_by": "playwright-chromium"}

    import modules.pfp.store as store
    monkeypatch.setattr(store, "generate_pfp", fake_generate)
    res = CliRunner().invoke(pfp, ["generate"])
    assert res.exit_code == 0, res.output
    assert "generated" in res.output and "playwright-chromium" in res.output


def test_push_errors_without_a_stored_avatar(monkeypatch, tmp_path):
    import cli.commands.pfp as pfpmod
    monkeypatch.setattr(pfpmod, "_instance_home", lambda: (tmp_path, "rob"))
    res = CliRunner().invoke(pfp, ["push", "--twitter"])
    assert res.exit_code != 0
    assert "no avatar yet" in res.output


def test_pick_loop_shuffles_and_saves(monkeypatch, tmp_path):
    import cli.commands.pfp as pfpmod
    target = tmp_path / "rob.json"
    monkeypatch.setattr(pfpmod, "_DEFAULT_CONFIG_PATH", target)
    keys = iter(["s", "v", "\r"])           # shuffle face -> shuffle voice -> save
    monkeypatch.setattr("click.getchar", lambda: next(keys))
    res = CliRunner().invoke(pfp, ["pick", "--seed", "Ada Nine"], env={"COLORTERM": ""})
    assert res.exit_code == 0, res.output
    saved = load_frozen_config(target)
    assert saved["variant"]                        # face was shuffled
    assert "voice" in saved["override"]            # voice was shuffled independently


def test_studio_opens_the_local_studio_html(monkeypatch):
    import cli.commands.pfp as pfpmod
    opened = {}
    monkeypatch.setattr("webbrowser.open", lambda uri: opened.setdefault("uri", uri))
    res = CliRunner().invoke(pfp, ["studio"])
    assert res.exit_code == 0, res.output
    assert opened.get("uri", "").endswith("avatar/studio.html")
    assert "opened studio" in res.output


def test_push_twitter_disabled_by_default(monkeypatch, tmp_path):
    import cli.commands.pfp as pfpmod
    from core.instance import pfp_path
    p = pfp_path(tmp_path, "rob")
    p.parent.mkdir(parents=True)
    p.write_bytes(b"\x89PNG")
    monkeypatch.setattr(pfpmod, "_instance_home", lambda: (tmp_path, "rob"))
    res = CliRunner().invoke(pfp, ["push", "--twitter"], env={"PFP_PUSH_TWITTER": ""})
    assert res.exit_code == 0, res.output
    assert "twitter: disabled" in res.output
