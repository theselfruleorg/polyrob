"""E6-E10: the owner seats for `/claim`, `/nft`, `/dapp`, `/identity`, `/contacts`.

Each of these rails shipped as an AGENT action and reached no human at all —
the owner could watch them and could not run them. `launchpad_claim` is the
sharpest case: 13.6 ETH of the agent's own creator fees sat in a Pons escrow
while the status snapshot told the owner to "run launchpad_claim", which is not
a thing a human can run from anywhere.

A chat verb must join FIVE lists or its handler is dead code: the dispatcher's
`_COMMANDS`, the harness `_OWNER_ADMIN_COMMANDS`, the dispatch body, the help
SSOT (via `core.verbs`), and — for anything that moves value — the room-refusal
set. These pin all five, plus the parse and the honest empty states.

⚠️ REACH, not policy. Nothing here widens a cap, exempts an approval lane or
skips a gate; every assertion below is about whether the owner can SAY the
thing, never about what the rail then allows.
"""
import pytest

from core.surfaces.dispatcher import _COMMANDS
from core.verbs import verb_for
from surfaces.telegram.harness import (
    _HELP_BODY, _OWNER_ADMIN_COMMANDS, _ROOM_REFUSED_COMMANDS,
)

VERBS = ["/claim", "/nft", "/dapp", "/identity", "/contacts"]


@pytest.mark.parametrize("verb", VERBS)
def test_the_verb_is_routable(verb):
    assert verb in _COMMANDS


@pytest.mark.parametrize("verb", VERBS)
def test_the_verb_is_owner_gated(verb):
    assert verb in _OWNER_ADMIN_COMMANDS


@pytest.mark.parametrize("verb", VERBS)
def test_the_verb_has_a_table_row_and_a_help_line(verb):
    row = verb_for(verb)
    assert row is not None, f"{verb} has no core.verbs row"
    assert f"{verb} " in _HELP_BODY or f"{verb} —" in _HELP_BODY
    # First person, and never a flag name.
    assert row.help[0].isupper()


@pytest.mark.parametrize("verb", VERBS)
def test_the_verb_is_refused_from_a_room(verb):
    assert verb in _ROOM_REFUSED_COMMANDS


# ---------------------------------------------------------------------------
# /claim
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_claim_refuses_a_non_owner():
    from surfaces.telegram.claim_ops import claim_reply
    assert "Only the owner" in await claim_reply(None, ["0xabc"])


@pytest.mark.asyncio
async def test_claim_with_no_token_explains_itself():
    from surfaces.telegram.claim_ops import claim_reply
    out = await claim_reply("rob", [])
    assert "Usage: /claim" in out
    # The escrow credits an ADDRESS, not a token — say so before he runs it.
    assert "ADDRESS" in out


@pytest.mark.asyncio
async def test_claim_is_a_dry_run_until_go(monkeypatch):
    """Typing the verb must never broadcast. `go` is the deliberate second act."""
    seen = {}

    async def _fake(self, params, ctx):
        seen["dry_run"] = params.dry_run
        seen["token"] = params.token
        from types import SimpleNamespace
        return SimpleNamespace(error=None, extracted_content="report")

    monkeypatch.setattr("tools.launchpad.tool.LaunchpadTool.launchpad_claim", _fake)
    from surfaces.telegram.claim_ops import claim_reply
    out = await claim_reply("rob", ["0xabc"])
    assert seen == {"dry_run": True, "token": "0xabc"}
    assert "Add `go` to claim it" in out

    await claim_reply("rob", ["0xabc", "go"])
    assert seen["dry_run"] is False


@pytest.mark.asyncio
async def test_claim_never_reads_an_empty_report_as_success(monkeypatch):
    async def _fake(self, params, ctx):
        from types import SimpleNamespace
        return SimpleNamespace(error=None, extracted_content="")

    monkeypatch.setattr("tools.launchpad.tool.LaunchpadTool.launchpad_claim", _fake)
    from surfaces.telegram.claim_ops import claim_reply
    assert "do NOT retry" in await claim_reply("rob", ["0xabc"])


# ---------------------------------------------------------------------------
# /nft
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_nft_refuses_a_non_owner():
    from surfaces.telegram.nft_ops import nft_reply
    assert "Only the owner" in await nft_reply(None, ["list"])


@pytest.mark.asyncio
async def test_nft_usage_says_a_transfer_always_needs_approval():
    """An NFT is unpriceable, so the caps cannot bound it — the owner does.
    The seat must not imply his caps are the protection here."""
    from surfaces.telegram.nft_ops import USAGE, nft_reply
    out = await nft_reply("rob", [])
    assert out == USAGE
    assert "ALWAYS comes to you for approval" in out
    # There is deliberately no verb that GRANTS an approval.
    assert "revoke" in out and "grant" not in out.lower().split("revoke")[0]


@pytest.mark.asyncio
async def test_nft_transfer_is_a_dry_run_until_go(monkeypatch):
    seen = {}

    async def _fake(self, params, ctx):
        seen.update(dry_run=params.dry_run, token_id=params.token_id,
                    to=params.to, chain=params.chain)
        from types import SimpleNamespace
        return SimpleNamespace(error=None, extracted_content="report")

    monkeypatch.setattr("tools.defi.trade_tool.DefiTradeTool.nft_transfer", _fake)
    from surfaces.telegram.nft_ops import nft_reply
    out = await nft_reply("rob", ["transfer", "0xc", "7", "0xdead", "on", "base"])
    assert seen == {"dry_run": True, "token_id": 7, "to": "0xdead", "chain": "base"}
    assert "Add `go` to send it" in out and "irreversible" in out


@pytest.mark.asyncio
async def test_nft_info_needs_a_whole_token_id():
    from surfaces.telegram.nft_ops import nft_reply
    assert "whole number" in await nft_reply("rob", ["info", "0xc", "seven"])


@pytest.mark.asyncio
async def test_an_unknown_nft_verb_names_the_vocabulary():
    from surfaces.telegram.nft_ops import nft_reply
    out = await nft_reply("rob", ["frobnicate"])
    assert "Unknown /nft verb" in out and "revoke" in out


# ---------------------------------------------------------------------------
# /dapp
# ---------------------------------------------------------------------------

def test_dapp_refuses_a_non_owner():
    from surfaces.telegram.dapp_ops import dapp_reply
    assert "Only the owner" in dapp_reply(None, ["list"])


def _armed_store(tmp_path):
    """A box where a page HAS been armed at some point.

    ⚠️ Needed by every test below that exercises a verb's own branches: on a
    virgin data dir `/dapp` now answers "no record on this box" and stops,
    because a READ must not CREATE `dapp_sessions.db` (see
    `test_dapp_read_never_creates_the_store.py`). Without this the tests would
    be asserting the branch the guard exists to skip.
    """
    from core.dapp_session_store import DappSessionStore
    return DappSessionStore(str(tmp_path / "dapp_sessions.db"))


def test_dapp_empty_state_is_honest_about_what_it_read(tmp_path, monkeypatch):
    """"Nothing is connected" and "I could not look" are different facts."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    _armed_store(tmp_path)
    from surfaces.telegram.dapp_ops import dapp_reply
    out = dapp_reply("rob", ["list"])
    assert "No web page has been armed" in out
    assert "DURABLE record" in out


def test_dapp_revoke_needs_an_id(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    _armed_store(tmp_path)
    from surfaces.telegram.dapp_ops import dapp_reply
    assert "Usage: /dapp revoke" in dapp_reply("rob", ["revoke"])


def test_dapp_revoke_of_an_unknown_session_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    _armed_store(tmp_path)
    from surfaces.telegram.dapp_ops import dapp_reply
    out = dapp_reply("rob", ["revoke", "nope"])
    assert "No dapp session" in out


def test_dapp_revoke_marks_the_row_and_says_what_it_cannot_do(tmp_path, monkeypatch):
    """⚠️ The store never RESUMES a wallet, so revoking the row binds every
    future read and may not stop a bridge already armed in a running session.
    The reply must not report a kill it cannot guarantee."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    from core.dapp_session_store import DappSessionStore
    import core.dapp_session_store as store_mod
    store = DappSessionStore(str(tmp_path / "dapp_sessions.db"))
    store.save("s1", "rob", {"origin": "https://x", "chain": "base",
                             "spent_usd": 1.0, "sent": [], "refused": []})
    monkeypatch.setattr(store_mod, "get_dapp_session_store",
                        lambda db_path=None: store)
    from surfaces.telegram.dapp_ops import dapp_reply
    assert "https://x" in dapp_reply("rob", ["list"])
    out = dapp_reply("rob", ["revoke", "s1"])
    assert "revoked in my durable record" in out
    assert "may not stop one mid-flight" in out
    assert store.get("s1", "rob").revoked is True
    # Idempotent, and honest about being a no-op the second time.
    assert "already revoked" in dapp_reply("rob", ["revoke", "s1"])


# ---------------------------------------------------------------------------
# /identity
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_identity_refuses_a_non_owner(tmp_path):
    from surfaces.telegram.identity_ops import identity_reply
    assert "Only the owner" in await identity_reply(None, str(tmp_path),
                                                    ["register"])


@pytest.mark.asyncio
async def test_identity_usage_warns_that_registration_is_permanent(tmp_path):
    from surfaces.telegram.identity_ops import identity_reply
    out = await identity_reply("rob", str(tmp_path), [])
    assert "permanent" in out and "NOT repeatable" in out


@pytest.mark.asyncio
async def test_identity_register_refuses_when_one_already_exists(tmp_path, monkeypatch):
    """⚠️ `register()` is NOT idempotent: a second call mints a second token
    and leaves two agentIds with no authority between them."""
    called = []

    async def _fake(self, params, ctx):
        called.append(params)
        from types import SimpleNamespace
        return SimpleNamespace(error=None, extracted_content="minted")

    monkeypatch.setattr("tools.defi.trade_tool.DefiTradeTool.register_agent", _fake)
    monkeypatch.setattr("surfaces.telegram.identity_ops._existing_registration",
                        lambda data_dir: (42, None))
    from surfaces.telegram.identity_ops import identity_reply
    out = await identity_reply("rob", str(tmp_path), ["register", "go"])
    assert "already registered as agentId" in out and "42" in out
    assert not called, "a second registration must never reach the rail"


@pytest.mark.asyncio
async def test_identity_register_refuses_when_it_cannot_tell(tmp_path, monkeypatch):
    """An unreadable record is not evidence of being unregistered — and that
    reading is the one that mints a duplicate."""
    called = []

    async def _fake(self, params, ctx):
        called.append(params)
        from types import SimpleNamespace
        return SimpleNamespace(error=None, extracted_content="minted")

    monkeypatch.setattr("tools.defi.trade_tool.DefiTradeTool.register_agent", _fake)
    monkeypatch.setattr("surfaces.telegram.identity_ops._existing_registration",
                        lambda data_dir: (None, "record unreadable"))
    from surfaces.telegram.identity_ops import identity_reply
    out = await identity_reply("rob", str(tmp_path), ["register", "go"])
    assert "I will not register" in out
    assert not called


@pytest.mark.asyncio
async def test_identity_register_is_a_dry_run_until_go(tmp_path, monkeypatch):
    seen = {}

    async def _fake(self, params, ctx):
        seen["dry_run"] = params.dry_run
        from types import SimpleNamespace
        return SimpleNamespace(error=None, extracted_content="report")

    monkeypatch.setattr("tools.defi.trade_tool.DefiTradeTool.register_agent", _fake)
    monkeypatch.setattr("surfaces.telegram.identity_ops._existing_registration",
                        lambda data_dir: (None, None))
    from surfaces.telegram.identity_ops import identity_reply
    out = await identity_reply("rob", str(tmp_path), ["register"])
    assert seen["dry_run"] is True
    assert "Add `go` to mint it" in out and "Permanent" in out


@pytest.mark.asyncio
async def test_set_uri_without_a_registration_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr("surfaces.telegram.identity_ops._existing_registration",
                        lambda data_dir: (None, None))
    from surfaces.telegram.identity_ops import identity_reply
    out = await identity_reply("rob", str(tmp_path), ["set-uri"])
    assert "no registration on record" in out


# ---------------------------------------------------------------------------
# /contacts
# ---------------------------------------------------------------------------

def test_contacts_refuses_a_non_owner(tmp_path):
    from surfaces.telegram.contacts_ops import contacts_reply
    assert "Only the owner" in contacts_reply(None, str(tmp_path), [])


def test_contacts_never_creates_the_store_it_reads(tmp_path):
    """A READ never CREATES a store, and an absent record says so rather than
    printing an empty list that reads as "nobody"."""
    import os
    from surfaces.telegram.contacts_ops import contacts_reply
    out = contacts_reply("rob", str(tmp_path), [])
    assert "no conversation record on this box yet" in out
    assert not os.path.exists(os.path.join(str(tmp_path), "conversations.db"))


def test_contacts_half_an_address_is_refused_with_both_halves_named(tmp_path):
    from surfaces.telegram.contacts_ops import contacts_reply
    out = contacts_reply("rob", str(tmp_path), ["email"])
    assert "both halves" in out


def test_contacts_lists_then_renders_one_transcript(tmp_path):
    from core.surfaces.conversations import ConversationStore
    import os
    store = ConversationStore(os.path.join(str(tmp_path), "conversations.db"))
    store.record_outbound("rob", "email", "a@b.com", "are you there?")
    store.record_inbound("rob", "email", "a@b.com", "yes I am")
    from surfaces.telegram.contacts_ops import contacts_reply
    listing = contacts_reply("rob", str(tmp_path), [])
    assert "email:a@b.com" in listing
    one = contacts_reply("rob", str(tmp_path), ["email", "a@b.com"])
    assert "yes I am" in one
    # ⚠️ A third party's words are DATA, framed as such.
    assert "untrusted" in one


def test_contacts_is_tenant_scoped(tmp_path):
    from core.surfaces.conversations import ConversationStore
    import os
    store = ConversationStore(os.path.join(str(tmp_path), "conversations.db"))
    store.record_inbound("someone-else", "email", "x@y.com", "not yours")
    from surfaces.telegram.contacts_ops import contacts_reply
    out = contacts_reply("rob", str(tmp_path), [])
    assert "x@y.com" not in out
