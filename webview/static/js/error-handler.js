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

        // Send to server for logging (if needed)
        this.reportToServer(errorInfo);
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

    reportToServer(errorInfo) {
        // Disabled - endpoint doesn't exist
        // TODO: Implement /api/errors/log endpoint if server-side error logging is needed
        return;
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
        background: rgba(239, 68, 68, 0.95);
        color: white;
        padding: 12px 16px;
        border-radius: 8px;
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
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
        color: white;
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
        background: rgba(0, 0, 0, 0.9);
        z-index: 100000;
        display: flex;
        align-items: center;
        justify-content: center;
    }

    .error-critical-content {
        background: #1a1a1a;
        color: white;
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
        background: #3b82f6;
        color: white;
        border: none;
        padding: 12px 24px;
        border-radius: 6px;
        font-size: 16px;
        cursor: pointer;
        transition: background 0.2s;
    }

    .btn-reload:hover {
        background: #2563eb;
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

export { errorHandler };
