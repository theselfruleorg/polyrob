/* Lightweight DOM helpers for the WebView UI. */

export const eventBus = document.createElement('div');

export function emit(name, detail = {}) {
    eventBus.dispatchEvent(new CustomEvent(name, { detail }));
}

export function on(name, handler) {
    eventBus.addEventListener(name, handler);
}

export function off(name, handler) {
    eventBus.removeEventListener(name, handler);
}

// ---------------------------------------------------------------------------
// Centralized Session State Manager
// ---------------------------------------------------------------------------
// Single source of truth for session state across all modules

class SessionStateManager {
    constructor() {
        this._sessionId = null;
        this._isNew = true;
        this._status = 'unknown';
        this._isOwner = false;
        this._isAuthenticated = false;
        this._initialized = false;
    }

    /**
     * Initialize state from DOM and window globals
     * Should be called once on page load
     */
    init() {
        if (this._initialized) return;
        
        // Read from DOM data attributes (primary source)
        this._sessionId = document.body?.dataset?.sessionId || null;
        this._isNew = document.body?.dataset?.isNew === 'true' || this._sessionId === 'new';
        
        // Read from window globals (set by template)
        this._isOwner = window.isSessionOwner || false;
        this._isAuthenticated = window.isUserAuthenticated || false;
        
        this._initialized = true;
    }

    get sessionId() {
        if (!this._initialized) this.init();
        return this._sessionId === 'new' ? null : this._sessionId;
    }

    get isNew() {
        if (!this._initialized) this.init();
        return this._isNew || !this._sessionId || this._sessionId === 'new';
    }

    get status() {
        return this._status;
    }

    set status(value) {
        const oldStatus = this._status;
        this._status = value;
        if (oldStatus !== value) {
            emit('session:statusChanged', { oldStatus, newStatus: value });
        }
    }

    get isOwner() {
        if (!this._initialized) this.init();
        return this._isOwner;
    }

    get isAuthenticated() {
        if (!this._initialized) this.init();
        return this._isAuthenticated;
    }

    /**
     * Transition from new session to active session
     * Updates all state and DOM attributes
     */
    transitionToActive(newSessionId) {
        this._sessionId = newSessionId;
        this._isNew = false;
        this._status = 'running';
        this._isOwner = true; // User who created it is owner
        
        // Update DOM
        if (document.body) {
            document.body.dataset.sessionId = newSessionId;
            document.body.dataset.isNew = 'false';
        }
        
        // Update URL without reload
        window.history.pushState({ sessionId: newSessionId }, '', `/session/${newSessionId}`);
        
        emit('session:activated', { sessionId: newSessionId });
    }

    /**
     * Check if user can interact with session
     */
    canInteract() {
        return this._isOwner || this._isNew;
    }
}

// Singleton instance
export const sessionState = new SessionStateManager();

// ---------------------------------------------------------------------------
// Debug Logger - respects localStorage.debug flag
// ---------------------------------------------------------------------------

const DEBUG_ENABLED = localStorage.getItem('debug') === 'true';

export const logger = {
    debug(...args) {
        if (DEBUG_ENABLED) console.log('[DEBUG]', ...args);
    },
    info(...args) {
        console.log('[INFO]', ...args);
    },
    warn(...args) {
        console.warn('[WARN]', ...args);
    },
    error(...args) {
        console.error('[ERROR]', ...args);
    }
};

/**
 * Safely set text content of an element by ID
 * @param {string} id - The element ID
 * @param {string} text - The text content to set
 * @returns {boolean} - True if element was found and updated, false otherwise
 */
export function setText(id, text) {
    const element = document.getElementById(id);
    if (!element) return false;
    element.textContent = text;
    return true;
}

/**
 * Escape HTML special characters to prevent XSS
 * @param {string} str - The string to escape
 * @returns {string} - The escaped string
 */
export function escapeHtml(str) {
    if (typeof str !== 'string') {
        str = JSON.stringify(str, null, 2);
    }
    return str.replace(/[&<>'"`]/g, (char) => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;', '`': '&#96;',
    }[char]));
}

/**
 * Fetch JSON data from an API endpoint with error handling
 * @param {string} url - The URL to fetch from
 * @param {Object} options - Fetch options
 * @returns {Promise<Object>} - The parsed JSON response
 * @throws {Error} - If fetch fails or response is not OK
 */
export async function fetchJSON(url, options = {}) {
    try {
        const response = await fetch(url, options);
        
        if (!response.ok) {
            throw new Error(`API error: ${response.status} ${response.statusText}`);
        }
        
        const data = await response.json();
        
        // Check for API-level errors
        if (data.status === 'error') {
            throw new Error(`API returned error: ${data.message || 'Unknown error'}`);
        }
        return data;
    } catch (error) {
        if (DEBUG_ENABLED) {
            console.error(`[DEBUG] fetchJSON error for ${url}:`, error);
        }
        throw error;
    }
}

/**
 * Turn a group of buttons into accessible tabs.
 * Each tab-button must have `data-tab` that matches the corresponding
 * tab panel ID.
 * 
 * @param {string} containerSelector - Selector for the tab container
 */
export function initTabs(containerSelector = '.tabs-wrapper') {
    const container = document.querySelector(containerSelector);
    if (!container) return;
    
    const buttons = container.querySelectorAll('.tab-button');
    
    // Function to activate a tab
    function activateTab(tabId) {
        // Deactivate all tabs
        buttons.forEach(btn => {
            btn.classList.remove('active');
            btn.setAttribute('aria-selected', 'false');
        });
        
        // Hide all panels
        document.querySelectorAll('.tab-panel').forEach(panel => {
            panel.classList.remove('active');
        });
        
        // Activate the selected tab
        const button = container.querySelector(`.tab-button[data-tab="${tabId}"]`);
        const panel = document.getElementById(`${tabId}-tab`);
        
        if (button) {
            button.classList.add('active');
            button.setAttribute('aria-selected', 'true');

            // Scroll active tab into view on mobile
            if (window.innerWidth <= 768) {
                button.scrollIntoView({
                    behavior: 'smooth',
                    block: 'nearest',
                    inline: 'center'
                });
            }
        }

        if (panel) {
            panel.classList.add('active');
        }

        // Dispatch a custom event for tab activation
        emit('tab:activated', { tabId });
    }
    
    // Add click handlers to tab buttons
    buttons.forEach(button => {
        const tabId = button.dataset.tab;

        button.addEventListener('click', () => {
            activateTab(tabId);
        });
    });

    // Deep-link support: /session/{id}#feed lands straight on that tab.
    const fromHash = (window.location.hash || '').replace('#', '');
    if (fromHash && container.querySelector(`.tab-button[data-tab="${fromHash}"]`)) {
        activateTab(fromHash);
    } else if (!container.querySelector('.tab-button.active') && buttons.length > 0) {
        // Activate first tab by default if none is active
        const firstTabId = buttons[0].dataset.tab;
        activateTab(firstTabId);
    }
}

/**
 * Format a file size in bytes to a human-readable string
 * @param {number} bytes - The file size in bytes
 * @returns {string} - Formatted file size (e.g., "1.5 MB")
 */
export function formatFileSize(bytes) {
    if (bytes === 0) return '0 B';
    
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(1024));
    
    return (bytes / Math.pow(1024, i)).toFixed(1) + ' ' + units[i];
}

/**
 * Highlight code blocks in an HTML element
 * @param {HTMLElement} element - The element containing code blocks
 */
export function highlightCode(element) {
    if (!element) return;
    
    const codeBlocks = element.querySelectorAll('pre code');
    
    if (window.hljs) {
        codeBlocks.forEach(block => {
            window.hljs.highlightElement(block);
        });
    }
}

/**
 * Format a cost value for display with appropriate precision
 * @param {number} cost - Cost in USD
 * @returns {string} - Formatted cost string
 */
export function formatCost(cost) {
    if (cost === undefined || cost === null) return '$0.00';
    
    // Convert to number if it's a string
    const costNum = typeof cost === 'string' ? parseFloat(cost) : cost;
    
    // Check if it's a valid number
    if (isNaN(costNum)) return '$0.00';
    
    // Format based on value range
    if (costNum < 0.001) {
        // For very small values, show more decimal places
        return `$${costNum.toFixed(5)}`;
    } else if (costNum < 0.01) {
        return `$${costNum.toFixed(4)}`;
    } else if (costNum < 0.1) {
        return `$${costNum.toFixed(3)}`;
    } else if (costNum < 1) {
        return `$${costNum.toFixed(2)}`;
    } else {
        return `$${costNum.toFixed(2)}`;
    }
}

// ---------------------------------------------------------------------------
// Rate Limiter - prevents rapid repeated calls
// ---------------------------------------------------------------------------

export class RateLimiter {
    constructor(minInterval = 1000) {
        this.minInterval = minInterval;
        this.lastCall = 0;
        this.pendingTimeout = null;
    }

    /**
     * Execute callback if rate limit allows, otherwise queue
     * @param {Function} callback - Function to execute
     * @param {boolean} force - Force immediate execution
     * @returns {boolean} - Whether callback was executed immediately
     */
    call(callback, force = false) {
        const now = Date.now();
        const timeSinceLastCall = now - this.lastCall;

        if (force || timeSinceLastCall >= this.minInterval) {
            this.lastCall = now;
            if (this.pendingTimeout) {
                clearTimeout(this.pendingTimeout);
                this.pendingTimeout = null;
            }
            callback();
            return true;
        }

        // Queue the call for later
        if (!this.pendingTimeout) {
            const delay = this.minInterval - timeSinceLastCall;
            this.pendingTimeout = setTimeout(() => {
                this.lastCall = Date.now();
                this.pendingTimeout = null;
                callback();
            }, delay);
        }
        return false;
    }

    /**
     * Cancel any pending call
     */
    cancel() {
        if (this.pendingTimeout) {
            clearTimeout(this.pendingTimeout);
            this.pendingTimeout = null;
        }
    }
}

// ---------------------------------------------------------------------------
// Exponential Backoff - for reconnection logic
// ---------------------------------------------------------------------------

export class ExponentialBackoff {
    constructor(options = {}) {
        this.baseDelay = options.baseDelay || 1000;
        this.maxDelay = options.maxDelay || 30000;
        this.maxRetries = options.maxRetries || 10;
        this.factor = options.factor || 2;
        this.attempt = 0;
    }

    /**
     * Get delay for next retry
     * @returns {number|null} - Delay in ms, or null if max retries exceeded
     */
    nextDelay() {
        if (this.attempt >= this.maxRetries) {
            return null;
        }
        const delay = Math.min(
            this.baseDelay * Math.pow(this.factor, this.attempt),
            this.maxDelay
        );
        this.attempt++;
        return delay;
    }

    /**
     * Reset the backoff state
     */
    reset() {
        this.attempt = 0;
    }

    /**
     * Check if max retries exceeded
     */
    isExhausted() {
        return this.attempt >= this.maxRetries;
    }
} 