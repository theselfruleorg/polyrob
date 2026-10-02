"""The trusted top-level package overlay is not group-writable shared state."""
import os
from pathlib import Path

import pytest

from core.data_perms import (
    REASON_NOT_GROUP_WRITABLE,
    REASON_NO_SETGID,
    audit_data_perms,
)

pytestmark = pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX only")


def _group():
    import grp
    return grp.getgrgid(os.getgid()).gr_name


@pytest.fixture
def home(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    os.chmod(root, 0o770)
    return root


def test_top_level_installer_overlay_is_not_shared_state(home):
    overlay = home / "pylibs"
    (overlay / "package").mkdir(parents=True)
    module = overlay / "package" / "__init__.py"
    module.write_text("# installer-owned package\n")
    os.chmod(overlay, 0o755)
    os.chmod(overlay / "package", 0o755)
    os.chmod(module, 0o644)
    before = {p: p.stat().st_mode for p in (overlay, overlay / "package", module)}
    report = audit_data_perms(str(home), group=_group())
    assert report.scanned == 1
    assert all(not Path(o.path).is_relative_to(overlay) for o in report.offenders)
    assert before == {p: p.stat().st_mode for p in before}
    # Excluding the overlay must not relax the shared root's setgid rule.
    assert any(o.path == str(home) and REASON_NO_SETGID in o.reasons
               for o in report.offenders)


def test_overlay_does_not_consume_shared_state_walk_budget(home):
    overlay = home / "pylibs"
    overlay.mkdir()
    for i in range(20):
        (overlay / f"module_{i}.py").write_text("# code\n")
    shared = home / "state.db"
    shared.write_text("state")
    os.chmod(shared, 0o600)
    report = audit_data_perms(str(home), group=_group(), max_entries=2)
    assert report.scanned == 2
    assert not report.truncated
    assert any(o.path == str(shared) and REASON_NOT_GROUP_WRITABLE in o.reasons
               for o in report.offenders)


def test_nested_directory_named_pylibs_is_still_audited(home):
    nested = home / "sessions" / "pylibs"
    nested.mkdir(parents=True)
    module = nested / "state.db"
    module.write_text("state")
    os.chmod(nested, 0o700)
    os.chmod(module, 0o600)
    report = audit_data_perms(str(home), group=_group())
    for path in (nested, module):
        assert any(o.path == str(path) and REASON_NOT_GROUP_WRITABLE in o.reasons
                   for o in report.offenders)


def test_top_level_file_named_pylibs_is_still_audited(home):
    impostor = home / "pylibs"
    impostor.write_text("state")
    os.chmod(impostor, 0o600)
    report = audit_data_perms(str(home), group=_group())
    assert any(o.path == str(impostor) and REASON_NOT_GROUP_WRITABLE in o.reasons
               for o in report.offenders)


def test_remedy_uses_custody_aware_deploy_ownership_pass(home):
    report = audit_data_perms(str(home), group=_group())
    assert "deploy ownership pass" in report.remedy
    assert "scripts/deploy_when_idle.sh" in report.remedy
    assert "chmod -R" not in report.remedy
    assert "chgrp -R" not in report.remedy
