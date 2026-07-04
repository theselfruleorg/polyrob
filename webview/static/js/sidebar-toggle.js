/**
 * Sidebar Toggle Manager
 *
 * Manages the visibility and state of the right sidebar panel.
 * Features:
 * - Toggle sidebar visibility
 * - Persist state in localStorage
 * - Auto-show when session becomes active
 * - Smooth CSS transitions
 */
import { logger } from '/static/js/ui-utils.js?v=2';

class SidebarToggleManager {
    constructor() {
        this.sidebarTop = document.querySelector('.sidebar-top');
        this.sidebarBottom = document.querySelector('.sidebar-bottom');
        this.toggleBtn = document.getElementById('sidebar-toggle-btn');
        this.toggleIcon = this.toggleBtn?.querySelector('.toggle-icon');
        this.sessionContainer = document.querySelector('.session-container');

        // Detect mobile
        this.isMobile = window.innerWidth <= 768;
        this.mobileToggleBtn = null;

        // State
        this.isVisible = this.loadState();

        // Initialize
        this.init();

        // Update mobile state on resize
        window.addEventListener('resize', () => {
            const wasMobile = this.isMobile;
            this.isMobile = window.innerWidth <= 768;

            // Reinitialize if mobile state changed
            if (wasMobile !== this.isMobile) {
                this.init();
            }
        });
    }

    init() {
        logger.debug('[SidebarToggle] Initializing, isMobile:', this.isMobile, 'isVisible:', this.isVisible);

        // Different handling for mobile vs desktop
        if (this.isMobile) {
            this.initMobile();
        } else {
            this.initDesktop();
        }

        // Listen for session activation events
        this.watchForSessionActivation();
    }

    initDesktop() {
        // Remove mobile toggle if it exists
        if (this.mobileToggleBtn) {
            this.mobileToggleBtn.remove();
            this.mobileToggleBtn = null;
        }

        // Apply initial state
        this.applyState(false); // No animation on initial load

        // Set up event listeners for desktop toggle button
        if (this.toggleBtn) {
            this.toggleBtn.addEventListener('click', () => this.toggle());
        }

        // Close sidebar when clicking backdrop
        if (this.sessionContainer) {
            this.sessionContainer.addEventListener('click', (e) => {
                // Only close if clicking the backdrop (::before pseudo-element area)
                if (this.isVisible && e.target === this.sessionContainer) {
                    this.hide();
                }
            });
        }
    }

    initMobile() {
        // Remove desktop hidden class if present
        this.sessionContainer?.classList.remove('sidebar-hidden');

        // Create mobile toggle button if it doesn't exist
        if (!this.mobileToggleBtn) {
            this.mobileToggleBtn = document.createElement('button');
            this.mobileToggleBtn.className = 'mobile-sidebar-toggle';
            this.mobileToggleBtn.setAttribute('aria-label', 'Toggle Vision & Info');
            this.mobileToggleBtn.setAttribute('title', 'Toggle Vision & Info');
            // Text content removed - icon added via CSS ::before
            document.body.appendChild(this.mobileToggleBtn);

            this.mobileToggleBtn.addEventListener('click', () => this.toggleMobile());
        }

        // Apply initial state (hide by default on mobile)
        if (this.isVisible) {
            this.showMobile();
        } else {
            this.hideMobile();
        }

        // Close on backdrop click (mobile)
        if (this.sessionContainer) {
            this.sessionContainer.addEventListener('click', (e) => {
                // Close if clicking the backdrop (::after pseudo-element)
                if (this.isVisible && (e.target === this.sessionContainer || e.target.classList.contains('session-container'))) {
                    // Make sure we're not clicking on sidebar or its children
                    if (!e.target.closest('.sidebar-top') && !e.target.closest('.sidebar-bottom')) {
                        this.hideMobile();
                    }
                }
            });
        }
    }

    /**
     * Toggle sidebar visibility (desktop)
     */
    toggle() {
        this.isVisible = !this.isVisible;
        this.applyState(true); // With animation
        this.saveState();

        logger.debug('[SidebarToggle] Toggled to:', this.isVisible ? 'visible' : 'hidden');
    }

    /**
     * Toggle sidebar visibility (mobile)
     */
    toggleMobile() {
        this.isVisible = !this.isVisible;
        this.saveState();

        if (this.isVisible) {
            this.showMobile();
        } else {
            this.hideMobile();
        }

        logger.debug('[SidebarToggle] Mobile toggled to:', this.isVisible ? 'visible' : 'hidden');
    }

    /**
     * Show sidebar (without toggling)
     */
    show() {
        if (!this.isVisible) {
            this.isVisible = true;
            if (this.isMobile) {
                this.showMobile();
            } else {
                this.applyState(true);
            }
            this.saveState();
            logger.debug('[SidebarToggle] Sidebar shown');
        }
    }

    /**
     * Show sidebar (mobile)
     */
    showMobile() {
        this.sessionContainer?.classList.add('mobile-sidebar-visible');
        // Add active class to button to trigger arrow flip
        if (this.mobileToggleBtn) {
            this.mobileToggleBtn.classList.add('active');
            this.mobileToggleBtn.setAttribute('aria-label', 'Close Vision & Info');
            this.mobileToggleBtn.setAttribute('title', 'Close Vision & Info');
        }
    }

    /**
     * Hide sidebar (without toggling)
     */
    hide() {
        if (this.isVisible) {
            this.isVisible = false;
            if (this.isMobile) {
                this.hideMobile();
            } else {
                this.applyState(true);
            }
            this.saveState();
            logger.debug('[SidebarToggle] Sidebar hidden');
        }
    }

    /**
     * Hide sidebar (mobile)
     */
    hideMobile() {
        this.sessionContainer?.classList.remove('mobile-sidebar-visible');
        // Remove active class from button to trigger arrow flip
        if (this.mobileToggleBtn) {
            this.mobileToggleBtn.classList.remove('active');
            this.mobileToggleBtn.setAttribute('aria-label', 'Show Vision & Info');
            this.mobileToggleBtn.setAttribute('title', 'Show Vision & Info');
        }
    }

    /**
     * Apply visibility state to DOM
     */
    applyState(animate = true) {
        const action = this.isVisible ? 'remove' : 'add';

        // Add/remove hidden class
        this.sessionContainer?.classList[action]('sidebar-hidden');

        // Update toggle button icon
        if (this.toggleIcon) {
            // ◧ for visible (panel on right), ◨ for hidden (panel hidden)
            this.toggleIcon.textContent = this.isVisible ? '◧' : '◨';
        }

        // Update button title
        if (this.toggleBtn) {
            this.toggleBtn.title = this.isVisible ? 'Hide sidebar' : 'Show sidebar';
        }

        // Disable transitions temporarily for instant application
        if (!animate) {
            this.sessionContainer?.classList.add('no-transition');
            this.sidebarTop?.classList.add('no-transition');
            this.sidebarBottom?.classList.add('no-transition');

            // Re-enable after a frame
            requestAnimationFrame(() => {
                this.sessionContainer?.classList.remove('no-transition');
                this.sidebarTop?.classList.remove('no-transition');
                this.sidebarBottom?.classList.remove('no-transition');
            });
        }
    }

    /**
     * Save state to localStorage
     */
    saveState() {
        try {
            localStorage.setItem('sidebar-visible', JSON.stringify(this.isVisible));
        } catch (e) {
            logger.warn('[SidebarToggle] Failed to save state:', e);
        }
    }

    /**
     * Load state from localStorage
     * Default: hidden (sidebar starts closed, user can toggle)
     */
    loadState() {
        try {
            const saved = localStorage.getItem('sidebar-visible');
            if (saved !== null) {
                return JSON.parse(saved);
            }
        } catch (e) {
            logger.warn('[SidebarToggle] Failed to load state:', e);
        }

        // Default behavior: sidebar hidden by default
        return false;
    }

    /**
     * Watch for session becoming active (transition from new to active)
     * Disabled: Sidebar stays hidden by default, user must toggle manually
     */
    watchForSessionActivation() {
        // Disabled - sidebar should remain hidden unless user toggles
        // Users can click the toggle button to show sidebar when needed
        logger.debug('[SidebarToggle] Auto-show disabled - sidebar stays hidden by default');
    }
}

// Initialize when DOM is ready
let sidebarToggleManager = null;

function initSidebarToggle() {
    logger.debug('[SidebarToggle] Initializing manager');
    sidebarToggleManager = new SidebarToggleManager();
}

// Initialize
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initSidebarToggle);
} else {
    initSidebarToggle();
}

// Export for external use
export { sidebarToggleManager };
