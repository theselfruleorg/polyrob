"""2026-08-28 log forensics: `message` without surface/target means the owner.

GLM-5 called `message(text=…, media_paths=[…])` 24 times in four days without
`surface`/`target` — always the final "notify the owner" step of a finished
goal. Both fields were required, so validation failed, the executor reported
"action does not exist", counted the step as empty, and two in a row tripped
the thinking-loop escalation. The deliverable existed; the owner was not told.
"""
from tools.controller.message_send import resolve_message_defaults
from tools.controller.views import MessageTargetAction


def test_model_validates_without_surface_and_target():
	m = MessageTargetAction(text="report attached", media_paths=["reports/x.md"])
	assert m.surface is None and m.target is None and m.text == "report attached"


def test_omitted_target_means_owner_on_primary_surface():
	assert resolve_message_defaults(None, None, {"telegram": "28436760", "email": "o@x"}) \
		== ("telegram", "owner")


def test_omitted_surface_falls_back_to_the_first_bound_owner_surface():
	assert resolve_message_defaults(None, "owner", {"email": "o@x"}) == ("email", "owner")
	assert resolve_message_defaults("", "", {}) == ("telegram", "owner")


def test_explicit_values_are_untouched():
	assert resolve_message_defaults("email", "someone@x", {"telegram": "1"}) \
		== ("email", "someone@x")
