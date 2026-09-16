/*
 * socketio-loader.js — load Socket.IO (CDN first, local fallback).
 *
 * De-inlined from layout.html <head> (043 phase 5, R6): the console CSP drops
 * `'unsafe-inline'` from `script-src`. Loaded as a classic in-head script at
 * the SAME position as the former inline block, so `window.socketIOReady` is
 * defined synchronously before any later script awaits it. Behaviour preserved.
 */
// Create a promise that resolves when Socket.IO is loaded
window.socketIOReady = new Promise((resolve, reject) => {
  // Function to load the local fallback
  function loadLocalSocketIO() {
    console.warn("Using local Socket.IO fallback");
    const fallbackScript = document.createElement("script");
    fallbackScript.src = "/static/js/socket.io.min.js";
    fallbackScript.onload = () => {
      console.log("Local Socket.IO loaded successfully");
      resolve(window.io);
    };
    fallbackScript.onerror = (err) => {
      console.error("Failed to load local Socket.IO:", err);
      reject(new Error("Could not load Socket.IO from any source"));
    };
    document.head.appendChild(fallbackScript);
  }

  // Only try to load from CDN if window.io is not already defined
  if (!window.io) {
    // Try to load from CDN first
    const cdnScript = document.createElement("script");
    cdnScript.src = "https://cdn.socket.io/4.6.0/socket.io.min.js";
    cdnScript.onload = () => {
      console.log("CDN Socket.IO loaded successfully");
      resolve(window.io);
    };
    cdnScript.onerror = loadLocalSocketIO;
    document.head.appendChild(cdnScript);
  } else {
    // Socket.IO already loaded
    resolve(window.io);
  }
});
