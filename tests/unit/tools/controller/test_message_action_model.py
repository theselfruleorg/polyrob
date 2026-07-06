from tools.controller.views import MessageTargetAction


def test_defaults():
	m = MessageTargetAction(surface="telegram", target="123", text="hi")
	assert m.action == "send" and m.reply_to is None


def test_action_field():
	m = MessageTargetAction(surface="telegram", target="123", text="hi", action="reply", reply_to="99")
	assert m.action == "reply" and m.reply_to == "99"
