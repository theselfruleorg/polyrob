from tools.controller.views import MessageTargetAction


def test_defaults():
	m = MessageTargetAction(surface="telegram", target="123", text="hi")
	assert m.action == "send" and m.reply_to is None
	assert m.media_paths is None


def test_action_field():
	m = MessageTargetAction(surface="telegram", target="123", text="hi", action="reply", reply_to="99")
	assert m.action == "reply" and m.reply_to == "99"


def test_media_paths_field():
	m = MessageTargetAction(surface="telegram", target="123", text="hi",
	                        media_paths=["invoices/card.png"])
	assert m.media_paths == ["invoices/card.png"]
