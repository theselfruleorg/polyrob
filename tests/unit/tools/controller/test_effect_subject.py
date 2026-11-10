"""108 W2: a verb declares its subject; the row stores it, with the id the act made."""
import types

import pytest
from pydantic import BaseModel

import core.event_log as el
from core.verb_policy import VerbPolicy


@pytest.fixture
def tlog(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_ENABLED", "true")
    el._INSTANCES.clear()
    yield lambda: el.get_event_log()
    el._INSTANCES.clear()


class _Controller:
    _is_sub_agent = False
    session_id = "s1"

    def __init__(self, tool, model=None):
        self._details = types.SimpleNamespace(tool=tool, function=None, param_model=model)

    def get_action_details(self, name):
        return self._details


def _ok(text="done"):
    return types.SimpleNamespace(error=None, extracted_content=text)


def test_only_a_social_verb_may_declare_a_subject():
    VerbPolicy("t_reply", tool="t", effect="social", subject="post")
    for effect in ("money", "comms", "inherit", "none"):
        with pytest.raises(ValueError, match="only a social verb"):
            VerbPolicy("t_a", tool="t", effect=effect, subject="post")


def test_no_money_verb_declares_a_subject():
    from core.verb_policy import VERB_POLICY
    assert not [r.name for r in VERB_POLICY.values() if r.subject and r.effect != "social"]


@pytest.mark.asyncio
async def test_a_row_stores_the_declared_param(tlog, monkeypatch):
    from tools.controller import effect_hooks
    monkeypatch.setattr(effect_hooks, "_declared_subject", lambda name: "post_id")
    await effect_hooks.make_effect_record_hook(_Controller("twitter"))(
        "twitter_reply", {"post_id": " 1234567 ", "text": "hi"}, _ok(), None)
    assert tlog().query(kind="external_write")[0]["attrs"]["subject"] == "1234567"


@pytest.mark.asyncio
async def test_a_property_of_the_param_model_normalizes_the_subject(tlog, monkeypatch):
    from tools.controller import effect_hooks

    class _Params(BaseModel):
        link: str

        @property
        def bare(self):
            return self.link.rsplit("/", 1)[-1]

    monkeypatch.setattr(effect_hooks, "_declared_subject", lambda name: "bare")
    await effect_hooks.make_effect_record_hook(_Controller("twitter", _Params))(
        "twitter_reply", {"link": "https://example.test/status/1234567"}, _ok(), None)
    assert tlog().query(kind="external_write")[0]["attrs"]["subject"] == "1234567"


@pytest.mark.asyncio
async def test_a_verb_with_no_subject_has_no_key(tlog):
    from tools.controller import effect_hooks
    await effect_hooks.make_effect_record_hook(_Controller("twitter"))(
        "twitter_post", {"text": "hi"}, _ok(), None)
    assert "subject" not in tlog().query(kind="external_write")[0]["attrs"]


@pytest.mark.parametrize("text, effect, want", [
    ("🐦 Replied with tweet 7654321 — verified: tweet 7654321 is readable by id", "social", "7654321"),
    ("🐦 Quoted tweet 7654321.", "social", "7654321"),
    ("🐦 Reply exceeded 280 chars — posted as a 2-tweet reply thread: 7654321, 7654322", "social", "7654321"),
    ("replied on X under status 1111111: https://x.com/me/status/7654321\nproof", "social", "7654321"),
    ("replied on X under status 1111111: https://x.com/me\nproof", "social", ""),
    ("🐦 Replied with tweet None.", "social", ""),
    ("message OK | text: 'see https://x.com/a/status/7654321'", "comms", ""),
    ("message OK [message_id 4034] | text: 'hi'", "comms", "4034"),
])
def test_the_ref_is_the_id_the_act_made(text, effect, want):
    from tools.controller.effect_hooks import _result_ref
    assert _result_ref(_ok(text), effect) == want


def test_the_x_verbs_declare_their_subject_in_the_pack():
    pytest.importorskip("polyrob_x")
    import importlib.resources as ir
    import tomllib
    data = tomllib.loads(ir.files("polyrob_x").joinpath("pack.toml").read_text("utf-8"))
    tw = data["tools"]["twitter"]["verbs"]
    assert tw["twitter_reply"]["subject"] == tw["twitter_quote"]["subject"] == "tweet_id"
    assert data["tools"]["x_browser"]["verbs"]["x_browser_x_reply"]["subject"] == "status_id"




def _fp_dedup(params):
    from core.effects import dedup_key
    from tools.controller.effect_hooks import _fingerprint
    return dedup_key("comms", "unknown", _fingerprint(params))


def test_different_texts_differ_and_the_same_call_matches():
    assert _fp_dedup({"text": "one"}) != _fp_dedup({"text": "two"})
    assert _fp_dedup({"text": "one", "target": "@a"}) == _fp_dedup({"target": "@a", "text": "one"})


def test_the_same_text_to_two_addressees_differs():
    assert _fp_dedup({"text": "one", "target": "@a"}) != _fp_dedup({"text": "one", "target": "@b"})


@pytest.mark.parametrize("left, right", [
    ({"texts": ["a", "b"]}, {"texts": ["c", "d"]}),
    ({"tweet_id": "1234567"}, {"tweet_id": "7654321"}),
    ({"token_in": "A", "amount": "1"}, {"token_in": "A", "amount": "2"}),
    ({"text": "x" * 500 + "a"}, {"text": "x" * 500 + "b"}),
])
def test_a_verbs_own_param_names_count(left, right):
    assert _fp_dedup(left) != _fp_dedup(right)


def test_an_empty_param_says_nothing():
    from tools.controller.effect_hooks import _fingerprint
    assert _fingerprint({"text": "", "target": None, "media_paths": []}) == ""
    assert _fingerprint({"text": "one", "target": None}) == _fingerprint({"text": "one"})


def test_a_param_model_gives_the_same_fingerprint():
    from tools.controller.effect_hooks import _fingerprint

    class _Params(BaseModel):
        text: str = ""
        target: str = ""

    assert _fingerprint(_Params(text="one")) == _fingerprint({"text": "one"})


@pytest.mark.asyncio
async def test_listing_the_post_ledger_is_no_act(tlog):
    from tools.controller.effect_hooks import make_effect_record_hook
    hook = make_effect_record_hook(_Controller(None))
    await hook("message", {"action": "posts"}, _ok("my posts"), None)
    await hook("message", {"action": "delete", "post": 3}, _ok("deleted"), None)
    await hook("message", {"text": "one"}, _ok("OK"), None)
    assert len(tlog().query(kind="external_write")) == 2
