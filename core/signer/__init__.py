"""066 Phase 2 — ``polyrob-signer``: the one process that holds the wallet seed.

The agent sends INTENTS, never bytes to sign blindly. The signer re-runs
``core.wallet.tx_guard.authorize`` (the SAME module — there is no second guard)
on its own pinned RPC, against its own hard caps (``/etc/polyrob/signer.toml``)
and its own ledger (``/var/lib/polyrob-signer``), then signs and broadcasts. A
shape it does not know is refused; there is no ``sign_message``,
``sign_typed_data`` or ``sign_raw`` on the wire.

``WALLET_SIGNER`` selects how the agent process signs (default ``local``):

* ``local``  — today: the agent holds the seed and signs in process. Nothing in
  this package runs.
* ``shadow`` — the agent still signs locally, AND asks the signer for a verdict
  on the same request. A disagreement is logged (``signer_shadow.jsonl``) and
  shown in the custody status section. The signer itself never blocks a send,
  but its caps are the envelope: the agent's gate clamps to them
  (``core.wallet.signer_envelope``).
* ``remote`` — the agent holds no key. ``AgentWallet`` is a
  :class:`core.signer.remote.RemoteWallet`; every signature comes from the
  signer over ``/run/polyrob-signer/signer.sock``.

Modules:

* :mod:`core.signer.protocol` — framing, the versioned request schema, the
  strict intent/tx codecs (an unknown field is an unknown shape).
* :mod:`core.signer.caps` — ``signer.toml``: hard caps, chains, clients.
* :mod:`core.signer.store` — the signer's own nonce journal, approvals, pause.
* :mod:`core.signer.server` — :class:`SignerService` (pure ``handle``) and the
  Unix-socket server with the ``SO_PEERCRED`` check.
* :mod:`core.signer.client` — the agent-side socket client.
* :mod:`core.signer.attest` — the in-process hand-off from ``tx_guard`` to the
  signing point (which intent a transaction was authorized under).
* :mod:`core.signer.remote` / :mod:`core.signer.shadow` — the two agent modes.
"""
import logging
import os

logger = logging.getLogger(__name__)

MODE_LOCAL = "local"
MODE_SHADOW = "shadow"
MODE_REMOTE = "remote"
MODES = (MODE_LOCAL, MODE_SHADOW, MODE_REMOTE)

#: The signer's socket. The server reads its own path from ``signer.toml``; the
#: agent uses this constant (tests monkeypatch it).
DEFAULT_SOCKET = "/run/polyrob-signer/signer.sock"
DEFAULT_CONFIG = "/etc/polyrob/signer.toml"
DEFAULT_STATE_DIR = "/var/lib/polyrob-signer"

_warned = set()


def signer_mode() -> str:
    """``WALLET_SIGNER`` = ``local|shadow|remote``, default ``local``.

    An unknown value is ``local`` and logs once. ``local`` is the safe reading:
    after the cut-over the agent unit no longer loads ``wallet.env``, so a typo
    there yields a PUBLIC-ONLY wallet that refuses to sign, never a second key
    holder.
    """
    raw = (os.environ.get("WALLET_SIGNER") or "").strip().lower()
    if not raw:
        return MODE_LOCAL
    if raw in MODES:
        return raw
    if raw not in _warned:
        _warned.add(raw)
        logger.warning("WALLET_SIGNER=%r is not one of %s; using local", raw, "|".join(MODES))
    return MODE_LOCAL


def socket_path() -> str:
    """Where the agent looks for the signer (one constant; see DEFAULT_SOCKET)."""
    return DEFAULT_SOCKET
