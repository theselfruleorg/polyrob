"""The EIP-1193 provider injected into a dapp page (042).

⚠️ No key, no signing and no policy live here. This script is a POSTBOX: it
forwards every JSON-RPC request to Python over a Playwright binding and returns
what Python decides. A page that tampers with it can only lie to itself — the
wallet's answers are computed on the other side of the binding, where the guard
is.

EIP-6963 (``announceProvider``) matters as much as ``window.ethereum``: a modern
dapp discovers wallets by listening for that event, and a provider that only
sets the legacy global is invisible to half the ecosystem.
"""

#: The page-side binding Playwright exposes. Deliberately obscure — a dapp that
#: calls it directly gets exactly the same guarded path as one going through the
#: provider, so the only cost of it being reachable is that it is ugly.
BINDING = "__polyrobWalletRequest"


def provider_script(*, address: str, chain_id_hex: str, binding: str = BINDING) -> str:
    """The init script for one armed session."""
    return """
(() => {
  const ADDRESS = %(address)s;
  const CHAIN_ID = %(chain)s;
  const listeners = {};

  const emit = (event, payload) => {
    (listeners[event] || []).forEach((fn) => { try { fn(payload); } catch (e) {} });
  };

  const provider = {
    isMetaMask: false,
    isPolyrob: true,
    chainId: CHAIN_ID,
    networkVersion: String(parseInt(CHAIN_ID, 16)),
    selectedAddress: ADDRESS,
    _metamask: { isUnlocked: () => Promise.resolve(true) },

    async request(args) {
      const method = (args && args.method) || '';
      const params = (args && args.params) || [];
      // Answered in the page for latency only. Both are echoes of what Python
      // already told us at injection time, never a second source of truth:
      // every method that can MOVE anything goes across the binding.
      if (method === 'eth_accounts' || method === 'eth_requestAccounts') return [ADDRESS];
      if (method === 'eth_chainId') return provider.chainId;
      if (method === 'net_version') return provider.networkVersion;

      const reply = await window.%(binding)s(JSON.stringify({ method, params }));
      const parsed = typeof reply === 'string' ? JSON.parse(reply) : reply;
      if (parsed && parsed.error) {
        const err = new Error(parsed.error.message || 'request rejected');
        err.code = parsed.error.code || 4001;
        err.data = parsed.error.data;
        throw err;
      }
      if (method === 'wallet_switchEthereumChain' && parsed && parsed.chainId) {
        provider.chainId = parsed.chainId;
        provider.networkVersion = String(parseInt(parsed.chainId, 16));
        emit('chainChanged', provider.chainId);
        return null;
      }
      return parsed ? parsed.result : null;
    },

    // Legacy shims. Plenty of live dapps still call these.
    send(a, b) {
      if (typeof a === 'string') return provider.request({ method: a, params: b || [] });
      if (typeof b === 'function') return provider.sendAsync(a, b);
      return provider.request(a);
    },
    sendAsync(payload, callback) {
      provider.request(payload)
        .then((result) => callback(null, { id: payload.id, jsonrpc: '2.0', result }))
        .catch((error) => callback(error, null));
    },
    enable() { return provider.request({ method: 'eth_requestAccounts' }); },
    on(event, handler) { (listeners[event] = listeners[event] || []).push(handler); return provider; },
    addListener(event, handler) { return provider.on(event, handler); },
    removeListener(event, handler) {
      listeners[event] = (listeners[event] || []).filter((fn) => fn !== handler);
      return provider;
    },
    removeAllListeners(event) {
      if (event) { delete listeners[event]; } else { Object.keys(listeners).forEach((k) => delete listeners[k]); }
      return provider;
    },
    isConnected() { return true; },
  };

  try {
    Object.defineProperty(window, 'ethereum', {
      value: provider, writable: false, configurable: true,
    });
  } catch (e) { window.ethereum = provider; }
  window.web3 = { currentProvider: provider };

  // EIP-6963. A dapp that only listens for this never sees window.ethereum.
  const detail = Object.freeze({
    info: Object.freeze({
      uuid: '7b3f9d10-0c21-4f0a-9a5e-6f2b8c4d1e90',
      name: 'POLYROB Agent Wallet',
      rdns: 'dev.polyrob.wallet',
      icon: 'data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciLz4=',
    }),
    provider,
  });
  const announce = () => window.dispatchEvent(
    new CustomEvent('eip6963:announceProvider', { detail }));
  window.addEventListener('eip6963:requestProvider', announce);
  announce();

  window.dispatchEvent(new Event('ethereum#initialized'));
  emit('connect', { chainId: provider.chainId });
})();
""" % {
        "address": _js_string(address),
        "chain": _js_string(chain_id_hex),
        "binding": binding,
    }


def _js_string(value: str) -> str:
    """A JSON-safe JS string literal. Never f-string interpolation into JS."""
    import json
    return json.dumps(str(value))
