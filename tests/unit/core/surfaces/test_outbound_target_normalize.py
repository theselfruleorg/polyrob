"""2026-08-17: telegram outbound-target normalization + bot-recipient guard.

Live journal failures (2026-08-15..16): the agent sent to ``t.me/thepublicden``
and bare ``thepublicden`` (Bad Request: chat not found — the Bot API takes
``@username`` or a numeric id, never a link), and to a ``…bot`` handle
(Forbidden: bots can't send messages to bots — structurally impossible).
"""
import asyncio

from core.surfaces.outbound_target import is_bot_username, normalize_surface_target


class TestNormalizeSurfaceTarget:
    def test_tme_link_becomes_handle(self):
        assert normalize_surface_target("telegram", "t.me/thepublicden") == "@thepublicden"
        assert normalize_surface_target("telegram", "https://t.me/thepublicden") == "@thepublicden"
        assert normalize_surface_target("telegram", "https://www.telegram.me/thepublicden/") == "@thepublicden"

    def test_bare_username_gets_at_prefix(self):
        assert normalize_surface_target("telegram", "thepublicden") == "@thepublicden"

    def test_numeric_ids_and_handles_pass_through(self):
        assert normalize_surface_target("telegram", "28436760") == "28436760"
        assert normalize_surface_target("telegram", "-1001234567890") == "-1001234567890"
        assert normalize_surface_target("telegram", "@already") == "@already"

    def test_invite_links_pass_through_unmangled(self):
        # no addressable chat id exists inside an invite hash — do not fabricate one
        assert normalize_surface_target("telegram", "t.me/+AbCdEf123") == "t.me/+AbCdEf123"
        assert normalize_surface_target(
            "telegram", "https://t.me/joinchat/AbCdEf123").startswith("https://t.me/joinchat")

    def test_other_surfaces_untouched(self):
        assert normalize_surface_target("email", "t.me/thepublicden") == "t.me/thepublicden"
        assert normalize_surface_target("email", "someone@example.com") == "someone@example.com"

    def test_non_string_passes_through(self):
        assert normalize_surface_target("telegram", 28436760) == 28436760


class TestIsBotUsername:
    def test_bot_handle_detected(self):
        assert is_bot_username("telegram", "@tmachinrobot") is True
        assert is_bot_username("telegram", "@SomeOtherBOT") is True

    def test_non_bot_targets_pass(self):
        assert is_bot_username("telegram", "@thepublicden") is False
        assert is_bot_username("telegram", "-1001234567890") is False
        assert is_bot_username("telegram", "28436760") is False
        # bare (un-normalized) strings are not judged — normalization runs first
        assert is_bot_username("telegram", "somebot") is False
        assert is_bot_username("email", "bots@bot") is False


class TestPerformMessageSendWiring:
    """The send helper keeps the RAW target for tier/allowlist/store matching
    (owner-authored allowlist entries match byte-exact) and normalizes only at
    the API boundary; a bot recipient is refused BEFORE hitting the API."""

    class _Router:
        def __init__(self):
            self.sent = []

        async def send_message(self, chat_id, text, surface_id, media=None):
            self.sent.append(chat_id)
            return True

    class _Allowlist:
        def __init__(self, entries):
            self.entries = set(entries)

        def is_allowed(self, user_id, surface, target):
            return str(target) in self.entries

    def test_tme_link_matches_raw_allowlist_but_sends_normalized(self):
        from tools.controller.message_send import perform_message_send
        router = self._Router()
        # The owner allowlisted the RAW form the agent types; the API call
        # still goes out '@'-shaped.
        result = asyncio.run(perform_message_send(
            router=router, allowlist=self._Allowlist({"t.me/thepublicden"}),
            owner_targets={}, user_id="rob",
            surface="telegram", target="t.me/thepublicden", text="hi"))
        assert result["success"] is True
        assert result["tier"] == "allowlisted"
        assert result["target"] == "t.me/thepublicden"
        assert result["sent_as"] == "@thepublicden"
        assert router.sent == ["@thepublicden"]

    def test_bot_recipient_refused_before_send(self):
        from tools.controller.message_send import perform_message_send
        router = self._Router()
        result = asyncio.run(perform_message_send(
            router=router, allowlist=None, owner_targets={}, user_id="rob",
            surface="telegram", target="someotherbot", text="hi"))
        assert result["success"] is False
        assert "bot" in result["error"]
        assert "target='owner'" in result["error"]
        assert router.sent == [], "the doomed API call must never fire"
