/* Global constants for WebView front-end */

// Centralized WebSocket URL handling - single source of truth
// Get WebSocket URL from meta tag if available, otherwise fallback to auto-detection
function resolveWebSocketUrl() {
    const wsUrlMeta = document.querySelector('meta[name="ws-url"]');
    const metaUrl = wsUrlMeta && wsUrlMeta.content ? wsUrlMeta.content : null;
    
    if (metaUrl) {
        // Use the explicitly configured WebSocket URL
        return metaUrl;
    } else {
        // Fallback to auto-detection based on current protocol
        const wsProtocol = location.protocol === 'https:' ? 'wss' : 'ws';
        return `${wsProtocol}://${location.host}`;
    }
}

// Set global constants for all scripts to use
window.WS_BASE = resolveWebSocketUrl(); 