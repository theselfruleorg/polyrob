"""2026-08-27 dedup-guard fix: an autonomous/forged turn proactively messaging
the OWNER is rate-limited against resending within a cooldown window, checked
against the durable conversation store's real send history — not the model's
own self-report of elapsed time.

Confirmed live pattern this closes: a fresh goal session has no visibility
into a SIBLING session's send from ~2h earlier, so 4 genuine sends of the same
consolidated owner ask landed in Telegram within ~4 hours.
"""
from tools.controller.action_registration import _autonomous_owner_resend_cooldown_refusal


class _Ctx:
    def __init__(self, is_sub_agent=False, role="orchestrator", metadata=None):
        self.is_sub_agent = is_sub_agent
        self.role = role
        self.metadata = metadata or {}


class _Controller:
    _is_sub_agent = False


_AUTONOMOUS_CTX = _Ctx(is_sub_agent=True)          # a leaf/sub-agent turn
_OWNER_CTX = _Ctx(is_sub_agent=False, role="orchestrator")  # genuine owner turn


class _FakeStore:
    def __init__(self, count=0):
        self._count = count
        self.calls = []

    def outbound_count_since(self, user_id, surface, address, since_secs, **kw):
        self.calls.append((user_id, surface, address, since_secs))
        return self._count


class _FakeContainer:
    def __init__(self, store):
        self._store = store

    def get_service(self, name):
        assert name == "conversation_store"
        return self._store


_OWNER_TARGETS = {"telegram": "28436760"}


def test_genuine_owner_turn_never_gated(monkeypatch):
    monkeypatch.setenv("OWNER_MESSAGE_COOLDOWN_SEC", "7200")
    store = _FakeStore(count=5)
    res = _autonomous_owner_resend_cooldown_refusal(
        _OWNER_CTX, _Controller(), container=_FakeContainer(store), user_id="rob",
        surface="telegram", target="owner", owner_targets=_OWNER_TARGETS)
    assert res is None
    assert store.calls == []  # never even queried for a real owner turn


def test_autonomous_turn_no_recent_send_not_gated(monkeypatch):
    monkeypatch.setenv("OWNER_MESSAGE_COOLDOWN_SEC", "7200")
    store = _FakeStore(count=0)
    res = _autonomous_owner_resend_cooldown_refusal(
        _AUTONOMOUS_CTX, _Controller(), container=_FakeContainer(store), user_id="rob",
        surface="telegram", target="owner", owner_targets=_OWNER_TARGETS)
    assert res is None


def test_autonomous_turn_recent_send_refused_with_actionable_message(monkeypatch):
    monkeypatch.setenv("OWNER_MESSAGE_COOLDOWN_SEC", "7200")
    store = _FakeStore(count=1)
    res = _autonomous_owner_resend_cooldown_refusal(
        _AUTONOMOUS_CTX, _Controller(), container=_FakeContainer(store), user_id="rob",
        surface="telegram", target="owner", owner_targets=_OWNER_TARGETS)
    assert res is not None
    assert "contact_history" in res.extracted_content
    assert "telegram" in res.extracted_content
    # queried against the RESOLVED owner address, not the literal alias
    assert store.calls == [("rob", "telegram", "28436760", 7200)]


def test_autonomous_turn_recent_send_to_real_owner_address_also_gated(monkeypatch):
    """The model may pass the resolved address directly instead of the 'owner' alias."""
    monkeypatch.setenv("OWNER_MESSAGE_COOLDOWN_SEC", "7200")
    store = _FakeStore(count=1)
    res = _autonomous_owner_resend_cooldown_refusal(
        _AUTONOMOUS_CTX, _Controller(), container=_FakeContainer(store), user_id="rob",
        surface="telegram", target="28436760", owner_targets=_OWNER_TARGETS)
    assert res is not None


def test_non_owner_target_never_gated(monkeypatch):
    """Only sends resolving to the OWNER are cooldown-checked — an allowlisted
    third-party target is unaffected regardless of recent owner sends."""
    monkeypatch.setenv("OWNER_MESSAGE_COOLDOWN_SEC", "7200")
    store = _FakeStore(count=5)
    res = _autonomous_owner_resend_cooldown_refusal(
        _AUTONOMOUS_CTX, _Controller(), container=_FakeContainer(store), user_id="rob",
        surface="telegram", target="@some_promo_chat", owner_targets=_OWNER_TARGETS)
    assert res is None
    assert store.calls == []


def test_cooldown_disabled_never_gates(monkeypatch):
    monkeypatch.setenv("OWNER_MESSAGE_COOLDOWN_SEC", "0")
    store = _FakeStore(count=5)
    res = _autonomous_owner_resend_cooldown_refusal(
        _AUTONOMOUS_CTX, _Controller(), container=_FakeContainer(store), user_id="rob",
        surface="telegram", target="owner", owner_targets=_OWNER_TARGETS)
    assert res is None


def test_no_container_fails_open(monkeypatch):
    monkeypatch.setenv("OWNER_MESSAGE_COOLDOWN_SEC", "7200")
    res = _autonomous_owner_resend_cooldown_refusal(
        _AUTONOMOUS_CTX, _Controller(), container=None, user_id="rob",
        surface="telegram", target="owner", owner_targets=_OWNER_TARGETS)
    assert res is None


def test_default_cooldown_is_two_hours(monkeypatch):
    from core.config_policy import owner_message_cooldown_seconds
    monkeypatch.delenv("OWNER_MESSAGE_COOLDOWN_SEC", raising=False)
    assert owner_message_cooldown_seconds() == 7200


# --- 2026-09-15 prod review, C5: the gate must read CONTENT ------------------
#
# The window was a bare COUNT of any owner send, so a materially new blocker was
# refused because something unrelated had gone out within 2h. Prod, verbatim:
#
#   contact_history checked: last owner sends were 09:12 (telegram), 08:14,
#   07:35 (email) — ALL predate the guard-blocker discovery (~09:40). The
#   blocker is materially new, so the retry is justified per the dedup guard's
#   own guidance.
#   … Retried owner notice; suppressed again by the 2h Telegram dedup despite
#   being new content. … Reporting to the session user and ending BLOCKED.
#
# The agent did exactly what the refusal text told it to do and was refused
# anyway. 32 refusals in 7 days; the goal ended BLOCKED, owner never informed.

class _BodyStore(_FakeStore):
    def __init__(self, bodies):
        super().__init__(count=len(bodies))
        self._bodies = list(bodies)
        self.body_calls = []

    def outbound_bodies_since(self, user_id, surface, address, since_secs, **kw):
        self.body_calls.append((user_id, surface, address, since_secs))
        return list(self._bodies)


def test_materially_new_body_is_not_refused(monkeypatch):
    monkeypatch.setenv("OWNER_MESSAGE_COOLDOWN_SEC", "7200")
    store = _BodyStore(["daily treasury summary: nothing to do"])
    res = _autonomous_owner_resend_cooldown_refusal(
        _AUTONOMOUS_CTX, _Controller(), container=_FakeContainer(store), user_id="rob",
        surface="telegram", target="owner", owner_targets=_OWNER_TARGETS,
        text="X posting is BLOCKED: both rails dead, need credits or a session")
    assert res is None
    assert store.body_calls == [("rob", "telegram", "28436760", 7200)]


def test_same_body_is_still_refused(monkeypatch):
    monkeypatch.setenv("OWNER_MESSAGE_COOLDOWN_SEC", "7200")
    body = "Track-record post BLOCKED — both X rails down, attempt 6"
    store = _BodyStore(["something else", body])
    res = _autonomous_owner_resend_cooldown_refusal(
        _AUTONOMOUS_CTX, _Controller(), container=_FakeContainer(store), user_id="rob",
        surface="telegram", target="owner", owner_targets=_OWNER_TARGETS, text=body)
    assert res is not None
    assert "already" in res.extracted_content


def test_same_body_modulo_whitespace_is_refused(monkeypatch):
    monkeypatch.setenv("OWNER_MESSAGE_COOLDOWN_SEC", "7200")
    store = _BodyStore(["Track-record post BLOCKED — attempt 6"])
    res = _autonomous_owner_resend_cooldown_refusal(
        _AUTONOMOUS_CTX, _Controller(), container=_FakeContainer(store), user_id="rob",
        surface="telegram", target="owner", owner_targets=_OWNER_TARGETS,
        text="  Track-record post BLOCKED — attempt 6\n")
    assert res is not None


def test_legacy_count_gate_when_no_text_is_supplied(monkeypatch):
    """A caller that passes no body keeps the old count-only behaviour."""
    monkeypatch.setenv("OWNER_MESSAGE_COOLDOWN_SEC", "7200")
    store = _BodyStore(["anything"])
    res = _autonomous_owner_resend_cooldown_refusal(
        _AUTONOMOUS_CTX, _Controller(), container=_FakeContainer(store), user_id="rob",
        surface="telegram", target="owner", owner_targets=_OWNER_TARGETS)
    assert res is not None


def test_store_without_body_reader_falls_back_to_the_count_gate(monkeypatch):
    monkeypatch.setenv("OWNER_MESSAGE_COOLDOWN_SEC", "7200")
    store = _FakeStore(count=1)   # no outbound_bodies_since
    res = _autonomous_owner_resend_cooldown_refusal(
        _AUTONOMOUS_CTX, _Controller(), container=_FakeContainer(store), user_id="rob",
        surface="telegram", target="owner", owner_targets=_OWNER_TARGETS,
        text="a genuinely new blocker")
    assert res is not None


def test_a_cooldown_refusal_is_readable_in_missed(monkeypatch, tmp_path):
    """A refusal the owner never saw must leave a durable trace, like every
    other suppression shape. Before this it left none at all."""
    monkeypatch.setenv("OWNER_MESSAGE_COOLDOWN_SEC", "7200")
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    from core.event_log import TelemetryEventLog
    from core.surfaces.user_delivery import NOTICE_MARKERS
    log = TelemetryEventLog(str(tmp_path / "t.db"))
    body = "the same blocker again"
    store = _BodyStore([body])
    res = _autonomous_owner_resend_cooldown_refusal(
        _AUTONOMOUS_CTX, _Controller(), container=_FakeContainer(store), user_id="rob",
        surface="telegram", target="owner", owner_targets=_OWNER_TARGETS, text=body,
        event_log=log)
    assert res is not None
    notices = log.query(kind="owner_notice", user_id="rob")
    assert notices and notices[0]["attrs"]["text"].startswith(NOTICE_MARKERS[4])
    assert body in notices[0]["attrs"]["text"]
    rows = log.query(kind="user_delivery", user_id="rob")
    assert rows and rows[0]["attrs"]["outcome"] == "cooldown"
