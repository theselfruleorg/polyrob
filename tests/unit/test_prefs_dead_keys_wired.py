"""P0.2 (proposal 018): the four historically-DEAD pref keys actually enforce.

`goals.notify_on_done`, `autonomy.self_wake`, `autonomy.background_review` and
`outbound.max_new_recipients_per_day` were settable/persisted/displayed but
their enforcement sites read the env flag directly — the classic write-only
trap. Each now routes through an ``effective_*`` helper beside its consumer
(house pattern: ``effective_goal_quota`` / ``effective_daily_cap``), with the
schema's merge semantics: override for notify, tighten-only ``and`` for the
autonomy loops, ``min`` for the seeding cap.
"""
import pytest

from core.prefs import write_preference


@pytest.fixture()
def home(tmp_path):
    return tmp_path


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("GOAL_NOTIFY_ON_DONE", "SELF_WAKE_ENABLED",
                "BACKGROUND_REVIEW_ENABLED", "CORRESPONDENT_MAX_NEW_PER_DAY",
                "POLYROB_LOCAL", "ROB_LOCAL", "AUTONOMY_POSTURE"):
        monkeypatch.delenv(var, raising=False)


# --- goals.notify_on_done (override merge) ---------------------------------

def test_notify_on_done_pref_disables(home):
    from agents.task.goals.dispatcher import effective_goal_notify_on_done
    assert effective_goal_notify_on_done("u1", home) is True  # env default ON
    ok, err = write_preference(home, "u1", "goals.notify_on_done", False)
    assert ok, err
    assert effective_goal_notify_on_done("u1", home) is False


def test_notify_on_done_pref_can_reenable_when_env_off(home, monkeypatch):
    from agents.task.goals.dispatcher import effective_goal_notify_on_done
    monkeypatch.setenv("GOAL_NOTIFY_ON_DONE", "off")
    write_preference(home, "u1", "goals.notify_on_done", True)
    # override merge: pref beats env in BOTH directions (safe key).
    assert effective_goal_notify_on_done("u1", home) is True


# --- autonomy.self_wake ("and" merge — pref can only disable) ---------------

def test_self_wake_pref_disables_but_never_enables(home, monkeypatch):
    from agents.task.agent.core.self_wake import effective_self_wake_enabled
    monkeypatch.setenv("SELF_WAKE_ENABLED", "1")
    assert effective_self_wake_enabled("u1", home) is True
    write_preference(home, "u1", "autonomy.self_wake", False)
    assert effective_self_wake_enabled("u1", home) is False
    # env OFF + pref True must stay OFF (tighten-only).
    monkeypatch.setenv("SELF_WAKE_ENABLED", "0")
    write_preference(home, "u1", "autonomy.self_wake", True)
    assert effective_self_wake_enabled("u1", home) is False


# --- autonomy.background_review ("and" merge) -------------------------------

def test_background_review_pref_disables_but_never_enables(home, monkeypatch):
    from agents.task.agent.core.background_review import (
        effective_background_review_enabled,
    )
    monkeypatch.setenv("BACKGROUND_REVIEW_ENABLED", "1")
    assert effective_background_review_enabled("u1", home) is True
    write_preference(home, "u1", "autonomy.background_review", False)
    assert effective_background_review_enabled("u1", home) is False
    monkeypatch.setenv("BACKGROUND_REVIEW_ENABLED", "0")
    write_preference(home, "u1", "autonomy.background_review", True)
    assert effective_background_review_enabled("u1", home) is False


def test_bg_review_should_fire_respects_pref(home, monkeypatch):
    # Behavioral wiring proof: the mixin's decision consults the pref.
    from agents.task.agent.core.background_review import BackgroundReviewMixin

    monkeypatch.setenv("BACKGROUND_REVIEW_ENABLED", "1")
    monkeypatch.setenv("SKILLS_WRITABLE", "1")
    monkeypatch.setenv("BG_REVIEW_INTERVAL", "1")

    class _A(BackgroundReviewMixin):
        _is_sub_agent = False
        session_id = None
        user_id = "u1"

    a = _A()
    write_preference(home, "u1", "autonomy.background_review", False)
    monkeypatch.setattr(
        "agents.task.agent.core.background_review._prefs_home_dir",
        lambda: str(home))
    assert a._bg_review_should_fire(turn_was_productive=True) is False


# --- outbound.max_new_recipients_per_day (min merge, guarded) ---------------

def test_max_new_per_day_pref_tightens_only(home):
    from core.surfaces.seed import effective_max_new_per_day
    assert effective_max_new_per_day("u1", home) == 20  # env default
    write_preference(home, "u1", "outbound.max_new_recipients_per_day", 3)
    assert effective_max_new_per_day("u1", home) == 3
    # A pref above the operator ceiling is clamped to the ceiling.
    write_preference(home, "u1", "outbound.max_new_recipients_per_day", 50)
    assert effective_max_new_per_day("u1", home) == 20


def test_max_new_per_day_env_zero_is_a_real_ceiling(home, monkeypatch):
    # cap=0 means "no new correspondents" — a hard block, NOT a disabled
    # sentinel (min_value=0 in the spec), so min-merge keeps it.
    from core.surfaces.seed import effective_max_new_per_day
    monkeypatch.setenv("CORRESPONDENT_MAX_NEW_PER_DAY", "0")
    write_preference(home, "u1", "outbound.max_new_recipients_per_day", 5)
    assert effective_max_new_per_day("u1", home) == 0
