"""081 WS-4.5: why every pack skill on prod logged "indexed as '<pack>:<id>' (id already taken)".

Prod (2026-10-15) logged it for all 10 pack skills after a restart, with no
same-id skill in the repo's builtin library. The two hypotheses in 081 were a
second scope for one pack, or a second pass over one index. Neither holds: the
index is built once per scan from a fresh dict, and a pack has one scope.

The cause is the deploy. The prod deploy synced `data/prompts` WITHOUT
`--delete`, so the pre-067 builtin copies of the 10 skills that moved into packs
(`7cbcdab49`, `131d99374`, `78a03ed7c`) are still on the box (the deploy fix is
pinned in the private ops tests). A builtin id is protected, so each pack skill
was renamed `<pack>:<id>`, the plain id served the STALE builtin body, and a cron
job that pins the plain id ran the old doctrine.
"""
import logging

from core.skills import scopes


def _pack(tmp_path, monkeypatch, builtin_root):
    pack_root = tmp_path / "pack" / "skills" / "x-engagement"
    pack_root.mkdir(parents=True)
    (pack_root / "SKILL.md").write_text(
        "---\nname: x-engagement\ndescription: the pack version\n---\n# X\nPACK BODY\n")
    scope = scopes.SkillScope("pack:x", tmp_path / "pack" / "skills", False, True)
    calls = []
    monkeypatch.setattr(scopes, "pack_scopes", lambda: calls.append(1) or [scope])
    monkeypatch.setattr(scopes, "_builtin_root", lambda: builtin_root)
    monkeypatch.setattr(scopes, "user_external_roots", lambda: [])
    monkeypatch.setattr(scopes, "trust_project_skills_effective", lambda: False)
    return calls


def test_one_scope_one_pass_keeps_the_plain_id(tmp_path, monkeypatch, caplog):
    builtin = tmp_path / "builtin"
    builtin.mkdir()
    calls = _pack(tmp_path, monkeypatch, builtin)
    with caplog.at_level(logging.INFO):
        first, second = scopes.external_index(), scopes.external_index()
    assert "x-engagement" in first and "x-engagement" in second
    assert "x:x-engagement" not in first
    assert "already taken" not in caplog.text
    assert calls == [1, 1], "one pack scope listing per scan, never a second pass"


def test_a_leftover_builtin_copy_renames_the_pack_skill(tmp_path, monkeypatch, caplog):
    """The prod symptom, reproduced: a stale builtin copy left by a no-delete sync."""
    builtin = tmp_path / "builtin"
    (builtin / "x-engagement").mkdir(parents=True)
    (builtin / "x-engagement" / "SKILL.md").write_text(
        "---\nname: x-engagement\ndescription: the pre-067 copy\n---\n# X\nSTALE BODY\n")
    _pack(tmp_path, monkeypatch, builtin)
    with caplog.at_level(logging.INFO):
        index = scopes.external_index()
    assert "x:x-engagement" in index and "x-engagement" not in index
    assert "indexed as 'x:x-engagement' (id already taken)" in caplog.text
    served = scopes.resolve("x-engagement", None, builtin_root=builtin, external=index)
    assert served.scope == "builtin", "the plain (pinnable) id serves the stale body"
