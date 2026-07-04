"""Regression: EmailHarness must provide a UserDirectory so inbound email is not
silently dropped.

Only the telegram harness ever registered a UserDirectory, so `polyrob email`
had none; build_inbound_message then raised AttributeError on None, which
poll_once swallowed AFTER marking the message \\Seen — permanently losing it.
"""
from surfaces.email.harness import EmailHarness
from surfaces.email.inbound import build_inbound_message


class _Container:
    def __init__(self):
        self._svc = {}

    def get_service(self, name):
        return self._svc.get(name)

    def register_service(self, name, svc):
        self._svc[name] = svc


def test_harness_builds_and_registers_user_directory(tmp_path):
    c = _Container()
    h = EmailHarness(c, task_agent=None, email_tool=object(), data_dir=str(tmp_path))
    assert h.user_directory is not None, "EmailHarness must have a UserDirectory"
    assert c.get_service("user_directory") is h.user_directory
    # It can identify a sender — the previously-crashing path.
    assert h.user_directory.resolve_internal("john@acme.com", "email")


def test_build_inbound_message_none_directory_is_loud_not_crash():
    # Defense-in-depth: None directory returns None (logged), never AttributeError.
    out = build_inbound_message({"from": "John <john@acme.com>", "body": "hi"}, None)
    assert out is None
