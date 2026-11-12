"""115 W6 — the account view says whether the identity publishes this agent's card: the
registry's ``tokenURI(agentId)`` against the file ``agent_nft_bind_identity`` writes. Yes, no,
or UNREADABLE (never "no" for an unread file); no identity, no line."""
from core.wallet import abi, erc8004, identity_card
from tests.unit.tools.test_agent_nft_reputation import RepChain, _snapshot
from tests.unit.tools.test_agent_nft_polyrob_view import armed  # noqa: F401

REGISTRY = erc8004.resolve_identity_registry("robinhood")


class CardChain(RepChain):
    def __init__(self, owner, *, uri=None, broken=False, **kw):
        super().__init__(owner, **kw)
        self.uri, self.uri_broken = uri, broken

    def __call__(self, method, params, *a, **k):
        if (method == "eth_call" and params[0]["to"].lower() == REGISTRY.lower()
                and params[0]["data"].startswith(identity_card.TOKEN_URI_SELECTOR)):
            if self.uri_broken:
                raise RuntimeError("rpc down")
            return "0x" + abi.encode([{"type": "string"}], [self.uri or "data:,other"]).hex()
        return super().__call__(method, params, *a, **k)


def _account(chain_obj, signer):
    from core.wallet import collection_registry
    (profile,) = collection_registry.profiles()
    account, _v = profile.account_of(3)
    return account


def test_the_published_card_reads_yes(armed):  # noqa: F811
    from tools.defi.agent_registration import account_agent_uri
    account = _account(None, armed)
    card = account_agent_uri(4663, account, agent_id=41, registry=REGISTRY)
    text = _snapshot(armed, CardChain(armed.address, uri=card))
    assert "card:      published: yes" in text


def test_another_file_reads_no_and_names_the_verb(armed):  # noqa: F811
    text = _snapshot(armed, CardChain(armed.address))
    assert "card:      published: no" in text and "agent_nft_bind_identity" in text


def test_an_unread_card_is_unreadable_never_no(armed):  # noqa: F811
    text = _snapshot(armed, CardChain(armed.address, broken=True))
    assert "card:      unreadable" in text and "published: no" not in text


def test_no_identity_no_card_line(armed):  # noqa: F811
    text = _snapshot(armed, CardChain(armed.address, has=False))
    assert "card:" not in text
