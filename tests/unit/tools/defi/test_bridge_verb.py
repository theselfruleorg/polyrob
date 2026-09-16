"""The bridge verb's policy: CAPS, NOT TAPS (owner decision, 2026-09-12).

⚠️ This docstring described the SUPERSEDED 2026-09-11 shape — "owner-only, always
approved, no cap" — for as long as the tests below contradicted it. The owner used
that shape and rejected it: an agent that can only tell its owner to bridge for
himself is a rail he operates by hand, not autonomy with a safety margin.

What bounds a bridge now is what bounds every other money verb: the per-transaction
ceiling, the rolling daily cap, the simulated and asserted deltas, and the owner
queue above `DEFI_AUTONOMOUS_MAX_USD`. Two refusals survive because neither is about
autonomy — a delegated sub-agent never moves money, and a correspondent-tainted
session never reaches a money verb at all.
"""
import asyncio
from types import SimpleNamespace

import pytest

from tools.defi import bridge_verb as bv


@pytest.fixture(autouse=True)
def bound_wallet_owner(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner")


class _Tool:
    def __init__(self):
        self.results = []

    def _ar(self, *, content=None, error=None):
        r = SimpleNamespace(content=content, error=error)
        self.results.append(r)
        return r

    def _get_wallet(self):
        return None


def _run(coro):
    return asyncio.run(coro)


def _params(**over):
    d = dict(from_chain="solana", to_chain="robinhood", amount=0.5,
             token_in="native", token_out="native", dry_run=True)
    d.update(over)
    return SimpleNamespace(**d)


def test_disarmed_by_default(monkeypatch):
    monkeypatch.delenv(bv.FLAG, raising=False)
    r = _run(bv.perform_bridge(_Tool(), _params(), None))
    assert "bridging is off" in r.error


def test_an_autonomous_turn_is_ALLOWED_and_bounded_by_the_cap(monkeypatch):
    """SUPERSEDES the 2026-09-11 blanket refusal.

    The owner used that shape and rejected it on 2026-09-12: an agent that can
    only tell its owner to bridge for himself is a rail he operates by hand. An
    autonomous turn now reaches the verb; the per-tx ceiling, the daily cap and
    the asserted deltas bound it instead.

    Asserted by what it no longer says — it passes the turn gate and refuses
    later, on the stub wallet."""
    monkeypatch.setenv(bv.FLAG, "true")
    monkeypatch.setattr("core.wallet.tx_guard._halted", lambda: False)
    monkeypatch.setattr("core.autonomy_control.allows",
                        lambda kind: SimpleNamespace(allowed=True, reason="ok"))
    r = _run(bv.perform_bridge(_Tool(), _params(dry_run=False), SimpleNamespace(user_id="owner")))
    assert "NEVER runs on an autonomous" not in (r.error or "")
    assert "owner-only by design" not in (r.error or "")


def test_a_sub_agent_may_not_bridge(monkeypatch):
    monkeypatch.setenv(bv.FLAG, "true")
    monkeypatch.setattr("tools.controller.turn_origin._is_forged_or_autonomous_turn",
                        lambda ctx, _: False)
    ctx = SimpleNamespace(role="leaf", is_sub_agent=True)
    r = _run(bv.perform_bridge(_Tool(), _params(), ctx))
    assert "delegated sub-agent" in r.error


def test_the_031_pause_stops_a_REAL_bridge(monkeypatch):
    """`spend` sits under the `all` scope; an owner stop has to stop this too."""
    monkeypatch.setenv(bv.FLAG, "true")
    monkeypatch.setattr("tools.controller.turn_origin._is_forged_or_autonomous_turn",
                        lambda ctx, _: False)
    monkeypatch.setattr("core.wallet.tx_guard._halted", lambda: False)
    monkeypatch.setattr("core.autonomy_control.allows",
                        lambda kind: SimpleNamespace(allowed=False,
                                                     reason="paused (all) by owner"))
    r = _run(bv.perform_bridge(_Tool(), _params(dry_run=False), SimpleNamespace(user_id="owner")))
    assert "paused" in r.error and "Nothing was broadcast" in r.error


def test_the_pause_does_NOT_block_a_dry_run(monkeypatch):
    """Found by the first prod dry run: with the owner's stop in force, the verb
    refused to even QUOTE. That is "I stopped you, now I cannot see anything" —
    the owner could not learn what a bridge would cost while deciding whether to
    resume. A dry run returns before anything is signed, so blocking it buys no
    safety (the rule `spend_lane.py` states for the same reason)."""
    monkeypatch.setenv(bv.FLAG, "true")
    monkeypatch.setattr("tools.controller.turn_origin._is_forged_or_autonomous_turn",
                        lambda ctx, _: False)

    def _must_not_be_called():
        raise AssertionError("the pause gate ran on a dry run")
    monkeypatch.setattr("core.wallet.tx_guard._halted", _must_not_be_called)
    # It gets past the pause and refuses later, on the stub wallet — which is
    # exactly the point: the pause is no longer what stopped it.
    r = _run(bv.perform_bridge(_Tool(), _params(dry_run=True), SimpleNamespace(user_id="owner")))
    assert r.error is None or "paused" not in (r.error or "")
    assert r.error is None or "HALTED" not in (r.error or "")


def test_a_pause_probe_failure_fails_closed(monkeypatch):
    """On a REAL run. A dry run never reaches this gate (see the test above)."""
    monkeypatch.setenv(bv.FLAG, "true")
    monkeypatch.setattr("tools.controller.turn_origin._is_forged_or_autonomous_turn",
                        lambda ctx, _: False)
    monkeypatch.setattr("core.wallet.tx_guard._halted", lambda: False)

    def boom(kind):
        raise RuntimeError("unreadable")
    monkeypatch.setattr("core.autonomy_control.allows", boom)
    r = _run(bv.perform_bridge(_Tool(), _params(dry_run=False), SimpleNamespace(user_id="owner")))
    assert "failing closed" in r.error


def test_an_erc20_ORIGIN_is_refused(monkeypatch):
    """The origin restriction is load-bearing: an ERC-20 origin adds an approve
    step whose spender is a third party's address."""
    monkeypatch.setenv(bv.FLAG, "true")
    monkeypatch.setattr("tools.controller.turn_origin._is_forged_or_autonomous_turn",
                        lambda ctx, _: False)
    monkeypatch.setattr("core.wallet.tx_guard._halted", lambda: False)
    monkeypatch.setattr("core.autonomy_control.allows",
                        lambda kind: SimpleNamespace(allowed=True, reason="ok"))
    r = _run(bv.perform_bridge(_Tool(), _params(token_in="usdc"), SimpleNamespace(user_id="owner")))
    assert "ORIGIN must be the chain's native asset" in r.error
    assert "allowance leg" in r.error


def test_an_erc20_DESTINATION_is_allowed_but_only_from_the_pinned_registry():
    """Receiving a token grants nobody anything — the only thing it changes is
    which balance phase 2 measures. But the asset must be one this wallet has
    verified on-chain; a model-supplied address was not.
    """
    from core.wallet import chains
    native, label = bv.resolve_dest_currency("native", "robinhood")
    assert native == "0x0000000000000000000000000000000000000000"
    assert label == "native"

    weth, label = bv.resolve_dest_currency("weth", "robinhood")
    assert weth == chains.get("robinhood").wrapped_native
    assert label == "weth"

    with pytest.raises(ValueError) as exc:
        bv.resolve_dest_currency("0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
                                 "robinhood")
    assert "not a pinned asset" in str(exc.value)
    assert "verified on-chain" in str(exc.value)


def test_a_destination_chain_outside_the_registry_resolves_no_token():
    with pytest.raises(ValueError) as exc:
        bv.resolve_dest_currency("weth", "nosuchchain")
    assert "pinned registry" in str(exc.value)


def test_an_unpinned_asset_on_a_known_chain_names_what_IS_allowed():
    """A refusal that does not say what would work is a dead end."""
    with pytest.raises(ValueError) as exc:
        bv.resolve_dest_currency("shib", "robinhood")
    msg = str(exc.value)
    assert "weth" in msg and "native" in msg
    # robinhood pins no USDC (its stablecoin is USDG) — the refusal must not
    # offer an asset that chain does not have.
    assert "usdc" not in msg


def test_an_approval_path_that_errors_denies(monkeypatch):
    """The one gate between an agent turn and the whole treasury."""
    class _Boom:
        def __init__(self, **kw):
            raise RuntimeError("queue unavailable")
    monkeypatch.setattr("tools.controller.approval_queue.OwnerQueueApprover", _Boom)
    ok, note = _run(bv._require_owner_approval(_Tool(), params_summary={},
                                               execution_context=None))
    assert ok is False and "failing closed" in note


def test_a_denied_approval_says_how_to_grant_it(monkeypatch):
    class _Deny:
        def __init__(self, **kw):
            pass

        async def request(self, *a, **k):
            return False
    monkeypatch.setattr("tools.controller.approval_queue.OwnerQueueApprover", _Deny)
    ok, note = _run(bv._require_owner_approval(_Tool(), params_summary={},
                                               execution_context=None))
    assert ok is False and "/approve" in note


def test_the_bridge_has_exactly_ONE_owner_gate():
    """SUPERSEDES the belt-and-braces row (039).

    The bridge used to sit in DEFAULT_APPROVAL_REQUIRED_TOOLS *and*
    PAYMENT_APPROVAL_TOOLS *and* carry its own owner queue. On 2026-09-12 the
    owner tapped three times for one bridge, reading two prompts that described
    the same transaction differently, then a third message telling him it had
    been "auto-approved … within caps". Belt-and-braces on an approval prompt is
    not redundancy, it is the owner doing the same job twice with less
    information each time.
    """
    from core.config_policy.payment_tools import (PAYMENT_APPROVAL_TOOLS,
                                                  VERB_OWNED_APPROVAL_GATES)
    from tools.controller.approval import DEFAULT_APPROVAL_REQUIRED_TOOLS
    assert "defi_trade_bridge" not in DEFAULT_APPROVAL_REQUIRED_TOOLS
    assert "defi_trade_bridge" not in PAYMENT_APPROVAL_TOOLS
    assert "defi_trade_bridge" in VERB_OWNED_APPROVAL_GATES


def test_describe_quote_always_names_the_arrival_floor():
    q = SimpleNamespace(amount_in_formatted=0.5, symbol_in="SOL", origin_chain_id=1,
                        symbol_out="ETH", dest_chain_id=4663, amount_in_usd=50.9,
                        amount_out_formatted=0.0197, amount_out_usd=50.6,
                        min_out_formatted=0.0193, impact_pct=-0.66,
                        time_estimate_sec=1, recipient="0xr", request_id="0xabc")
    text = bv.describe_quote(q)
    assert "ARRIVAL FLOOR" in text and "0.01930000" in text


def test_broadcasting_requires_a_pinned_solana_rpc(monkeypatch):
    """The `solana_swap` mirror: a shared public endpoint cannot be the trust
    anchor for moving funds across a chain boundary."""
    import inspect

    from tools.defi import bridge_verb
    src = inspect.getsource(bridge_verb.perform_bridge)
    assert 'DEFI_SOLANA_RPC' in src
    assert 'Dry runs are unaffected' in src


def test_an_unvalued_outflow_refuses_rather_than_booking_zero():
    """Booking an unknown as $0.00 silently widens every OTHER money verb's
    remaining cap by the size of this bridge (the 033 confident-zero class)."""
    import inspect

    from tools.defi import bridge_verb
    src = inspect.getsource(bridge_verb.perform_bridge)
    assert "cannot be booked honestly" in src
    assert "amount_usd is None and not params.dry_run" in src


def test_the_origin_leg_is_confirmed_before_waiting_on_an_arrival():
    """A send whose blockhash expired never happened. Polling the destination
    for the full deadline and then reporting `in_flight` says 'your money is
    between two chains' about a transaction that does not exist — frightening,
    wrong, and the answer most likely to tempt a re-send."""
    import inspect

    from tools.defi import bridge_verb
    src = inspect.getsource(bridge_verb.perform_bridge)
    assert "_solana_confirm" in src
    assert "REVERTED ON-CHAIN" in src
    assert "expired blockhash" in src
    # and it must happen BEFORE phase 2
    assert src.index("_solana_confirm") < src.index("await_arrival")


def test_the_cli_reads_the_real_ActionResult_field():
    """`.content` does not exist on ActionResult; the field is
    `extracted_content`. Reading the wrong one printed '(no output)' over a
    complete bridge report on the first prod run."""
    import inspect

    from tools.controller.types import ActionResult
    assert not hasattr(ActionResult(extracted_content="x"), "content")

    from cli.commands import wallet
    src = inspect.getsource(wallet.wallet_bridge.callback)
    assert "extracted_content" in src
    assert 'getattr(result, "content"' not in src


def test_the_bridge_widens_the_program_allowlist_from_a_PINNED_constant():
    """Taking the program id from the quote we are vetting would make the check
    vacuous. It must come from the pinned constant."""
    import inspect

    from tools.defi import bridge_verb
    src = inspect.getsource(bridge_verb.perform_bridge)
    assert "RELAY_PROGRAM_IDS" in src
    assert "extra_allowed=RELAY_PROGRAM_IDS" in src
    # never from the payload
    assert "extra_allowed=quote" not in src


def test_an_under_move_is_refused_not_only_an_over_move():
    """The classic check catches moving MORE than declared. Only that would have
    passed a transaction moving 5,000 lamports of a declared 900,000,000 —
    precisely what a mis-measured simulation looked like on prod."""
    import inspect

    from tools.defi import bridge_verb
    src = inspect.getsource(bridge_verb.perform_bridge)
    assert "moves only" in src
    assert "outflow < amount_in_raw" in src


def test_an_ungranted_approval_never_invents_a_handle(monkeypatch):
    """Live on prod 2026-09-12: holding no tap id, the agent told its owner to run
    `/approve 0x1789221782309b...` — the RELAY REQUEST ID, which matches nothing.
    The old note said "`/approve <id>` grants it" and the agent substituted the
    only id it had. The note must name the handle's SHAPE and where to read it."""
    class _Deny:
        def __init__(self, **kw):
            pass

        async def request(self, *a, **k):
            return False
    monkeypatch.setattr("tools.controller.approval_queue.OwnerQueueApprover", _Deny)
    ok, note = _run(bv._require_owner_approval(_Tool(), params_summary={},
                                               execution_context=None))
    assert ok is False
    assert "tap-" in note
    assert "/pending" in note
    assert "NOT the relay request id" in note
    assert "durable" in note.lower()
