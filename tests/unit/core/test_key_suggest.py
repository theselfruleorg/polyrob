"""D2 (prod 2026-10-10): `preferences explain TWITTER_APPROVAL_REQUIRED` said only
"unknown setting" — the real flag is TWITTER_REQUIRE_APPROVAL. Every unknown-key
refusal now names the closest keys, word order notwithstanding."""
from core import config_service
from core.key_suggest import did_you_mean, suggest_keys


def test_reordered_words_find_the_real_flag():
    hits = suggest_keys("TWITTER_APPROVAL_REQUIRED", config_service.known_keys())
    assert hits[0] == "TWITTER_REQUIRE_APPROVAL"


def test_nothing_close_gives_no_hint():
    assert suggest_keys("zzzz", config_service.known_keys()) == []
    assert did_you_mean("zzzz", ["GOALS_ENABLED"]) == ""


def test_did_you_mean_lists_up_to_three():
    out = did_you_mean("GOALS_ENABLD", ["GOALS_ENABLED", "GOAL_ENABLED", "X"])
    assert out.startswith(" (did you mean GOALS_ENABLED")


def test_unknown_key_message_names_the_real_key():
    msg = config_service.unknown_key_message("TWITTER_APPROVAL_REQUIRED")
    assert "unknown setting: TWITTER_APPROVAL_REQUIRED" in msg
    assert "did you mean TWITTER_REQUIRE_APPROVAL" in msg


def test_closest_key_prefers_the_word_match():
    assert config_service.closest_key("TWITTER_APPROVAL_REQUIRED") == "TWITTER_REQUIRE_APPROVAL"


def test_set_refusal_carries_the_suggestion():
    res = config_service.set_value("TWITTER_APPROVAL_REQUIRED", "true")
    assert not res.ok
    assert res.suggestion == "TWITTER_REQUIRE_APPROVAL"


def test_unknown_pref_error_names_a_close_instance_flag():
    from core.prefs import unknown_pref_error
    err = unknown_pref_error("TWITTER_APPROVAL_REQUIRED", "true")
    assert "TWITTER_REQUIRE_APPROVAL" in err and "instance flag" in err
