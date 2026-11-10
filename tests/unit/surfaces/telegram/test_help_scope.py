"""F-05 (113 W7): a non-owner DM reads only the verbs he can run."""
from core.surfaces.dispatcher import RouteDecision, RouteKind
from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
from surfaces.telegram.help_scope import LIFECYCLE_VERBS, lifecycle_help, scoped_help
from surfaces.telegram.inbound import InboundResult


def _result(text="/help", *, user="u_someone", chat_type="dm", surface="telegram"):
    src = SessionSource(surface_id=surface, chat_id="55", chat_type=chat_type)
    return InboundResult(
        inbound=InboundMessage(text=text, identity=Identity(user_id=user, source=src)),
        decision=RouteDecision(RouteKind.COMMAND, f"agent:main:{surface}:{chat_type}:55",
                               command="/help"))


def test_non_owner_dm_gets_the_lifecycle_rows(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u_owner_fixture")
    out = scoped_help(_result())
    assert out == lifecycle_help(out.rsplit("talk to ", 1)[1].rstrip("."))
    lines = [ln.split()[0] for ln in out.splitlines() if ln.startswith("/")]
    assert lines == list(LIFECYCLE_VERBS)


def test_every_surface_uses_the_same_rule(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u_owner_fixture")
    assert scoped_help(_result(surface="slack")) is not None


def test_the_owner_and_a_room_keep_their_help(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u_owner_fixture")
    assert scoped_help(_result(user="u_owner_fixture")) is None
    assert scoped_help(_result(chat_type="group")) is None


def test_help_for_a_lifecycle_verb_is_its_detail(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u_owner_fixture")
    assert scoped_help(_result("/help close")) is None
    assert scoped_help(_result("/help wallet")) is not None


def test_an_owner_check_fault_reads_as_not_the_owner(monkeypatch):
    import core.instance as inst

    def _boom():
        raise RuntimeError("unreadable")
    monkeypatch.setattr(inst, "resolve_owner_principal", lambda *a, **k: _boom())
    assert scoped_help(_result(user="u_owner_fixture")) is not None
