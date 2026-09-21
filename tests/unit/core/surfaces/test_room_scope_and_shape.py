"""D15, D23 and D13 — a room verb means THIS room, and a member grant works.

* **D15** — `/paid cancel <id>` took an offer id and nothing else. `/paid` is
  reachable by a ROOM ADMIN and `/paid offers` prints ids in full, so one
  room's admin could withdraw any other room's pending offer by quoting its id.
* **D23** — the room SERVICE job hardcoded `chat_type="supergroup"` and dropped
  the thread entirely, so a job in a plain group or a channel bound a session
  key for a chat type the room does not have, and a job inside a forum TOPIC
  answered into the group's root thread.
* **D13** — a member's GRANTED verb was refused by the mode gate before the
  grant was consulted, so 046's `chat.member_verbs` only ever worked in an
  `active` room.
"""
import pytest

from core.surfaces.chat_policy import ChatPolicy, mode_allows_trigger


# --- D15 --------------------------------------------------------------------

class _Row:
    def __init__(self, offer_id, surface, chat_id, status="pending"):
        self.offer_id = offer_id
        self.surface = surface
        self.chat_id = chat_id
        self.status = status
        self.verb = "mute"
        self.target_name = "S"
        self.target_user_id = "9911"


class _Store:
    def __init__(self, row):
        self._row = row
        self.set_calls = []

    def get(self, offer_id):
        return self._row if self._row.offer_id == offer_id else None

    def set_status(self, offer_id, status, reason=""):
        self.set_calls.append((offer_id, status, reason))


@pytest.fixture()
def admin_ops(monkeypatch):
    from core.surfaces import room_action_admin as adm
    row = _Row("off-1", "telegram", "-100")
    store = _Store(row)
    monkeypatch.setattr(adm, "_store", lambda container: store)
    return adm, store


def test_cancel_withdraws_an_offer_in_this_room(admin_ops):
    adm, store = admin_ops
    out = adm.cancel(None, "off-1", by="admin-7", surface="telegram",
                     chat_id="-100")
    assert out.startswith("✅")
    assert store.set_calls == [("off-1", "refused", "cancelled by admin-7")]


def test_cancel_refuses_another_rooms_offer(admin_ops):
    """⚠️ Deliberately the SAME sentence as an unknown id: confirming that an
    id exists somewhere else is itself information about another room's trade."""
    adm, store = admin_ops
    out = adm.cancel(None, "off-1", by="admin-7", surface="telegram",
                     chat_id="-999")
    assert out == "❌ unknown offer off-1."
    assert store.set_calls == []


def test_cancel_unscoped_still_works_for_an_owner_seat(admin_ops):
    """Omitting the scope is the legacy call, reserved for the owner's own
    seats (the CLI and the console), which are not room-bounded."""
    adm, store = admin_ops
    assert adm.cancel(None, "off-1", by="rob").startswith("✅")


# --- D23 --------------------------------------------------------------------

def _service_job(monkeypatch, tmp_path, **kwargs):
    from core.surfaces import group_admin
    from cron import room_service

    scheduled = {}

    class _Job:
        id = "job-1234abcd"
        schedule_spec = "30m"

    class _Cron:
        def list_jobs(self, uid):
            return []

        def schedule(self, *, task, schedule_spec, user_id, payload):
            scheduled.update(payload=payload, task=task)
            return _Job()

    monkeypatch.setattr(room_service, "_cron_service", lambda d: _Cron())
    monkeypatch.setattr(group_admin, "data_home", lambda c: str(tmp_path))
    monkeypatch.setattr(group_admin, "is_room_allowed", lambda c, s, cid: True)
    out = room_service.service(None, "rob", "telegram", "-100", **kwargs)
    return out, scheduled


def test_the_service_job_persists_the_real_chat_type(monkeypatch, tmp_path):
    _out, scheduled = _service_job(monkeypatch, tmp_path, chat_type="channel")
    assert scheduled["payload"]["group"]["chat_type"] == "channel"


def test_the_service_job_persists_the_forum_topic(monkeypatch, tmp_path):
    _out, scheduled = _service_job(monkeypatch, tmp_path,
                                   chat_type="supergroup", thread_id="77")
    assert scheduled["payload"]["group"]["thread_id"] == "77"


def test_an_unknown_chat_type_keeps_the_historical_value(monkeypatch, tmp_path):
    """A caller that cannot tell us must not make us guess something NEW."""
    _out, scheduled = _service_job(monkeypatch, tmp_path)
    assert scheduled["payload"]["group"]["chat_type"] == "supergroup"
    assert "thread_id" not in scheduled["payload"]["group"]


# --- D13 --------------------------------------------------------------------

def test_a_members_granted_verb_is_addressed_in_a_mention_room():
    """046's grant was reachable only from an `active` room: the mode gate
    refused the line before `chat.member_verbs` was ever consulted."""
    policy = ChatPolicy.defaults().replace(mode="mention",
                                           member_verbs=("help", "mute"))
    assert mode_allows_trigger(policy, mentioned=False, role="member",
                               wake_hit=False, is_command=True,
                               granted_command=True) is True


def test_an_ungranted_member_command_is_still_not_addressed():
    """The bonus is the GRANT, never the slash. A stranger's `/groups allow
    here` must not force a room open."""
    policy = ChatPolicy.defaults().replace(mode="mention", member_verbs=("help",))
    assert mode_allows_trigger(policy, mentioned=False, role="member",
                               wake_hit=False, is_command=True,
                               granted_command=False) is False


def test_a_granted_member_verb_is_still_refused_in_listen_mode():
    """`listen` means owner-and-admin only. A member grant does not promote
    him past the room's own mode."""
    policy = ChatPolicy.defaults().replace(mode="listen",
                                           member_verbs=("help", "mute"))
    assert mode_allows_trigger(policy, mentioned=True, role="member",
                               wake_hit=False, is_command=True,
                               granted_command=True) is False


def test_the_owner_bonus_is_unchanged():
    policy = ChatPolicy.defaults().replace(mode="mention")
    assert mode_allows_trigger(policy, mentioned=False, role="owner",
                               wake_hit=False, is_command=True) is True
