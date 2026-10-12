"""Repo-root collection anchors.

``collect_ignore`` paths are relative to THIS file, so they name the repo-root
``scripts/`` only. (pytest.ini ``norecursedirs`` matches a directory BASENAME
anywhere, which is how ``scripts`` there once also dropped every test under
``tests/unit/scripts/`` from CI.) The root ``scripts/test_*.py`` files are
live-key runners, not unit tests; a tree without ``scripts/`` ignores nothing.
"""

collect_ignore = ["scripts"]


# --- D7 (2026-10-10): posture members are set only through POLYROB_OVERRIDES ---
#
# A posture member (core.config_policy.members.MEMBER_FLAGS — KB_ENABLED,
# GOALS_ENABLED, SHELL_TOOLS_ENABLED, …) is no longer an env setting of its own:
# the runtime IGNORES a direct `KB_ENABLED=true`. Several hundred tests set a
# member with `monkeypatch.setenv("KB_ENABLED", "1")`; this ONE shared fixture
# routes such a call into the POLYROB_OVERRIDES line instead (and `delenv` of a
# member removes its override entry), so those tests keep saying what they mean.
# A test that must prove a DIRECT member line is ignored writes it with
# `monkeypatch.setitem(os.environ, NAME, value)`, which this shim does not touch.
import os as _os

import pytest as _pytest
from _pytest.monkeypatch import MonkeyPatch as _MonkeyPatch


class _MemberAwareMonkeyPatch(_MonkeyPatch):
    @staticmethod
    def _member(name) -> bool:
        from core.config_policy.members import MEMBER_FLAGS
        return str(name) in MEMBER_FLAGS

    def _set_override(self, name, value) -> None:
        from core.config_policy.members import OVERRIDES_ENV, with_override
        raw = with_override(_os.environ.get(OVERRIDES_ENV), str(name), value)
        if raw:
            super().setenv(OVERRIDES_ENV, raw)
        else:
            super().delenv(OVERRIDES_ENV, raising=False)

    def setenv(self, name, value, prepend=None):
        if self._member(name):
            value = str(value)
            self._set_override(name, value if value.strip() else None)
            return
        super().setenv(name, value, prepend)

    def delenv(self, name, raising=True):
        if self._member(name):
            self._set_override(name, None)
            super().delenv(name, raising=False)
            return
        super().delenv(name, raising)


@_pytest.fixture
def monkeypatch():
    """pytest's ``monkeypatch``, member-aware (see the block comment above)."""
    mp = _MemberAwareMonkeyPatch()
    yield mp
    mp.undo()
