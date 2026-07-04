"""E10 (A6 gap 9) — normalize the skills REST read auth onto the SAME canonical
Depends() pattern the writes already use, instead of a bespoke getattr
duplicate. Behavior-preserving: anonymous reads still see the base catalog
only (no 401) — this closes an inconsistency, not a leak.
"""
import inspect

import api.skill_endpoints as se


class _FakeState:
    pass


class _AuthedRequest:
    def __init__(self, user_id):
        self.state = _FakeState()
        self.state.user_id = user_id


class _AnonRequest:
    def __init__(self):
        self.state = _FakeState()


def test_get_current_user_optional_returns_id_when_authenticated():
    assert se.get_current_user_optional(_AuthedRequest("tenant-a")) == "tenant-a"


def test_get_current_user_optional_returns_none_when_anonymous():
    assert se.get_current_user_optional(_AnonRequest()) is None


def test_list_skills_uses_canonical_optional_dependency():
    sig = inspect.signature(se.list_skills)
    default = sig.parameters["user_id"].default
    assert getattr(default, "dependency", None) is se.get_current_user_optional


def test_get_skill_uses_canonical_optional_dependency():
    sig = inspect.signature(se.get_skill)
    default = sig.parameters["user_id"].default
    assert getattr(default, "dependency", None) is se.get_current_user_optional
