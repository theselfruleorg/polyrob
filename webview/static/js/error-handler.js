/**
 * Global Error Handling for WebView
 *
 * Provides centralized error handling, logging, and user notification
 */

class ErrorHandler {
    constructor() {
        this.errorCount = 0;
        this.maxErrors = 10; // Prevent error spam
        this.errorTimeout = 60000; // Reset count after 1 minute
        this.lastErrorReset = Date.now();
        // Per-message dedupe map for notify() (message -> last shown ts).
        this._recentNotifications = new Map();

        this.setupGlobalHandlers();
    }

    setupGlobalHandlers() {
        // Catch unhandled JavaScript errors
        window.addEventListener('error', (event) => {
            this.handleError({
                message: event.message,
                filename: event.filename,
                lineno: event.lineno,
                colno: event.colno,
                error: event.error
            });

            // Prevent default browser error handling
            event.preventDefault();
        });

        // Catch unhandled promise rejections
        window.addEventListener('unhandledrejection', (event) => {
            this.handleError({
                message: 'Unhandled Promise Rejection',
                error: event.reason,
                promise: true
            });

            // Prevent default browser error handling
            event.preventDefault();
        });

        console.log('[ErrorHandler] Global error handlers initialized');
    }

    handleError(errorInfo) {
        // Reset counter if timeout elapsed
        if (Date.now() - this.lastErrorReset > this.errorTimeout) {
            this.errorCount = 0;
            this.lastErrorReset = Date.now();
        }

        // Increment error count
        this.errorCount++;

        // Log error details
        console.error('[ErrorHandler] Error caught:', errorInfo);

        // If too many errors, show critical alert and stop
        if (this.errorCount >= this.maxErrors) {
            this.showCriticalError();
            return;
        }

        // Show user-friendly error notification
        this.showErrorNotification(errorInfo);
    }

    showErrorNotification(errorInfo) {
        // Create error notification element
        const notification = document.createElement('div');
        notification.className = 'error-notification';
        notification.innerHTML = `
            <div class="error-notification-content">
                <span class="error-icon">⚠️</span>
                <span class="error-message">Something went wrong. Please try refreshing the page.</span>
                <button class="error-close" onclick="this.parentElement.parentElement.remove()">×</button>
            </div>
        `;

        // Add to page
        document.body.appendChild(notification);

        // Auto-remove after 5 seconds
        setTimeout(() => {
            if (notification.parentElement) {
                notification.remove();
            }
        }, 5000);
    }

    /**
     * Surface a SPECIFIC failure message to the user (030 D4 error-UX adoption).
     *
     * Unlike handleError() (generic "something went wrong"), the caller passes
     * the exact message the user should read. Identical messages are deduped
     * within `dedupeMs` so repeated-poll failure paths can call this without
     * producing toast spam. The message is rendered via textContent (it often
     * embeds server/error text — never treat it as HTML).
     *
     * @param {string} message - user-facing text
     * @param {Object} [opts]
     * @param {number} [opts.dedupeMs=30000] - suppress identical messages within this window
     * @returns {boolean} true if a notification was actually shown
     */
    notify(message, opts = {}) {
        const text = String(message || 'Something went wrong.');
        const dedupeMs = opts.dedupeMs === undefined ? 30000 : opts.dedupeMs;
        const now = Date.now();

        const last = this._recentNotifications.get(text);
        if (last !== undefined && now - last < dedupeMs) {
            return false;
        }
        this._recentNotifications.set(text, now);
        // Keep the dedupe map bounded (Map preserves insertion order).
        while (this._recentNotifications.size > 50) {
            this._recentNotifications.delete(this._recentNotifications.keys().next().value);
        }

        // Share the global spam guard with handleError(); past the threshold
        // the console record (made by the caller) is the surviving trail.
        if (now - this.lastErrorReset > this.errorTimeout) {
            this.errorCount = 0;
            this.lastErrorReset = now;
        }
        this.errorCount++;
        if (this.errorCount >= this.maxErrors) {
            return false;
        }

        const notification = document.createElement('div');
        notification.className = 'error-notification';
        const content = document.createElement('div');
        content.className = 'error-notification-content';
        const icon = document.createElement('span');
        icon.className = 'error-icon';
        icon.textContent = '⚠️';
        const msg = document.createElement('span');
        msg.className = 'error-message';
        msg.textContent = text;
        const close = document.createElement('button');
        close.className = 'error-close';
        close.textContent = '×';
        close.addEventListener('click', () => notification.remove());
        content.appendChild(icon);
        content.appendChild(msg);
        content.appendChild(close);
        notification.appendChild(content);
        document.body.appendChild(notification);

        setTimeout(() => {
            if (notification.parentElement) {
                notification.remove();
            }
        }, 8000);
        return true;
    }

    showCriticalError() {
        // Create critical error overlay
        const overlay = document.createElement('div');
        overlay.className = 'error-critical-overlay';
        overlay.innerHTML = `
            <div class="error-critical-content">
                <h2>⚠️ Critical Error</h2>
                <p>Multiple errors detected. The page may not work correctly.</p>
                <button onclick="window.location.reload()" class="btn-reload">Reload Page</button>
            </div>
        `;

        document.body.appendChild(overlay);

        console.error('[ErrorHandler] Critical error threshold reached - blocking further errors');
    }
}

// CSS for error notifications
const errorStyles = document.createElement('style');
errorStyles.textContent = `
    .error-notification {
        position: fixed;
        top: 20px;
        right: 20px;
        z-index: 10000;
        animation: slideIn 0.3s ease-out;
    }

    .error-notification-content {
        background: var(--color-accent-red-wash-95);
        color: var(--color-on-accent);
        padding: 12px 16px;
        border-radius: 8px;
        box-shadow: 0 4px 12px var(--color-shadow-wash-30);
        display: flex;
        align-items: center;
        gap: 12px;
        max-width: 400px;
    }

    .error-icon {
        font-size: 20px;
        flex-shrink: 0;
    }

    .error-message {
        flex: 1;
        font-size: 14px;
        line-height: 1.4;
    }

    .error-close {
        background: transparent;
        border: none;
        color: var(--color-on-accent);
        font-size: 24px;
        cursor: pointer;
        padding: 0;
        width: 24px;
        height: 24px;
        line-height: 1;
        flex-shrink: 0;
    }

    .error-close:hover {
        opacity: 0.7;
    }

    .error-critical-overlay {
        position: fixed;
        top: 0;
        left: 0;
        right: 0;
        bottom: 0;
        background: var(--color-shadow-wash-90);
        z-index: 100000;
        display: flex;
        align-items: center;
        justify-content: center;
    }

    .error-critical-content {
        background: var(--color-bg-elevated);
        color: var(--color-text-primary);
        padding: 32px;
        border-radius: 12px;
        text-align: center;
        max-width: 500px;
    }

    .error-critical-content h2 {
        font-size: 24px;
        margin: 0 0 16px 0;
    }

    .error-critical-content p {
        font-size: 16px;
        margin: 0 0 24px 0;
        opacity: 0.8;
    }

    .btn-reload {
        background: var(--color-accent-blue);
        color: var(--color-on-accent);
        border: none;
        padding: 12px 24px;
        border-radius: 6px;
        font-size: 16px;
        cursor: pointer;
        transition: background 0.2s;
    }

    .btn-reload:hover {
        background: var(--color-accent-blue);
    }

    @keyframes slideIn {
        from {
            transform: translateX(400px);
            opacity: 0;
        }
        to {
            transform: translateX(0);
            opacity: 1;
        }
    }
`;
document.head.appendChild(errorStyles);

// Initialize error handler
const errorHandler = new ErrorHandler();

// Bridge for CLASSIC (non-module) scripts — profile.js / activity.js load
// without type="module" and cannot import this file. They
// use `window.errorHandler?.notify(...)` (guarded: module scripts execute
// after classic ones, so the global may not exist at their top-level run).
window.errorHandler = errorHandler;

export { errorHandler };
