"""WS-G (057 R6): the shared-data-home permission audit.

Every case here is a real filesystem tree — the module's whole job is to read
modes and gids honestly, so a mocked stat would test nothing.
"""
import os
import stat

import pytest

from core.data_perms import (
    DEFAULT_DATA_GROUP,
    REASON_NOT_GROUP_WRITABLE,
    REASON_NO_SETGID,
    REASON_ROOT_OWNED,
    REASON_WRONG_GROUP,
    audit_data_perms,
    render_perms_lines,
)

pytestmark = pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX only")


def _own_group() -> str:
    import grp
    return grp.getgrgid(os.getgid()).gr_name


@pytest.fixture
def home(tmp_path):
    """A data home in a SUBDIR of tmp_path — the suite's autouse fixtures drop
    verdicts.db / telemetry_events.db straight into tmp_path itself."""
    h = tmp_path / "data"
    h.mkdir()
    return h


def _clean_tree(root):
    """A data home that satisfies every rule for the caller's own group."""
    d = root / "auto"
    d.mkdir()
    os.chmod(root, 0o2770)
    os.chmod(d, 0o2770)
    f = d / "session.db"
    f.write_text("x")
    os.chmod(f, 0o660)
    return root


def test_missing_data_home_is_skipped_not_ok(tmp_path):
    r = audit_data_perms(str(tmp_path / "nope"), group=_own_group())
    assert r.skipped and not r.ok
    assert "no data home" in r.skip_reason


def test_unknown_group_skips_gracefully(home):
    _clean_tree(home)
    r = audit_data_perms(str(home), group="polyrob-data-does-not-exist")
    assert r.skipped and not r.group_exists and not r.ok
    assert "does not exist" in r.skip_reason
    # a skipped audit must never render as a pass
    assert "OK" not in render_perms_lines(r)[0]


def test_clean_tree_is_ok(home):
    _clean_tree(home)
    r = audit_data_perms(str(home), group=_own_group())
    assert not r.skipped, r.skip_reason
    assert r.ok, [o.render() for o in r.offenders]
    assert r.scanned >= 3
    assert render_perms_lines(r)[0].startswith("data perms: OK")


def test_group_unwritable_file_is_named(home):
    _clean_tree(home)
    bad = home / "auto" / "root_written.db"
    bad.write_text("x")
    os.chmod(bad, 0o644)
    r = audit_data_perms(str(home), group=_own_group())
    assert not r.ok
    assert r.counts[REASON_NOT_GROUP_WRITABLE] == 1
    assert any(o.path.endswith("root_written.db")
               and REASON_NOT_GROUP_WRITABLE in o.reasons for o in r.offenders)
    assert "remedy:" in "\n".join(render_perms_lines(r))


def test_dir_without_setgid_is_named(home):
    _clean_tree(home)
    d = home / "locks"
    d.mkdir()
    os.chmod(d, 0o770)  # group-writable, NOT setgid
    r = audit_data_perms(str(home), group=_own_group())
    assert r.counts[REASON_NO_SETGID] == 1
    assert not r.counts[REASON_NOT_GROUP_WRITABLE]


def test_wallet_dir_is_exempt_from_group_write(home):
    """The deploy pins wallet/ to 750 on purpose — only the agent signs."""
    _clean_tree(home)
    w = home / "wallet"
    w.mkdir()
    os.chmod(w, 0o750)
    key = w / "submissions.sqlite"
    key.write_text("x")
    os.chmod(key, 0o640)
    r = audit_data_perms(str(home), group=_own_group())
    assert r.ok, [o.render() for o in r.offenders]


def test_wrong_group_is_named(home):
    _clean_tree(home)
    r = audit_data_perms(str(home), group=_own_group())
    assert r.ok
    # judge the same tree against a DIFFERENT existing group: every entry is wrong
    import grp
    others = [g.gr_name for g in grp.getgrall()
              if g.gr_gid != os.getgid() and g.gr_gid >= 0]
    if not others:
        pytest.skip("no second group on this host")
    r2 = audit_data_perms(str(home), group=others[0])
    if r2.skipped:
        pytest.skip(r2.skip_reason)
    assert r2.counts[REASON_WRONG_GROUP] >= 1
    assert any(REASON_WRONG_GROUP in o.reasons for o in r2.offenders)


def test_symlink_is_never_judged(home):
    _clean_tree(home)
    link = home / "auto" / "link"
    link.symlink_to(home / "auto" / "session.db")
    r = audit_data_perms(str(home), group=_own_group())
    assert r.ok, [o.render() for o in r.offenders]


def test_entry_budget_truncates_and_says_so(home):
    _clean_tree(home)
    for i in range(20):
        f = home / "auto" / f"f{i}"
        f.write_text("x")
        os.chmod(f, 0o660)
    r = audit_data_perms(str(home), group=_own_group(), max_entries=5)
    assert r.truncated and r.scanned <= 5
    assert "budget" in "\n".join(render_perms_lines(r))


def test_offender_list_is_capped_but_total_is_not(home):
    _clean_tree(home)
    for i in range(9):
        f = home / "auto" / f"bad{i}"
        f.write_text("x")
        os.chmod(f, 0o600)
    r = audit_data_perms(str(home), group=_own_group(), max_offenders=3)
    assert r.offender_total == 9 and len(r.offenders) == 3
    assert "and 6 more" in "\n".join(render_perms_lines(r))


def test_audit_never_mutates(home):
    _clean_tree(home)
    before = {p: os.lstat(p).st_mode
              for p in (str(home), str(home / "auto"),
                        str(home / "auto" / "session.db"))}
    audit_data_perms(str(home), group=_own_group())
    for p, m in before.items():
        assert os.lstat(p).st_mode == m
    assert not (home / "wallet").exists()


def test_root_sensitive_dirs_are_the_documented_three():
    from core.data_perms import ROOT_SENSITIVE_DIRS
    assert set(ROOT_SENSITIVE_DIRS) == {"auto", "wallet", "locks"}
    assert DEFAULT_DATA_GROUP == "polyrob-data"
    assert REASON_ROOT_OWNED == "root_owned"
    assert stat.S_ISGID  # sanity: the flag the dir rule reads
