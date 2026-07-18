"""Tests for `cli.commands.doctor.setup_lines` — onboarding-completeness view
(avatar, surfaces, SOUL/identity docs). Pure over an env dict; never raises."""
from cli.commands.doctor import setup_lines


def test_avatar_line_not_generated(tmp_path):
    lines = setup_lines({"POLYROB_DATA_DIR": str(tmp_path)})
    joined = "\n".join(lines)
    assert "avatar: not generated" in joined
    assert "pfp generate" in joined


def test_avatar_line_generated(tmp_path):
    from core.instance import pfp_dir
    d = pfp_dir(tmp_path, "rob")
    d.mkdir(parents=True)
    (d / "pfp.png").write_bytes(b"png")
    lines = setup_lines({"POLYROB_DATA_DIR": str(tmp_path), "POLYROB_INSTANCE_ID": "rob"})
    assert any("avatar: generated" in l for l in lines)


def test_surfaces_none_configured(tmp_path):
    lines = setup_lines({"POLYROB_DATA_DIR": str(tmp_path)})
    assert any("surfaces: none configured" in l for l in lines)


def test_surfaces_telegram_token_only_shows_gateway_qualifier(tmp_path):
    # Token present but TELEGRAM_SURFACE_ENABLED not set/truthy: `polyrob telegram`
    # (standalone) would still work (it defaults the flag on itself), but
    # `polyrob gateway` would NOT start the surface without the flag — say so.
    lines = setup_lines({"POLYROB_DATA_DIR": str(tmp_path), "TELEGRAM_BOT_TOKEN": "t"})
    surfaces_line = next(l for l in lines if l.startswith("surfaces:"))
    assert "telegram (token only" in surfaces_line
    assert "TELEGRAM_SURFACE_ENABLED" in surfaces_line


def test_surfaces_discord_enabled_and_token_shows_plain_name(tmp_path):
    lines = setup_lines({
        "POLYROB_DATA_DIR": str(tmp_path),
        "DISCORD_SURFACE_ENABLED": "true",
        "DISCORD_BOT_TOKEN": "t",
    })
    surfaces_line = next(l for l in lines if l.startswith("surfaces:"))
    assert "discord" in surfaces_line
    assert "discord (" not in surfaces_line  # plain name, no qualifier


def test_surfaces_discord_falsey_denylist_arbitrary_truthy_value(tmp_path):
    # bool_env/parse_bool are a falsey-DENYlist (anything not in _FALSEY is truthy),
    # not an allow-list — DISCORD_SURFACE_ENABLED=enabled actually starts the surface
    # at runtime (agents.task.surface_config -> core.env.bool_env), so doctor must
    # report it as plain "discord", not a stale "token only" qualifier.
    lines = setup_lines({
        "POLYROB_DATA_DIR": str(tmp_path),
        "DISCORD_SURFACE_ENABLED": "enabled",
        "DISCORD_BOT_TOKEN": "t",
    })
    surfaces_line = next(l for l in lines if l.startswith("surfaces:"))
    assert "discord" in surfaces_line
    assert "discord (" not in surfaces_line  # plain name, no qualifier


def test_surfaces_slack_enabled_without_token_flags_missing_token(tmp_path):
    lines = setup_lines({"POLYROB_DATA_DIR": str(tmp_path), "SLACK_SURFACE_ENABLED": "true"})
    surfaces_line = next(l for l in lines if l.startswith("surfaces:"))
    assert "slack (enabled, token missing)" in surfaces_line


def test_surfaces_flag_only_surfaces_unchanged(tmp_path):
    lines = setup_lines({"POLYROB_DATA_DIR": str(tmp_path), "EMAIL_SURFACE_ENABLED": "true"})
    surfaces_line = next(l for l in lines if l.startswith("surfaces:"))
    assert "email" in surfaces_line


def test_soul_default_vs_authored(tmp_path):
    lines = setup_lines({"POLYROB_DATA_DIR": str(tmp_path)})
    assert any("identity docs: default" in l for l in lines)
    (tmp_path / "identity").mkdir(parents=True)
    (tmp_path / "identity" / "identity.md").write_text("# I am Rob")
    lines = setup_lines({"POLYROB_DATA_DIR": str(tmp_path)})
    assert any("identity docs: authored" in l for l in lines)
    assert any("identity.md" in l for l in lines if l.startswith("identity docs:"))


def test_soul_authored_names_only_present_docs(tmp_path):
    # Only operating.md exists — the line must name it, not hardcode identity.md.
    (tmp_path / "identity").mkdir(parents=True)
    (tmp_path / "identity" / "operating.md").write_text("# how I operate")
    lines = setup_lines({"POLYROB_DATA_DIR": str(tmp_path)})
    doc_line = next(l for l in lines if l.startswith("identity docs:"))
    assert "operating.md" in doc_line
    assert "identity.md" not in doc_line
