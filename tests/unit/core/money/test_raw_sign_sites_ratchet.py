"""Ratchet: where raw signing and broadcast happen in shipped code.

Every site that signs a transaction or message, broadcasts raw bytes, or hands
a raw key/account to a third-party SDK is listed below with the gate that holds
it. A NEW site fails this test: route it through an existing gated site
(``tx_guard.authorize`` on EVM, ``_solana_turn_gate``/``authorize_spend`` on
Solana) or add a row here with the gate you verified. Shrink-only: a row whose
site is gone must be deleted.

Sinks (AST, attribute reference or call, so ``to_thread(rail.sign_and_send, tx)``
counts): ``sign_transaction``, ``sign_transaction_with``, ``send_raw_transaction``,
``send_raw``, ``sign_and_send``, ``sign_message``, ``sign_typed_data``,
``unsafe_sign_hash``, ``sign_hash``, the SVM wrapper ``_solana_send``; a call
whose first argument is the RPC method ``sendTransaction`` /
``eth_sendRawTransaction``; ``EthAccountSigner(...)``; ``Account.from_key(...)`` / ``w3.eth.account.from_key(...)``.

Blind spot: a third-party SDK that receives an account (x402, Hyperliquid)
signs inside the SDK; the handoff is the pinned site.
"""
import ast
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[4]
ROOTS = ("core", "modules", "agents", "tools", "api", "cli", "surfaces",
         "webview", "cron", "packs")
SKIP_PARTS = {"tests", "__pycache__", "node_modules", ".venv", "venv", "build", "dist"}
SINK_ATTRS = {"sign_transaction", "sign_transaction_with", "send_raw_transaction",
              "send_raw", "sign_and_send", "sign_message", "sign_typed_data",
              "unsafe_sign_hash", "sign_hash", "_solana_send"}
RPC_METHODS = {"sendTransaction", "eth_sendRawTransaction"}

_SIGNER = "the signer implementation itself (the seam every gated caller reaches)"
_SIGNER_SVC = "polyrob-signer op: re-runs tx_guard / its own hard caps and ledger (core/signer/caps.py)"
_TX_GUARD = "after tx_guard.authorize allowed it, inside gate.reserve()"

#: (path, enclosing qualname, sink) -> why it is gated. Shrink-only.
ALLOWED = {
    # -- the signer process (066 P2) --------------------------------------
    ("core/signer/server.py", "SignerService._op_evm_send", "sign_and_send"): _SIGNER_SVC,
    ("core/signer/server.py", "SignerService._op_deposit_sweep", "sign_and_send"): (
        "deposit-address sweep to the destination pinned in signer.toml"),
    ("core/signer/server.py", "SignerService._op_x402_authorize", "sign_typed_data"): (
        "EIP-3009 authorization under the signer's x402 caps"),
    ("core/signer/server.py", "SignerService._op_eip8004_feedback_auth", "sign_message"): (
        "signs ONLY the EIP-8004 FeedbackAuth typed shape; not a payment"),
    ("core/signer/server.py", "SignerService._op_journal_sign", "sign_message"): (
        "EIP-191 over ONLY the account journal template (is_journal_template); a _MONEY_OPS op, "
        "so refused under the signer's pause; not a payment"),
    ("core/signer/shadow.py", "ShadowEvmSigner.sign_message", "sign_message"): _SIGNER,
    ("core/signer/shadow.py", "ShadowEvmSigner.sign_transaction", "sign_transaction"): _SIGNER,
    ("core/signer/shadow.py", "ShadowEvmSigner.sign_typed_data", "sign_typed_data"): _SIGNER,
    # -- in-process signers and rails -------------------------------------
    ("core/wallet/signer.py", "LocalEoaSigner.__init__", "from_key"): _SIGNER,
    ("core/wallet/signer.py", "LocalEoaSigner.sign_message", "sign_message"): _SIGNER,
    ("core/wallet/signer.py", "LocalEoaSigner.sign_typed_data", "sign_message"): _SIGNER,
    ("core/wallet/signer.py", "LocalEoaSigner.sign_transaction", "sign_transaction"): (
        "signer; refuses a transaction with no chainId"),
    ("core/wallet/solana_signer.py", "SolanaSigner.sign_message", "sign_message"): _SIGNER,
    ("core/wallet/solana_x402.py", "SolanaX402Signer.sign_transaction", "sign_message"): (
        "x402 SVM partial-sign of the payer slot; the x402 service caps the payment"),
    ("core/wallet/solana_x402.py", "SolanaX402Signer.sign_transaction", "sign_transaction"): (
        "delegates to SolanaSigner, which refuses a foreign fee payer"),
    ("core/wallet/broadcast/evm.py", "EvmRail.sign_and_send", "sign_transaction"): (
        "the EVM rail: every caller below is gated; journals before broadcast"),
    ("core/wallet/broadcast/evm.py", "EvmRail.sign_and_send", "eth_sendRawTransaction"): (
        "the EVM rail broadcast"),
    ("core/wallet/solana_rail.py", "SolanaRail.send_raw", "sendTransaction"): (
        "the SVM rail broadcast: refuses unsigned/durable-nonce bytes; journals first"),
    ("core/wallet/nft_account.py", "sign_entry", "sign_message"): (
        "EIP-191 journal attestation with a fixed prefix; not a transaction or permit"),
    # -- modules -------------------------------------------------------------
    ("modules/eip8004/reputation.py", "ReputationManager.create_feedback_auth", "sign_message"): (
        "off-chain EIP-8004 feedback signature, not a payment"),
    ("modules/payments/treasury_sweeper.py", "TreasurySweeper._submit_sweep", "sign_transaction"): (
        "deposit-address sweeps to TREASURY_ADDRESS; TREASURY_SWEEPER_ENABLED (default off)"),
    ("modules/payments/treasury_sweeper.py", "TreasurySweeper._submit_sweep", "send_raw_transaction"): (
        "same sweep; chain id and derived address re-asserted, journaled first"),
    ("modules/payments/treasury_sweeper.py", "TreasurySweeper._sweep_deposit", "from_key"): (
        "same sweep; derived key must equal the recorded deposit address"),
    ("modules/payments/wallet_generator.py", "DepositWalletGenerator.generate_deposit_address", "from_key"): (
        "derives a deposit ADDRESS; signs nothing"),
    ("modules/payments/wallet_generator.py", "DepositWalletGenerator.get_account_for_sweep", "from_key"): (
        "feeds the sweeper only (TREASURY_SWEEPER_ENABLED)"),
    # -- EVM money verbs: tx_guard.authorize --------------------------------
    ("tools/defi/trade_tool.py", "DefiTradeTool._run_guarded", "sign_and_send"): _TX_GUARD,
    ("tools/defi/trade_tool.py", "DefiTradeTool.transfer", "sign_and_send"): _TX_GUARD,
    ("tools/defi/trade_tool.py", "DefiTradeTool.wrap", "sign_and_send"): _TX_GUARD,
    ("tools/defi/trade_tool.py", "DefiTradeTool.unwrap", "sign_and_send"): _TX_GUARD,
    ("tools/defi/call_verb.py", "perform_call", "sign_and_send"): _TX_GUARD,
    ("tools/defi/deploy_verb.py", "_perform_deploy", "sign_and_send"): (
        _TX_GUARD + "; refuses without a measured gas figure"),
    ("tools/defi/lp_verbs.py", "_perform", "sign_and_send"): (
        _TX_GUARD + "; pause re-checked via authorize_spend right before signing"),
    ("tools/defi/bridge_evm_leg.py", "EvmOriginLeg.send", "sign_and_send"): (
        "bridge EVM origin: prepare() ran tx_guard; perform_bridge holds the owner "
        "queue above the ceiling and re-checks gate.check inside gate.reserve()"),
    ("tools/launchpad/execute.py", "guarded_send", "sign_and_send"): _TX_GUARD,
    ("tools/agent_nft/guarded.py", "guarded_call", "sign_and_send"): _TX_GUARD,
    ("tools/agent_nft/reveal.py", "run", "sign_and_send"): _TX_GUARD,
    ("tools/dapp_browser/bridge.py", "WalletBridge._attempt", "sign_and_send"): (
        _TX_GUARD + "; page value declared as the only outflow, session budget"),
    # -- SVM money verbs ---------------------------------------------------
    ("tools/defi/trade_tool.py", "DefiTradeTool._solana_send", "sign_transaction"): (
        "the SVM wrapper; callers are pinned below"),
    ("tools/defi/trade_tool.py", "DefiTradeTool._solana_send", "send_raw"): (
        "the SVM wrapper; callers are pinned below"),
    ("tools/defi/trade_tool.py", "DefiTradeTool.solana_swap", "_solana_send"): (
        "after _solana_turn_gate, simulation deltas and gate.check"),
    ("tools/defi/solana_send_verb.py", "perform_solana_transfer", "_solana_send"): (
        "after _solana_turn_refusal (the _solana_turn_gate mirror), simulation and gate.check"),
    ("tools/defi/bridge_verb.py", "perform_bridge", "_solana_send"): (
        "after _solana_turn_refusal, simulation, owner queue above the ceiling, gate.check"),
    ("tools/defi/spl_deploy_verb.py", "perform_solana_deploy_token", "sign_transaction_with"): (
        "after authorize_spend + _solana_turn_refusal, simulation, gate.check, ceiling"),
    ("tools/defi/spl_deploy_verb.py", "perform_solana_deploy_token", "send_raw"): (
        "same as the sign above"),
    # -- raw account handed to a signing SDK --------------------------------
    ("tools/x402/real_client.py", "RealX402Client._to_sdk_signer", "EthAccountSigner"): (
        "x402 SDK: max_amount policy, network + canonical-USDC pin, service PolicyGate"),
    ("tools/x402/real_client.py", "RealX402Client.fetch_with_payment", "EthAccountSigner"): (
        "same as _to_sdk_signer"),
    ("packs/markets/polyrob_markets/hyperliquid/service.py", "HyperliquidTool._get_exchange_client", "from_key"): (
        "Hyperliquid SDK account; every order verb runs trade_turn_refusal + evaluate_live_trade"),
    ("packs/markets/polyrob_markets/hyperliquid/service.py", "HyperliquidTool.approve_agent", "from_key"): (
        "master-key agent approval after trade_turn_refusal"),
}


def _sites(path: pathlib.Path):
    tree = ast.parse(path.read_text(errors="ignore"))
    out = set()

    def visit(node, stack):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(ch, stack + [ch.name])
                continue
            where = ".".join(stack) or "<module>"
            if isinstance(ch, ast.Attribute) and ch.attr in SINK_ATTRS:
                out.add((where, ch.attr))
            if isinstance(ch, ast.Call):
                f = ch.func
                if (ch.args and isinstance(ch.args[0], ast.Constant)
                        and ch.args[0].value in RPC_METHODS):
                    out.add((where, ch.args[0].value))
                name = f.id if isinstance(f, ast.Name) else (
                    f.attr if isinstance(f, ast.Attribute) else None)
                if name == "EthAccountSigner":
                    out.add((where, "EthAccountSigner"))
                owner = getattr(f, "value", None)
                if (isinstance(f, ast.Attribute) and f.attr == "from_key" and (
                        (isinstance(owner, ast.Name) and owner.id == "Account")
                        or (isinstance(owner, ast.Attribute) and owner.attr == "account"))):
                    out.add((where, "from_key"))
            visit(ch, stack)

    visit(tree, [])
    return out


def _found():
    found = set()
    for root in ROOTS:
        for p in (REPO / root).rglob("*.py"):
            rel = p.relative_to(REPO)
            if SKIP_PARTS.intersection(rel.parts) or rel.name.startswith("test_"):
                continue
            for where, sink in _sites(p):
                found.add((rel.as_posix(), where, sink))
    return found


def test_raw_sign_sites_are_allowlisted():
    new = sorted(_found() - set(ALLOWED))
    assert not new, (
        "new raw signing/broadcast site(s) — route through a gated site "
        "(tx_guard.authorize / _solana_turn_gate / authorize_spend) or add a "
        f"row to ALLOWED with the gate you verified: {new}")


def test_the_allowlist_only_shrinks():
    stale = sorted(set(ALLOWED) - _found())
    assert not stale, f"delete these rows (the site is gone or renamed): {stale}"


def test_every_row_names_its_gate():
    assert all(isinstance(v, str) and v.strip() for v in ALLOWED.values())
