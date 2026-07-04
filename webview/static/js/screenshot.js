/* Screenshot management for WebView sessions */
import { setText, fetchJSON, on, emit, logger, sessionState } from '/static/js/ui-utils.js?v=2';

/**
 * ScreenshotManager - Singleton class to manage screenshot loading and display
 * Prevents multiple timers and duplicate fetches across tabs
 */
class ScreenshotManager {
    constructor() {
        // Only create a single instance
        if (ScreenshotManager.instance) {
            return ScreenshotManager.instance;
        }
        
        // Store this instance
        ScreenshotManager.instance = this;
        
        // Use centralized session state
        this.sessionId = sessionState.sessionId;
        
        // Default screenshot refresh interval in milliseconds
        this.DEFAULT_REFRESH_INTERVAL = 2000; // 2 seconds
        this.screenshotRefreshInterval = this.DEFAULT_REFRESH_INTERVAL;
        
        // Screenshot loading state tracking
        this.isLoadingScreenshot = false;
        this.lastLoadTime = 0;
        this.consecutiveFailures = 0;
        this.screenshotTimer = null;
        this.isActive = false;
        
        // Latest screenshot data cache
        this.latestScreenshotData = null;
        
        // DOM elements - will be set in initialize()
        this.screenshotImg = null;
        this.screenshotPlaceholder = null;
        this.screenshotError = null;
        this.pageUrlDisplay = null;
        
        // Initialize when DOM is loaded
        document.addEventListener('DOMContentLoaded', () => this.initialize());
        
        return this;
    }
    
    /**
     * Initialize the screenshot manager when DOM is loaded
     */
    initialize() {
        // Get DOM elements
        this.screenshotImg = document.getElementById('session-screenshot');
        this.screenshotPlaceholder = document.getElementById('screenshot-placeholder');
        this.screenshotError = document.getElementById('screenshot-error');
        this.pageUrlDisplay = document.getElementById('page-url');

        // Check if this is a new session (no real session ID yet)
        const isNew = sessionState.isNew;
        const hasSessionId = this.sessionId && this.sessionId !== '' && this.sessionId !== 'new';

        // Check if screenshot elements exist on the page
        this.isActive = !!(this.screenshotImg && this.screenshotPlaceholder);

        // Don't load screenshots for new sessions
        if (isNew || !hasSessionId) {
            logger.debug('[ScreenshotManager] New session detected, skipping screenshot loading');
            if (this.screenshotPlaceholder) {
                this.showPlaceholder("Start a conversation to begin");
            }
            return;
        }

        if (this.isActive) {
            logger.debug('[ScreenshotManager] Initializing screenshot manager');
            // Initial load
            this.loadScreenshot();

            // Set up fullscreen button
            const fullscreenBtn = document.getElementById('fullscreen-btn');
            if (fullscreenBtn) {
                fullscreenBtn.addEventListener('click', () => this.toggleFullscreen());
            }

            // Listen for tab activation events
            on('tab:activated', (event) => {
                // If the session tab is activated, ensure screenshot is fresh
                if (event.detail && (event.detail.tabId === 'feed' || event.detail.tabId === 'feed-tab')) {
                    // Force refresh when tab is activated
                    this.loadScreenshot(true);
                }
            });
        } else {
            logger.debug('[ScreenshotManager] Screenshot elements not found, manager in inactive state');
        }
    }
    
    /**
     * Load the latest screenshot for the current session
     * @param {boolean} forceRefresh - Whether to force a refresh even if recently loaded
     */
    async loadScreenshot(forceRefresh = false) {
        // Skip if screenshot elements don't exist on this page
        if (!this.isActive) return;
        
        // First clear any existing timer to prevent stacking requests
        clearTimeout(this.screenshotTimer);
        
        // Prevent multiple simultaneous requests
        if (this.isLoadingScreenshot) {
            // Schedule next check and return
            this.screenshotTimer = setTimeout(() => this.loadScreenshot(), this.screenshotRefreshInterval);
            return;
        }
        
        // Rate limiting - ensure at least 500ms between requests unless force refreshing
        const now = Date.now();
        if (!forceRefresh && now - this.lastLoadTime < 500) {
            // Schedule next check and return
            this.screenshotTimer = setTimeout(() => this.loadScreenshot(), this.screenshotRefreshInterval);
            return;
        }
        
        this.isLoadingScreenshot = true;
        this.lastLoadTime = now;
        
        try {
            logger.debug(`[ScreenshotManager] Fetching screenshot for ${this.sessionId}`);
            
            // Emit event to notify that screenshot loading has started
            emit('screenshot:loading');
            
            // Add cache busting parameter to prevent browser caching
            const timestamp = Date.now();
            const result = await fetchJSON(`/api/session/${this.sessionId}/screenshot?ts=${timestamp}`);
            
            // Reset consecutive failures counter on success
            this.consecutiveFailures = 0;
            
            // Normal refresh rate
            this.screenshotRefreshInterval = this.DEFAULT_REFRESH_INTERVAL;
            
            if (result && result.status === 'ok') {
                // Cache the latest screenshot data
                this.latestScreenshotData = result;
                
                // Handle multiple response formats
                const screenshotUrl = result.url;
                const isPlaceholder = result.is_placeholder === true;
                const pageUrl = result.page_url || null;
                
                // Update page URL display if available
                this.updatePageUrl(pageUrl);
                
                if (isPlaceholder) {
                    // Show placeholder for intentional placeholders
                    this.showPlaceholder("Waiting for browser activity...");
                } else if (screenshotUrl) {
                    // Load actual screenshot
                    this.loadScreenshotImage(screenshotUrl);
                } else {
                    // No valid URL provided
                    this.showPlaceholder("No screenshot available");
                }
                
                // Emit event with screenshot data
                emit('screenshot:loaded', { data: result });
            } else {
                // Show appropriate error or placeholder
                if (result && result.message) {
                    this.showPlaceholder(result.message);
                } else {
                    this.showPlaceholder("No screenshot available");
                }
                
                // Emit error event
                emit('screenshot:error', { message: result?.message || 'Failed to load screenshot' });
            }
        } catch (error) {
            logger.error(`[ScreenshotManager] Error loading screenshot: ${error.message}`);
            this.consecutiveFailures++;
            
            // Back off on repeated failures
            if (this.consecutiveFailures > 3) {
                // Exponential backoff up to 10 seconds
                this.screenshotRefreshInterval = Math.min(
                    this.DEFAULT_REFRESH_INTERVAL * Math.pow(1.5, this.consecutiveFailures - 3), 
                    10000
                );
                logger.debug(`[ScreenshotManager] Screenshot backoff: ${this.screenshotRefreshInterval}ms`);
            }
            
            this.showPlaceholder("Error loading screenshot");
            
            // Emit error event
            emit('screenshot:error', { error });
        } finally {
            this.isLoadingScreenshot = false;
            
            // Schedule next check
            this.screenshotTimer = setTimeout(() => this.loadScreenshot(), this.screenshotRefreshInterval);
        }
    }
    
    /**
     * Get the latest screenshot data without fetching
     * @returns {Object|null} The cached screenshot data or null if not available
     */
    getLatestScreenshot() {
        return this.latestScreenshotData;
    }
    
    /**
     * Load and display a screenshot image from the given URL
     * @param {string} url - The URL of the screenshot to display
     */
    loadScreenshotImage(url) {
        // Create a new Image to test loading
        const tempImg = new Image();
        
        tempImg.onload = () => {
            // Image loaded successfully
            if (this.screenshotImg) {
                this.screenshotImg.src = url;
                this.screenshotImg.classList.remove('hidden');
            }
            if (this.screenshotPlaceholder) {
                this.screenshotPlaceholder.classList.add('hidden');
            }
            if (this.screenshotError) {
                this.screenshotError.classList.add('hidden');
            }
        };
        
        tempImg.onerror = () => {
            // Image failed to load
            logger.error(`[ScreenshotManager] Failed to load screenshot from ${url}`);
            this.showPlaceholder("Failed to load screenshot");
        };
        
        // Set the source to begin loading
        tempImg.src = url;
    }
    
    /**
     * Show the placeholder with a custom message
     * @param {string} message - The message to display in the placeholder
     */
    showPlaceholder(message = "No screenshot available") {
        if (this.screenshotPlaceholder) {
            // Update the message text (first div inside placeholder)
            const messageEl = this.screenshotPlaceholder.querySelector('div:first-child');
            if (messageEl) {
                messageEl.textContent = message;
            }
            
            this.screenshotPlaceholder.classList.remove('hidden');
        }
        
        if (this.screenshotImg) {
            this.screenshotImg.classList.add('hidden');
        }
        
        if (this.screenshotError) {
            this.screenshotError.classList.add('hidden');
        }
    }
    
    /**
     * Update the page URL display
     * @param {string|null} url - The URL to display, or null if none available
     */
    updatePageUrl(url) {
        if (!this.pageUrlDisplay) return;
        
        if (url) {
            // Try to format the URL nicely
            try {
                const urlObj = new URL(url);
                // Display hostname with protocol
                this.pageUrlDisplay.textContent = urlObj.hostname;
                // Add the full URL as a tooltip
                this.pageUrlDisplay.title = url;
                
                // Add a clickable link
                this.pageUrlDisplay.innerHTML = `<a href="${url}" target="_blank" rel="noopener noreferrer">${urlObj.hostname}</a>`;
            } catch (e) {
                // If URL parsing fails, just show the raw URL
                this.pageUrlDisplay.textContent = url;
                this.pageUrlDisplay.title = url;
            }
        } else {
            this.pageUrlDisplay.textContent = "No URL available";
            this.pageUrlDisplay.title = "";
        }
    }
    
    /**
     * Toggle fullscreen view of the screenshot
     */
    toggleFullscreen() {
        // Get or create the fullscreen container from the DOM
        const fullscreenContainer = document.getElementById('fullscreen-container');
        if (!fullscreenContainer) {
            logger.error('[ScreenshotManager] Fullscreen container not found');
            return;
        }
        
        // Get the fullscreen image element
        const fullscreenImg = document.getElementById('fullscreen-img');
        if (!fullscreenImg) {
            logger.error('[ScreenshotManager] Fullscreen image element not found');
            return;
        }
        
        // Show the container
        fullscreenContainer.classList.add('active');
        
        // Set the image source from current screenshot if available
        if (this.screenshotImg && !this.screenshotImg.classList.contains('hidden')) {
            fullscreenImg.src = this.screenshotImg.src;
        } else {
            // Load a fresh screenshot directly in the modal
            const timestamp = Date.now();
            fullscreenImg.src = `/api/session/${this.sessionId}/screenshot/file?ts=${timestamp}`;
        }
        
        // Add close handler if not already present
        const closeBtn = document.getElementById('fullscreen-close');
        if (closeBtn && !closeBtn.hasAddedHandler) {
            closeBtn.addEventListener('click', () => {
                fullscreenContainer.classList.remove('active');
            });
            closeBtn.hasAddedHandler = true;
        }
    }
    
    /**
     * Stop auto-refresh timer if active
     */
    stopRefreshTimer() {
        if (this.screenshotTimer) {
            clearTimeout(this.screenshotTimer);
            this.screenshotTimer = null;
            logger.debug('[ScreenshotManager] Screenshot refresh timer stopped');
        }
    }
    
    /**
     * Start the refresh timer if not active
     */
    startRefreshTimer() {
        if (!this.screenshotTimer && this.isActive) {
            logger.debug('[ScreenshotManager] Starting screenshot refresh timer');
            this.loadScreenshot();
        }
    }
}

// Create the singleton instance
const screenshotManager = new ScreenshotManager();

/**
 * Load the latest screenshot (uses the singleton manager)
 * @param {boolean} forceRefresh - Whether to force a refresh 
 */
function loadScreenshot(forceRefresh = false) {
    screenshotManager.loadScreenshot(forceRefresh);
}

/**
 * Get the latest screenshot data without fetching
 * @returns {Object|null} The latest screenshot data
 */
function getLatestScreenshot() {
    return screenshotManager.getLatestScreenshot();
}

/**
 * Toggle fullscreen view of the screenshot (uses the singleton manager)
 */
function toggleFullscreen() {
    screenshotManager.toggleFullscreen();
}

// Export functions for use in other modules
export { loadScreenshot, toggleFullscreen, getLatestScreenshot }; 