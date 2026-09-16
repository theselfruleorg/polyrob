"""046: what a MEMBER of a room is told — the price list, the cap, the rail.

The defect these pin: a member's `/help` answered with the OWNER's whole verb
catalog and delivered it to the OWNER's DM, so the member who asked saw nothing.
"""
import pytest

from core.surfaces import room_actions
from core.surfaces.chat_policy import ChatPolicy


class _Cfg:
    def __init__(self, data_dir):
        self.data_dir = data_dir


class _Container:
    def __init__(self, data_dir):
        self.config = _Cfg(data_dir)

    def get_service(self, name):
        return None


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("ROOM_ACTIONS_ENABLED", "true")


def _priced(**kw):
    base = dict(member_verbs=("help", "mute", "ban"), paid_enabled=True,
                paid_mute_usd=0.5, paid_mute_max_duration="24h",
                paid_ban_usd=2.0, paid_ban_max_duration="6h")
    base.update(kw)
    return ChatPolicy(**base)


# --- the cap and the price have ONE reader each ----------------------------

def test_the_help_quotes_the_cap_the_offer_path_enforces():
    """⚠️ A second cap reader would advertise a duration the offer then refuses."""
    policy = _priced(paid_mute_max_duration="90d")
    eff = room_actions.EFFECTS["mute"]
    # The room asked for 90d; the CATALOG caps mute at 30d, and that is what the
    # member must be told.
    assert room_actions.max_duration_sec(policy, eff) == 30 * 86400
    assert room_actions.format_duration(30 * 86400) == "30d"


def test_offer_and_help_read_the_same_cap_function():
    import inspect
    assert "max_duration_sec(policy, eff)" in inspect.getsource(room_actions.offer)
    assert "max_duration_sec(policy, eff)" in inspect.getsource(
        room_actions.member_price_rows)


def test_the_member_rows_read_the_one_price_band():
    import inspect
    assert "_price_band(policy, verb)" in inspect.getsource(
        room_actions.member_price_rows)


# --- only what this room GRANTED ------------------------------------------

def test_a_priced_but_ungranted_verb_is_not_advertised_to_members(on, tmp_path):
    policy = _priced(member_verbs=("help", "mute"))
    rows = room_actions.member_price_rows(_Container(str(tmp_path)),
                                          surface="telegram", chat_id="-1",
                                          policy=policy)
    assert [r.verb for r in rows] == ["mute"]


def test_a_granted_but_unpriced_verb_is_NAMED_with_its_reason(on, tmp_path):
    """⚠️ Silence would read as 'that verb does not exist here' while every
    attempt to use it is refused."""
    policy = _priced(member_verbs=("help", "mute", "ban"), paid_ban_usd=0.0)
    rows = {r.verb: r for r in room_actions.member_price_rows(
        _Container(str(tmp_path)), surface="telegram", chat_id="-1",
        policy=policy)}
    assert rows["ban"].sellable is False
    assert "no price" in rows["ban"].why
    text = room_actions.render_member_prices(_Container(str(tmp_path)),
                                             surface="telegram", chat_id="-1",
                                             policy=policy)
    assert "/ban — not available" in text


def test_the_instance_flag_off_is_said_not_hidden(tmp_path, monkeypatch):
    monkeypatch.delenv("ROOM_ACTIONS_ENABLED", raising=False)
    rows = room_actions.member_price_rows(_Container(str(tmp_path)),
                                          surface="telegram", chat_id="-1",
                                          policy=_priced())
    assert all(not r.sellable for r in rows)
    assert all("instance" in r.why for r in rows)


def test_a_room_with_paid_off_says_so(on, tmp_path):
    rows = room_actions.member_price_rows(_Container(str(tmp_path)),
                                          surface="telegram", chat_id="-1",
                                          policy=_priced(paid_enabled=False))
    assert all("off in this room" in r.why for r in rows)


# --- the help itself -------------------------------------------------------

def test_member_help_carries_the_price_the_cap_and_the_usage(on, tmp_path):
    text = room_actions.render_member_help(_Container(str(tmp_path)),
                                           surface="telegram", chat_id="-1",
                                           agent_name="Rob")
    # The default policy (loaded from an empty data dir) grants only `help`.
    assert text.startswith("Rob here")
    assert "/help" in text


def test_member_help_prices_a_granted_room(on, tmp_path, monkeypatch):
    monkeypatch.setattr(room_actions, "policy_for",
                        lambda *a, **kw: _priced())
    text = room_actions.render_member_help(_Container(str(tmp_path)),
                                           surface="telegram", chat_id="-1")
    assert "$0.50" in text and "$2.00" in text
    assert "up to 24h" in text and "up to 6h" in text
    assert "reply to their message with `/mute 30m`" in text
    assert "/paid" not in text          # not granted -> not advertised


def test_member_help_names_paid_only_when_the_room_granted_it(on, tmp_path,
                                                              monkeypatch):
    monkeypatch.setattr(room_actions, "policy_for",
                        lambda *a, **kw: _priced(
                            member_verbs=("help", "mute", "paid")))
    text = room_actions.render_member_help(_Container(str(tmp_path)),
                                           surface="telegram", chat_id="-1")
    assert "/paid — the price list" in text


def test_member_help_never_carries_an_owner_verb(on, tmp_path, monkeypatch):
    from surfaces.telegram.harness import _HELP_BODY
    monkeypatch.setattr(room_actions, "policy_for", lambda *a, **kw: _priced())
    text = room_actions.render_member_help(_Container(str(tmp_path)),
                                           surface="telegram", chat_id="-1")
    for owner_verb in ("/trade", "/wallet", "/pause", "/approve", "/goals",
                       "/deploy", "/launch", "/dev"):
        if owner_verb in _HELP_BODY:
            assert owner_verb not in text, (
                f"{owner_verb} leaked into a member's help")


# --- the model is told, and told it is not the actor -----------------------

def test_describe_for_model_is_empty_when_the_room_sells_nothing(on, tmp_path):
    assert room_actions.describe_for_model(
        _Container(str(tmp_path)), surface="telegram", chat_id="-1",
        policy=ChatPolicy()) == ""


def test_describe_for_model_says_the_member_types_it_not_the_agent(on, tmp_path):
    text = room_actions.describe_for_model(_Container(str(tmp_path)),
                                           surface="telegram", chat_id="-1",
                                           policy=_priced())
    assert "mute $0.50" in text and "ban $2.00" in text
    low = text.lower()
    assert "you do not perform" in low
    assert "no tool for it" in low
    assert "`/mute 30m`" in text
