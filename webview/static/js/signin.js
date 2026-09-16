/**
 * signin.js — the multitenant wallet sign-in (SIWE) page.
 *
 * De-inlined from signin.html (043 phase 5, R6): the console CSP drops
 * `'unsafe-inline'` from `script-src`, so the two former inline blocks (the
 * ethers.js loader with a CDN fallback, and the connect/verify flow) ride this
 * one external module loaded with `src=`. Behaviour is preserved exactly.
 */

// --- ethers.js loader (local first, CDN fallback) --------------------------- //
(function () {
  const localScript = document.createElement("script");
  localScript.src = "/static/js/ethers.min.js";
  localScript.onerror = function () {
    const cdnScript = document.createElement("script");
    cdnScript.src = "https://cdn.jsdelivr.net/npm/ethers@5.7.2/dist/ethers.umd.min.js";
    document.head.appendChild(cdnScript);
  };
  document.head.appendChild(localScript);
})();

// --- connect / SIWE verify -------------------------------------------------- //
(function () {
  const API_BASE = window.location.origin;
  const connectBtn = document.getElementById("connectBtn");
  const statusMessage = document.getElementById("statusMessage");
  const urlParams = new URLSearchParams(window.location.search);
  const returnTo = urlParams.get("return_to") || "/";

  function showStatus(message, type = "info") {
    statusMessage.textContent = message;
    statusMessage.className = `status status--${type}`;
  }

  function showSpinner(show) {
    if (show) {
      connectBtn.innerHTML = '<span class="spinner"></span>CONNECTING...';
      connectBtn.disabled = true;
    } else {
      connectBtn.textContent = "CONNECT WALLET";
      connectBtn.disabled = false;
    }
  }

  async function connectWallet() {
    try {
      if (typeof ethers === "undefined") {
        showStatus("Loading...", "info");
        setTimeout(connectWallet, 1000);
        return;
      }
      await new Promise((r) => setTimeout(r, 100));

      if (typeof window.ethereum === "undefined") {
        showStatus("Please install MetaMask", "error");
        setTimeout(() => window.open("https://metamask.io/download/", "_blank"), 2000);
        return;
      }

      showSpinner(true);
      showStatus("Requesting wallet access...", "info");

      const provider = new ethers.providers.Web3Provider(window.ethereum);
      await provider.send("eth_requestAccounts", []);
      const signer = provider.getSigner();
      const walletAddress = await signer.getAddress();
      const chainId = (await provider.getNetwork()).chainId;

      showStatus("Fetching authentication...", "info");

      const nonceResponse = await fetch(`${API_BASE}/api/auth/nonce`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ wallet_address: walletAddress, chain_id: chainId }),
      });

      if (!nonceResponse.ok) {
        const error = await nonceResponse.json().catch(() => ({}));
        if (nonceResponse.status === 429) {
          showStatus("Too many requests. Wait.", "error");
          showSpinner(false);
          return;
        }
        throw new Error(error.detail || "Failed to get nonce");
      }

      const { message, nonce } = await nonceResponse.json();

      showStatus("Sign message in wallet...", "info");
      const signature = await signer.signMessage(message);

      showStatus("Verifying...", "info");

      const verifyResponse = await fetch(`${API_BASE}/api/auth/verify`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          wallet_address: walletAddress,
          message: message,
          signature: signature,
          nonce: nonce,
          chain: "ethereum",
        }),
      });

      if (!verifyResponse.ok) {
        const error = await verifyResponse.json().catch(() => ({}));
        if (verifyResponse.status === 403) {
          showStatus("ACCESS DENIED", "error");
          showSpinner(false);
          return;
        }
        throw new Error(error.detail || "Auth failed");
      }

      const { tier } = await verifyResponse.json();
      // /api/auth/verify also sets the same token as an HttpOnly cookie. The
      // cookie is the console credential; persisting a second JS-readable copy
      // made stale Authorization headers override a newer valid login.
      localStorage.removeItem("auth_token");
      localStorage.setItem("wallet_address", walletAddress);
      localStorage.setItem("tier", tier);

      showStatus("ACCESS GRANTED", "success");
      setTimeout(() => {
        window.location.href = returnTo;
      }, 500);
    } catch (error) {
      console.error("Auth error:", error);
      showStatus(error.message, "error");
      showSpinner(false);
    }
  }

  function init() {
    // Clear credentials written by older console builds, then ask the server
    // whether the HttpOnly cookie is current.
    localStorage.removeItem("auth_token");
    fetch(`${API_BASE}/api/auth/me`, { credentials: "include" })
      .then((res) => (res.ok ? (window.location.href = returnTo) : null))
      .catch(() => {});
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  connectBtn.addEventListener("click", connectWallet);

  // Character hover effect
  document.querySelectorAll("#logo pre").forEach((pre) => {
    const text = pre.textContent;
    pre.innerHTML = text
      .split("")
      .map((char) =>
        char === " " || char === "\n" ? char : `<span class="char">${char}</span>`
      )
      .join("");
  });
})();
