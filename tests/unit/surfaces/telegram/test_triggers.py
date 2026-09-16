from surfaces.telegram.triggers import entity_text, message_mentions_bot, is_reply_to_bot, sender_is_bot


def test_entity_text_uses_utf16_units():
    text = "🚀 @robbot hi"          # the rocket is 2 UTF-16 units
    assert entity_text(text, 3, 7) == "@robbot"


def test_mention_entity_matches_bot():
    msg = {"text": "hey @RobBot go", "entities": [{"type": "mention", "offset": 4, "length": 7}]}
    assert message_mentions_bot(msg, bot_username="robbot", bot_id=1) is True


def test_bot_command_with_suffix_counts():
    msg = {"text": "/status@robbot", "entities": [{"type": "bot_command", "offset": 0, "length": 14}]}
    assert message_mentions_bot(msg, bot_username="robbot", bot_id=1) is True


def test_bot_command_for_other_bot_does_not_count():
    msg = {"text": "/status@otherbot", "entities": [{"type": "bot_command", "offset": 0, "length": 16}]}
    assert message_mentions_bot(msg, bot_username="robbot", bot_id=1) is False


def test_caption_entities_count():
    msg = {"caption": "@robbot look", "caption_entities": [{"type": "mention", "offset": 0, "length": 7}]}
    assert message_mentions_bot(msg, bot_username="robbot", bot_id=1) is True


def test_text_mention_by_numeric_id():
    msg = {"text": "Rob help", "entities": [{"type": "text_mention", "offset": 0, "length": 3,
                                             "user": {"id": 1, "is_bot": True}}]}
    assert message_mentions_bot(msg, bot_username="robbot", bot_id=1) is True


def test_unknown_username_is_none():
    assert message_mentions_bot({"text": "@robbot"}, bot_username=None, bot_id=None) is None


def test_reply_to_bot_by_id_or_username():
    msg = {"reply_to_message": {"from": {"id": 1, "is_bot": True, "username": "robbot"}}}
    assert is_reply_to_bot(msg, bot_username="robbot", bot_id=1) is True
    assert is_reply_to_bot(msg, bot_username="x", bot_id=2) is False


def test_sender_is_bot():
    assert sender_is_bot({"from": {"id": 9, "is_bot": True}}) is True
    assert sender_is_bot({"from": {"id": 9}}) is False
