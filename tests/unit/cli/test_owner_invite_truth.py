"""`polyrob owner invite` reports the stored binding, and `--rebind` moves a contact.

The seed is idempotent and keeps an existing row, so the verb used to print its own
SESSION_ID argument over a binding that had never moved.
"""
import os

import click
import pytest
from click.testing import CliRunner

from cli.commands import owner_correspondents as oc
from cli.commands.owner import owner
from core.surfaces.conversations import ConversationStore
from core.surfaces.correspondents import CorrespondentRegistry

TENANT = "u_owner"


@pytest.fixture
def reg(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")
    monkeypatch.setenv("CORRESPONDENT_REQUIRE_APPROVAL", "false")
    return CorrespondentRegistry(os.path.join(str(tmp_path), "correspondents.db"))


@pytest.fixture
def moves(monkeypatch):
    seen = []
    monkeypatch.setattr("core.surfaces.seed.record_contact_rebound",
                        lambda *args: seen.append(args))
    return seen


def _invite(*args):
    return CliRunner().invoke(owner, ["invite", "discord", "1234", *args, "--user", TENANT])


def _row(reg):
    return reg.binding(surface="discord", address="1234", user_id=TENANT)


# ── Step 1: the verb says what is stored ─────────────────────────────────────────

def test_a_new_invite_prints_the_stored_row(reg):
    r = _invite("sess_a")
    assert r.exit_code == 0, r.output
    assert "-> session sess_a: active" in r.output


def test_the_same_session_again_is_idempotent(reg):
    assert _invite("sess_a").exit_code == 0
    r = _invite("sess_a")
    assert r.exit_code == 0, r.output
    assert "-> session sess_a: active" in r.output


def test_another_session_is_refused_and_names_the_real_one(reg):
    assert _invite("sess_old").exit_code == 0
    r = _invite("sess_new")
    assert r.exit_code == 1
    assert "already bound to session sess_old" in r.output and "--rebind" in r.output
    assert "-> session sess_new" not in r.output
    assert _row(reg)["session_id"] == "sess_old"


def test_a_pending_row_is_refused_the_same_way(reg, monkeypatch):
    monkeypatch.setenv("CORRESPONDENT_REQUIRE_APPROVAL", "true")
    assert "pending" in _invite("sess_old").output
    r = _invite("sess_new")
    assert r.exit_code == 1 and "already bound to session sess_old (pending)" in r.output


@pytest.mark.parametrize("session", ["sess_a", "sess_b"])
def test_an_expired_row_exits_1_and_changes_nothing(reg, session):
    assert _invite("sess_a").exit_code == 0
    assert reg.deactivate(surface="discord", address="1234", user_id=TENANT)
    r = _invite(session)
    assert r.exit_code == 1 and "rejected or revoked" in r.output
    assert _row(reg)["state"] == "expired" and _row(reg)["session_id"] == "sess_a"


@pytest.mark.parametrize("state,why", sorted(oc._NOT_BOUND.items()))
def test_a_seed_that_bound_nothing_exits_1(reg, monkeypatch, state, why):
    monkeypatch.setattr("core.surfaces.seed.maybe_seed_correspondent", lambda *a, **k: state)
    r = _invite("sess_a")
    assert r.exit_code == 1 and why in r.output


@pytest.mark.parametrize("bad", ["../../etc", "a b", "x;y", "a" * 51, "67", "executor_abc12345"])
def test_a_value_that_cannot_name_a_session_is_refused(reg, bad):
    r = _invite(bad)
    assert r.exit_code == 1 and "not a session id" in r.output
    assert _row(reg) is None


def test_an_unknown_session_warns_and_still_binds(reg, tmp_path):
    (tmp_path / "sessions").mkdir()
    r = _invite("no_such_session")
    assert r.exit_code == 0, r.output
    assert "warning: no session no_such_session" in r.output
    assert _row(reg)["session_id"] == "no_such_session"


def test_a_known_session_has_no_warning(reg, tmp_path):
    (tmp_path / "sessions" / TENANT / "sess_known").mkdir(parents=True)
    r = _invite("sess_known")
    assert r.exit_code == 0 and "warning" not in r.output


def test_a_session_tree_that_cannot_be_read_gives_no_warning(reg, monkeypatch):
    def unreadable(*_a):
        raise PermissionError("denied")

    monkeypatch.setattr("cli.session_paths.session_directory", unreadable)
    monkeypatch.setattr("cli.commands.session._data_root",
                        lambda: type("R", (), {"is_dir": staticmethod(lambda: True)})())
    assert "warning" not in _invite("sess_a").output


def test_only_an_exact_session_counts_as_known(reg, tmp_path):
    (tmp_path / "sessions" / TENANT / "sess_abc").mkdir(parents=True)
    assert "warning: no session sess_a" in _invite("sess_a").output


def test_two_sessions_with_the_prefix_are_not_a_crash(reg, tmp_path):
    for name in ("sess_a1", "sess_a2"):
        (tmp_path / "sessions" / TENANT / name).mkdir(parents=True)
    r = _invite("sess_a")
    assert r.exit_code == 0 and isinstance(r.exception, (type(None), click.ClickException))
    assert "warning: no session sess_a" in r.output


# ── Step 2: --rebind ─────────────────────────────────────────────────────────────

def test_rebind_moves_the_binding_its_anchors_and_the_conversation(reg, tmp_path, moves):
    assert _invite("sess_old").exit_code == 0
    reg.seed_thread_anchor(surface="discord", address="1234", thread_id="m1",
                           session_id="sess_old", user_id=TENANT)
    store = ConversationStore(os.path.join(str(tmp_path), "conversations.db"))
    store.record_inbound(TENANT, "discord", "1234", "hello", session_id="sess_old")
    r = _invite("sess_new", "--rebind")
    assert r.exit_code == 0, r.output
    assert "from session sess_old -> sess_new" in r.output and "2 binding row(s)" in r.output
    assert {row["session_id"] for row in reg.list(TENANT)} == {"sess_new"}
    assert store.get(TENANT, "discord", "1234")["session_id"] == "sess_new"
    assert moves == [(TENANT, "discord", "1234", "sess_old", "sess_new")]


def test_rebind_keeps_a_pending_contact_pending(reg, moves, monkeypatch):
    monkeypatch.setenv("CORRESPONDENT_REQUIRE_APPROVAL", "true")
    assert _invite("sess_old").exit_code == 0
    assert _invite("sess_new", "--rebind").exit_code == 0
    assert _row(reg)["state"] == "pending"
    assert reg.resolve(surface="discord", address="1234") is None


def test_rebind_never_reopens_an_expired_contact(reg, moves):
    assert _invite("sess_old").exit_code == 0
    reg.deactivate(surface="discord", address="1234", user_id=TENANT)
    r = _invite("sess_new", "--rebind")
    assert r.exit_code == 1 and "rejected or revoked" in r.output
    assert _row(reg)["session_id"] == "sess_old" and moves == []


def test_rebind_to_the_same_session_records_no_move(reg, moves):
    assert _invite("sess_a").exit_code == 0
    assert _invite("sess_a", "--rebind").exit_code == 0
    assert moves == []


def test_rebind_with_no_binding_creates_nothing(reg, moves):
    r = _invite("sess_a", "--rebind")
    assert r.exit_code == 1 and "no binding to move" in r.output
    assert _row(reg) is None and moves == []


def test_rebind_refuses_a_thread(reg, moves):
    assert _invite("sess_a").exit_code == 0
    r = _invite("sess_b", "--rebind", "--thread", "m1")
    assert r.exit_code == 2 and moves == []


def test_rebind_says_when_the_conversation_did_not_move(reg, tmp_path, moves, monkeypatch):
    assert _invite("sess_old").exit_code == 0
    ConversationStore(os.path.join(str(tmp_path), "conversations.db"))

    def fails(*_a, **_k):
        raise OSError("disk is full")

    monkeypatch.setattr(ConversationStore, "rebind_session", fails)
    r = _invite("sess_new", "--rebind")
    assert r.exit_code == 1 and "the stored conversation did not" in r.output
    assert "OSError: disk is full" in r.output
    assert _row(reg)["session_id"] == "sess_new" and len(moves) == 1


def test_a_move_records_one_event_and_tells_the_owner(monkeypatch):
    from core.surfaces import seed
    events, notices = [], []

    class Log:
        def record(self, kind, **kw):
            events.append((kind, kw))

    monkeypatch.setattr(seed, "_event_log", lambda: Log())
    monkeypatch.setattr(seed, "_push_contact_notice",
                        lambda _c, user, sid, text, *, source: notices.append((user, sid, text, source)))
    seed.record_contact_rebound(TENANT, "discord", "1234", "sess_old", "sess_new")
    kind, kw = events[0]
    assert kind == "correspondent_rebound"
    assert kw["attrs"] == {"address": "1234", "from_session": "sess_old", "to_session": "sess_new"}
    user, sid, text, source = notices[0]
    assert (user, sid, source) == (TENANT, "sess_new", "correspondent_rebound")
    assert "sess_old" in text and "sess_new" in text


def test_a_failed_event_still_tells_the_owner(monkeypatch):
    from core.surfaces import seed
    notices = []

    class Log:
        def record(self, *_a, **_k):
            raise OSError("no disk")

    monkeypatch.setattr(seed, "_event_log", lambda: Log())
    monkeypatch.setattr(seed, "_push_contact_notice",
                        lambda *a, **k: notices.append(a))
    seed.record_contact_rebound(TENANT, "discord", "1234", "sess_old", "sess_new")
    assert len(notices) == 1


def test_the_verb_stays_under_owner_help():
    assert "invite" in owner.commands and "--rebind" in str(
        [p.opts for p in owner.commands["invite"].params])
