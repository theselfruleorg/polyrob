"""F-collabland (live-test): ToolStatus.DISABLED must exist.

collabland_tool.py set self._status = ToolStatus.DISABLED when its API key is
missing, but the enum had no DISABLED member → AttributeError on every boot
('type object ToolStatus has no attribute DISABLED'). 'disabled' is a legit
state distinct from FAILED (intentionally off vs broken).
"""
from tools.base_tool import ToolStatus


def test_disabled_member_exists():
    assert ToolStatus.DISABLED.value == "disabled"


def test_disabled_distinct_from_failed():
    assert ToolStatus.DISABLED is not ToolStatus.FAILED


def test_collabland_disabled_assignment_does_not_raise():
    # The exact pattern collabland_tool.py:165 used — must not AttributeError.
    status = ToolStatus.DISABLED
    assert status in ToolStatus
