"""`polyrob pfp` command group: show (live render) + generate/randomize + pick.

Avatar creation is optional/deferrable, so `pfp show` must work with no stored
avatar (renders from a seed). `generate` mints a RANDOM identity by default;
`randomize` re-rolls all/face/voice; the pick flow shuffles FACE and VOICE
independently and freezes into the instance identity home.
"""
import json

from click.testing import CliRunner

from cli.commands.pfp import (
    pfp,
    default_config,
    random_config,
    shuffle_face,
    shuffle_voice,
    save_config,
)
from modules.pfp.config import load_frozen_config


def test_default_config_is_valid():
    cfg = default_config("Ada Nine")
    assert load_frozen_config(cfg)["seed"] == "Ada Nine"
    assert cfg["variant"] == "" and cfg["override"] == {}


def test_random_config_mints_a_fresh_variant():
    a, b = random_config("Ada Nine"), random_config("Ada Nine")
    assert a["variant"].startswith("#") and b["variant"].startswith("#")
    assert a["variant"] != b["variant"]          # actually random
    assert load_frozen_config(a)["seed"] == "Ada Nine"


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


def test_generate_invokes_store_with_a_random_identity(monkeypatch):
    import cli.commands.pfp as pfpmod

    monkeypatch.setattr(pfpmod, "_instance_home", lambda: ("/tmp/home", "rob"))
    seen = {}

    def fake_generate(home, instance_id, **kw):
        assert (home, instance_id) == ("/tmp/home", "rob")
        seen.update(kw)
        return {"rendered_by": "playwright-chromium"}

    import modules.pfp.store as store
    monkeypatch.setattr(store, "generate_pfp", fake_generate)
    res = CliRunner().invoke(pfp, ["generate"])
    assert res.exit_code == 0, res.output
    assert "generated" in res.output and "playwright-chromium" in res.output
    assert seen["config"]["variant"].startswith("#")     # random by default
    assert "voice:" in res.output and "next:" in res.output   # identity + guidance


def test_generate_stock_uses_the_committed_identity(monkeypatch):
    import cli.commands.pfp as pfpmod

    monkeypatch.setattr(pfpmod, "_instance_home", lambda: ("/tmp/home", "rob"))
    seen = {}

    def fake_generate(home, instance_id, **kw):
        seen.update(kw)
        return {"rendered_by": "playwright-chromium"}

    import modules.pfp.store as store
    monkeypatch.setattr(store, "generate_pfp", fake_generate)
    res = CliRunner().invoke(pfp, ["generate", "--stock"])
    assert res.exit_code == 0, res.output
    assert seen["config"]["variant"] == ""               # the stock face, not a roll
    assert seen["config"]["seed"] == "Rob Ottmachin"


def test_generate_variant_pins_the_roll(monkeypatch):
    import cli.commands.pfp as pfpmod

    monkeypatch.setattr(pfpmod, "_instance_home", lambda: ("/tmp/home", "rob"))
    seen = {}

    def fake_generate(home, instance_id, **kw):
        seen.update(kw)
        return {"rendered_by": "playwright-chromium"}

    import modules.pfp.store as store
    monkeypatch.setattr(store, "generate_pfp", fake_generate)
    res = CliRunner().invoke(pfp, ["generate", "--seed", "Ada Nine", "--variant", "#pin"])
    assert res.exit_code == 0, res.output
    assert seen["config"]["seed"] == "Ada Nine"
    assert seen["config"]["variant"] == "#pin"


def test_randomize_rerolls_face_and_voice(monkeypatch, tmp_path):
    import cli.commands.pfp as pfpmod

    monkeypatch.setattr(pfpmod, "_instance_home", lambda: (tmp_path, "rob"))
    seen = {}

    def fake_generate(home, instance_id, **kw):
        seen.update(kw)
        return {"rendered_by": "pillow-mesh"}

    import modules.pfp.store as store
    monkeypatch.setattr(store, "generate_pfp", fake_generate)
    res = CliRunner().invoke(pfp, ["randomize"])
    assert res.exit_code == 0, res.output
    assert seen["force"] is True
    assert seen["config"]["variant"].startswith("#")     # fresh roll
    assert "voice" not in seen["config"]["override"]     # voice follows the new variant


def test_randomize_voice_only_keeps_the_face(monkeypatch, tmp_path):
    import cli.commands.pfp as pfpmod
    from modules.pfp import store as real_store
    from core.instance import pfp_dir

    # a stored identity to start from
    meta_dir = pfp_dir(tmp_path, "rob")
    meta_dir.mkdir(parents=True)
    (meta_dir / "pfp.json").write_text(json.dumps(
        {"generator": "mindprint@v2", "seed": "Ada Nine", "variant": "#keep",
         "size": 256, "override": {}, "locked": False}), encoding="utf-8")

    monkeypatch.setattr(pfpmod, "_instance_home", lambda: (tmp_path, "rob"))
    seen = {}

    def fake_generate(home, instance_id, **kw):
        seen.update(kw)
        return {"rendered_by": "pillow-mesh"}

    monkeypatch.setattr(real_store, "generate_pfp", fake_generate)
    res = CliRunner().invoke(pfp, ["randomize", "voice"])
    assert res.exit_code == 0, res.output
    assert seen["config"]["variant"] == "#keep"          # face untouched
    assert "voice" in seen["config"]["override"]         # voice re-rolled


def test_push_errors_without_a_stored_avatar(monkeypatch, tmp_path):
    import cli.commands.pfp as pfpmod
    monkeypatch.setattr(pfpmod, "_instance_home", lambda: (tmp_path, "rob"))
    res = CliRunner().invoke(pfp, ["push", "--twitter"])
    assert res.exit_code != 0
    assert "no avatar yet" in res.output


def test_pick_loop_shuffles_and_freezes_into_the_instance_home(monkeypatch, tmp_path):
    import cli.commands.pfp as pfpmod
    from core.instance import load_pfp_meta, pfp_path

    monkeypatch.setattr(pfpmod, "_instance_home", lambda: (tmp_path, "rob"))
    keys = iter(["s", "v", "\r"])           # shuffle face -> shuffle voice -> save
    monkeypatch.setattr("click.getchar", lambda: next(keys))
    res = CliRunner().invoke(pfp, ["pick", "--seed", "Ada Nine"], env={"COLORTERM": ""})
    assert res.exit_code == 0, res.output
    meta = load_pfp_meta(tmp_path, "rob")           # frozen where consumers read it
    assert meta and meta["variant"]                 # face was shuffled
    assert "voice" in meta["override"]              # voice was shuffled independently
    assert pfp_path(tmp_path, "rob").is_file()      # png rendered too (no browser needed)


def test_pick_out_exports_the_config_json(monkeypatch, tmp_path):
    import cli.commands.pfp as pfpmod

    monkeypatch.setattr(pfpmod, "_instance_home", lambda: (tmp_path, "rob"))
    keys = iter(["\r"])                     # save the opening random roll
    monkeypatch.setattr("click.getchar", lambda: next(keys))
    target = tmp_path / "export" / "me.json"
    res = CliRunner().invoke(pfp, ["pick", "--seed", "Ada Nine", "--out", str(target)],
                             env={"COLORTERM": ""})
    assert res.exit_code == 0, res.output
    assert load_frozen_config(target)["seed"] == "Ada Nine"
    assert load_frozen_config(target)["variant"]    # pick starts from a random roll


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


# ---- one-time setup contract at the CLI ----

def _seed_meta(tmp_path, *, locked, variant="#keep"):
    from core.instance import pfp_dir, pfp_path
    d = pfp_dir(tmp_path, "rob")
    d.mkdir(parents=True, exist_ok=True)
    (d / "pfp.json").write_text(json.dumps(
        {"generator": "mindprint@v2", "seed": "Ada Nine", "variant": variant,
         "size": 256, "override": {}, "locked": locked}), encoding="utf-8")
    pfp_path(tmp_path, "rob").write_bytes(b"\x89PNG\r\n\x1a\npx")


def test_randomize_refuses_a_kept_identity(monkeypatch, tmp_path):
    import cli.commands.pfp as pfpmod
    monkeypatch.setattr(pfpmod, "_instance_home", lambda: (tmp_path, "rob"))
    _seed_meta(tmp_path, locked=True)
    res = CliRunner().invoke(pfp, ["randomize"])
    assert res.exit_code != 0
    assert "kept" in res.output.lower()


def test_generate_refuses_identity_change_on_kept(monkeypatch, tmp_path):
    import cli.commands.pfp as pfpmod
    monkeypatch.setattr(pfpmod, "_instance_home", lambda: (tmp_path, "rob"))
    _seed_meta(tmp_path, locked=True)
    res = CliRunner().invoke(pfp, ["generate", "--seed", "Someone Else"])
    assert res.exit_code != 0
    assert "kept" in res.output.lower()


def test_keep_locks_the_draft(monkeypatch, tmp_path):
    import cli.commands.pfp as pfpmod
    from core.instance import load_pfp_meta
    monkeypatch.setattr(pfpmod, "_instance_home", lambda: (tmp_path, "rob"))
    _seed_meta(tmp_path, locked=False)
    res = CliRunner().invoke(pfp, ["keep"])
    assert res.exit_code == 0, res.output
    assert "permanently" in res.output.lower()
    assert load_pfp_meta(tmp_path, "rob")["locked"] is True


def test_push_refuses_a_draft(monkeypatch, tmp_path):
    import cli.commands.pfp as pfpmod
    monkeypatch.setattr(pfpmod, "_instance_home", lambda: (tmp_path, "rob"))
    _seed_meta(tmp_path, locked=False)
    res = CliRunner().invoke(pfp, ["push", "--twitter"])
    assert res.exit_code != 0
    assert "draft" in res.output.lower() and "keep" in res.output.lower()


def test_pick_refuses_a_kept_identity(monkeypatch, tmp_path):
    import cli.commands.pfp as pfpmod
    monkeypatch.setattr(pfpmod, "_instance_home", lambda: (tmp_path, "rob"))
    _seed_meta(tmp_path, locked=True)
    res = CliRunner().invoke(pfp, ["pick"], env={"COLORTERM": ""})
    assert res.exit_code != 0
    assert "kept" in res.output.lower()


def test_pick_save_keeps_permanently(monkeypatch, tmp_path):
    import cli.commands.pfp as pfpmod
    from core.instance import load_pfp_meta
    monkeypatch.setattr(pfpmod, "_instance_home", lambda: (tmp_path, "rob"))
    keys = iter(["s", "\r"])
    monkeypatch.setattr("click.getchar", lambda: next(keys))
    res = CliRunner().invoke(pfp, ["pick", "--seed", "Ada Nine"], env={"COLORTERM": ""})
    assert res.exit_code == 0, res.output
    meta = load_pfp_meta(tmp_path, "rob")
    assert meta["locked"] is True                       # pick's save IS the acceptance


def test_say_speaks_the_stored_voice(monkeypatch, tmp_path):
    import cli.commands.pfp as pfpmod
    import modules.pfp.voice as voicemod
    monkeypatch.setattr(pfpmod, "_instance_home", lambda: (tmp_path, "rob"))
    _seed_meta(tmp_path, locked=True)
    from core.instance import pfp_dir
    meta_p = pfp_dir(tmp_path, "rob") / "pfp.json"
    meta = json.loads(meta_p.read_text())
    meta["voice"] = {"pitch": 1.29, "rate": 1.02, "timbre": 0.78}
    meta_p.write_text(json.dumps(meta))

    spoken = {}

    def fake_speak(voice, text, **kw):
        spoken["voice"], spoken["text"] = voice, text
        return "say (Samantha)"
    monkeypatch.setattr(voicemod, "speak_voice", fake_speak)
    res = CliRunner().invoke(pfp, ["say", "hello there"])
    assert res.exit_code == 0, res.output
    assert spoken["voice"]["pitch"] == 1.29               # the STORED signature
    assert spoken["text"] == "hello there"
    assert "spoken via say" in res.output


def test_say_without_avatar_previews_the_default_seed(monkeypatch, tmp_path):
    import cli.commands.pfp as pfpmod
    import modules.pfp.voice as voicemod
    monkeypatch.setattr(pfpmod, "_instance_home", lambda: (tmp_path, "rob"))
    spoken = {}
    monkeypatch.setattr(voicemod, "speak_voice",
                        lambda voice, text, **kw: spoken.setdefault("v", voice) and "say" or "say")
    res = CliRunner().invoke(pfp, ["say"])
    assert res.exit_code == 0, res.output                 # deferrable, like `show`
    assert set(spoken["v"]) == {"pitch", "rate", "timbre"}


def test_say_reports_when_no_engine(monkeypatch, tmp_path):
    import cli.commands.pfp as pfpmod
    import modules.pfp.voice as voicemod
    monkeypatch.setattr(pfpmod, "_instance_home", lambda: (tmp_path, "rob"))

    def raise_unavailable(voice, text, **kw):
        raise voicemod.VoiceUnavailable("no native TTS engine found — use the webview")
    monkeypatch.setattr(voicemod, "speak_voice", raise_unavailable)
    res = CliRunner().invoke(pfp, ["say"])
    assert res.exit_code != 0
    assert "webview" in res.output.lower() or "tts" in res.output.lower()
