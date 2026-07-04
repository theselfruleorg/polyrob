/**
 * Chat Manager for Task Agent WebView
 *
 * Manages chat-style display of agent progress and user interactions.
 */

import { escapeHtml, formatCost, on, emit, off, sessionState, logger, RateLimiter, ExponentialBackoff } from '/static/js/ui-utils.js?v=5';
import { eventStore } from '/static/js/event-store.js?v=5';
import { chatFilter } from '/static/js/event-filter.js?v=5';
import { rafThrottle } from '/static/js/performance-utils.js?v=5';
import { FileAttachmentManager } from '/static/js/file-attachments.js?v=5';

export class ChatManager {
    // Maximum messages to keep in memory to prevent unbounded growth
    static MAX_MESSAGES = 1000;

    constructor(sessionId) {  // Remove socket param - will be set later
        // Use centralized session state instead of manual tracking
        this.sessionId = sessionState.sessionId || sessionId;
        this.socket = null;  // Will be set by connectToSocket()
        this.isNewSession = sessionState.isNew;

        // State
        this.messages = [];
        this.currentAgentMessage = null;
        this.isStreaming = false;
        this.scrollLocked = false;

        // Track current agent message ID for proper event sequencing
        this.currentAgentMessageId = null;

        // Session state tracking (paused removed - use cancelled for interruption)
        this.sessionStatus = null;  // running, completed, cancelled, etc.

        // Action registry (from available_actions event)
        this.actionRegistry = {};  // { action_name: service_name }

        // Pending messages queue (messages sent but not yet confirmed by feed)
        this.pendingMessages = [];  // [{text, timestamp, id}]

        // Chunk-based message tracking (for discrete timestamped chunks)
        this._currentIteration = 0;  // Current iteration number for grouping
        this._chunkGroups = new Map();  // groupId -> {chunks: [], collapsed: false}

        // DOM elements
        this.chatContainer = document.getElementById('chat-messages');
        this.inputField = document.getElementById('chat-input');
        this.sendButton = document.getElementById('chat-send-btn');
        this.scrollButton = document.getElementById('scroll-to-bottom');
        this.emptyState = this.chatContainer?.querySelector('.chat-empty-state');

        // Track processed event IDs (EventStore handles dedup, this is for rendering)
        this._processedEventIds = new Set();

        // File attachment manager (will be initialized in init() after DOM is ready)
        this.fileManager = null;

        // Rate limiter for message sending (1 message per second)
        this.messageRateLimiter = new RateLimiter(1000);

        // Reconnection backoff
        this.reconnectBackoff = new ExponentialBackoff({ maxRetries: 10 });

        // Store bound event handlers for cleanup
        this._boundHandlers = {
            sendClick: null,
            inputKeydown: null,
            inputPaste: null,
            inputChange: null,
            scrollThrottled: null,
            scrollButtonClick: null,
            containerClick: null,
            documentKeydown: null,
            viewportResize: null
        };

        // Track if destroyed
        this._destroyed = false;

        // Initialize
        if (this.chatContainer && this.inputField && this.sendButton) {
            this.init();
        } else {
            logger.warn('[Chat] Required DOM elements not found, initialization skipped');
        }
    }

    init() {
        logger.debug('[Chat] Initializing for session:', this.sessionId, 'isNew:', this.isNewSession);

        // Initialize file attachment manager now that DOM is ready
        try {
            this.fileManager = new FileAttachmentManager();
            logger.debug('[Chat] ✓ FileAttachmentManager initialized');
        } catch (error) {
            logger.error('[Chat] ✗ FileAttachmentManager initialization FAILED:', error);
            this.fileManager = null;
        }

        // Check session ownership and set up read-only mode if needed
        this.checkOwnershipAndSetupReadOnly();

        // Set up UI listeners (always needed)
        this.setupUIListeners();

        // Set up custom event listeners IMMEDIATELY (before socket)
        // This ensures we receive events even if socket connects later
        this.setupCustomEventListeners();

        if (this.isNewSession) {
            // New session mode - don't load history but DO setup socket listeners
            // so we're ready when first message is sent
            logger.debug('[Chat] New session mode - waiting for user input');
            this.connectToSocket();  // Connect now so we're ready
        } else {
            // Active session mode - subscribe to EventStore and connect socket
            logger.debug('[Chat] Active session - subscribing to EventStore');
            this.subscribeToEventStore();
            this.loadInitialTask();
            this.connectToSocket();
        }
    }

    /**
     * Subscribe to EventStore for all event updates.
     * This replaces loadHistory() - EventStore is populated by session.js
     */
    subscribeToEventStore() {
        logger.debug('[Chat] Subscribing to EventStore');

        // Subscribe to all events
        this._eventStoreUnsubscribe = eventStore.subscribe('all', (change) => {
            if (change.action === 'insert') {
                this.processEventFromStore(change.event);
            } else if (change.action === 'batch') {
                // Process batch in order
                change.events.forEach(event => this.processEventFromStore(event));
            } else if (change.action === 'clear') {
                // Session reset - clear chat
                this.messages = [];
                this._processedEventIds.clear();
                if (this.chatContainer) {
                    this.chatContainer.innerHTML = '';
                }
            }
        });

        // Process any existing events in EventStore (from session.js initial load)
        const existingEvents = eventStore.getAll();
        if (existingEvents.length > 0) {
            logger.debug(`[Chat] Processing ${existingEvents.length} existing events from EventStore`);
            existingEvents.forEach(event => this.processEventFromStore(event));
        }
    }

    /**
     * Process a single event from EventStore
     * NOTE: Duplicate check and marking is handled by onFeedUpdate()
     */
    processEventFromStore(event) {
        if (!event || !event.type) return;

        // Update action registry from available_actions events (before filtering)
        if (event.type === 'available_actions' && event.data?.by_service) {
            this.updateActionRegistry(event.data.by_service);
        }

        // Process via onFeedUpdate which handles filtering and dedup
        this.onFeedUpdate(event);
    }

    /**
     * Load initial task from API (for existing sessions)
     */
    async loadInitialTask() {
        try {
            const response = await fetch(`/api/session/${this.sessionId}/task`);
            if (response.ok) {
                const data = await response.json();
                if (data.status === 'ok' && data.task) {
                    const timestamp = data.timestamp || (Date.now() / 1000);
                    this.addMessage(this._createMessage('user', data.task, {
                        id: 'msg_task_initial',
                        timestamp,
                        metadata: { isInitialTask: true }
                    }), { skipScroll: true });
                }
            }
        } catch (err) {
            logger.warn('[Chat] Failed to load initial task:', err);
        }
    }

    /**
     * Check session ownership and set up read-only mode for non-owners
     */
    checkOwnershipAndSetupReadOnly() {
        // Use centralized session state
        const isOwner = sessionState.isOwner;
        const isAuthenticated = sessionState.isAuthenticated;

        logger.debug('[Chat] Ownership check:', { isOwner, isAuthenticated, isNew: this.isNewSession });

        // If user is not the owner, disable input and show notice
        if (!isOwner && !this.isNewSession) {
            this.setReadOnlyMode(isAuthenticated);
        }
    }

    /**
     * Enable read-only mode (disable inputs for non-owners)
     */
    setReadOnlyMode(isAuthenticated) {
        logger.debug('[Chat] Setting read-only mode');

        // Hide the entire input wrapper
        const inputWrapper = document.querySelector('.chat-input-wrapper');
        if (inputWrapper) {
            inputWrapper.style.display = "none";
        }

        // Also hide attached files container and upload progress
        const attachedFilesContainer = document.getElementById('attached-files-container');
        if (attachedFilesContainer) {
            attachedFilesContainer.style.display = "none";
        }

        const uploadProgress = document.getElementById('upload-progress');
        if (uploadProgress) {
            uploadProgress.style.display = "none";
        }

        // Show read-only notice (will overlay the hidden input area)
        const readonlyNotice = document.getElementById('readonly-notice');
        const readonlyNoticeText = document.getElementById('readonly-notice-text');

        if (readonlyNotice && readonlyNoticeText) {
            if (isAuthenticated) {
                readonlyNoticeText.textContent = "📺 Viewing session in read-only mode. Only the session owner can send messages.";
            } else {
                readonlyNoticeText.innerHTML = '🔒 <a href="/signin" style="color: #ffc107; text-decoration: underline;">Sign in</a> to interact with this session.';
            }
            readonlyNotice.style.display = 'block';
        }
    }

    async connectToSocket() {
        try {
            if (window.socketReady) {
                logger.debug('[Chat] Waiting for socket...');
                this.socket = await window.socketReady;
                this.setupSocketListeners();
                this.reconnectBackoff.reset(); // Reset on successful connection
                logger.debug('[Chat] Socket connected');
            } else {
                // Use exponential backoff for retries
                const delay = this.reconnectBackoff.nextDelay();
                if (delay === null) {
                    logger.error('[Chat] Max reconnection attempts reached');
                    this.showError('Unable to connect. Please refresh the page.');
                    return;
                }
                logger.warn(`[Chat] Socket not available, retrying in ${delay}ms`);
                setTimeout(() => this.connectToSocket(), delay);
            }
        } catch (err) {
            logger.error('[Chat] Socket connection error:', err);
            // Use backoff for error retries too
            const delay = this.reconnectBackoff.nextDelay();
            if (delay === null) {
                this.showError('Connection failed. Please refresh the page.');
                return;
            }
            this.showError(`Connection error, retrying in ${Math.round(delay/1000)}s...`);
            setTimeout(() => this.connectToSocket(), delay);
        }
    }

    /**
     * Set up custom event listeners (always ready, even before socket)
     */
    setupCustomEventListeners() {
        logger.debug('[Chat] Setting up custom event listeners');

        // NOTE: Feed updates come via EventStore subscription (subscribeToEventStore)
        // The chat:feedUpdate event bus is NO LONGER used to avoid duplicate processing

        // Listen for tab activation events - save handler for cleanup
        this._boundHandlers.tabActivated = (event) => {
            if (event.detail.tabId === 'chat-tab') {
                logger.debug('[Chat] Tab activated - scrolling to bottom');
                this.scrollToBottom();
            }
        };
        on('tab:activated', this._boundHandlers.tabActivated);

        logger.debug('[Chat] ✅ Custom event listeners ready');
    }

    // === UTILITY METHODS ===

    /**
     * Format timestamp to HH:MM:SS time string
     */
    _formatTime(timestamp) {
        return new Date(timestamp * 1000).toLocaleTimeString('en-US', {
            hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false
        });
    }

    /**
     * Render thinking indicator HTML
     */
    _renderThinkingIndicator(withEscHint = false) {
        const base = '<span class="thinking-text">thinking</span><span class="thinking-dots"><span>.</span><span>.</span><span>.</span></span>';
        return withEscHint ? `${base} <span class="esc-hint">(esc to interrupt)</span>` : base;
    }

    /**
     * Render message header (time + author)
     */
    _renderMessageHeader(time, author = 'agent') {
        return `<span class="message-time">[${time}]</span><span class="message-author">&lt;${author}&gt;</span>`;
    }

    /**
     * Create a message object with standard structure
     */
    _createMessage(type, text, overrides = {}) {
        return {
            id: `msg_${type}_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`,
            type,
            text,
            timestamp: Date.now() / 1000,
            ...overrides
        };
    }

    /**
     * Reset current agent message state
     */
    _resetAgentMessageState() {
        this.currentAgentMessage = null;
        this.currentAgentMessageId = null;
        this.isStreaming = false;
    }

    /**
     * Clean up orphan thinking indicators from DOM
     */
    _cleanupOrphanThinkingIndicators() {
        const orphans = document.querySelectorAll('.step-thinking-dots, .agent-message .thinking-dots');
        if (orphans.length > 0) {
            logger.debug('[Chat] Cleaning up', orphans.length, 'orphan thinking indicators');
            orphans.forEach(el => el.remove());
        }
    }

    /**
     * Update action registry from available_actions event
     */
    updateActionRegistry(byService) {
        logger.debug('[Chat] Updating action registry from available_actions');

        // byService format: { service_name: [action1, action2, ...] }
        for (const [serviceName, actions] of Object.entries(byService)) {
            if (Array.isArray(actions)) {
                actions.forEach(action => {
                    // Handle both string and object formats
                    const actionName = typeof action === 'string' ? action : action.name || action.action_type;
                    if (actionName) {
                        this.actionRegistry[actionName.toLowerCase()] = serviceName.toLowerCase();
                    }
                });
            }
        }

        logger.debug('[Chat] Action registry updated:', Object.keys(this.actionRegistry).length, 'actions');
    }

    /**
     * Set up socket-specific listeners (streaming)
     */
    setupSocketListeners() {
        logger.debug('[Chat] Setting up socket listeners for session:', this.sessionId);

        if (!this.socket) {
            logger.warn('[Chat] No socket available for listeners');
            return;
        }

        // Streaming chunks (live updates during agent thinking)
        this.socket.on('stream_chunk', (data) => {
            logger.debug('[Chat] Received stream_chunk for session:', data.session_id);

            if (data.session_id === this.sessionId) {
                this.onStreamChunk(data);
            }
        });

        // Also listen for 'streaming_output' (alternative event name)
        this.socket.on('streaming_output', (data) => {
            logger.debug('[Chat] Received streaming_output for session:', data.session_id);

            if (data.session_id === this.sessionId) {
                this.onStreamChunk(data);
            }
        });

        // Also listen for 'stream_update' (emitted by SSE-to-WS bridge)
        this.socket.on('stream_update', (data) => {
            logger.debug('[Chat] Received stream_update for session:', data.session_id);

            if (data.session_id === this.sessionId) {
                this.onStreamChunk(data);
            }
        });

        logger.debug('[Chat] Socket listeners ready');
    }

    /**
     * Check if chat tab is currently active
     */
    isChatTabActive() {
        const chatTab = document.getElementById('chat-tab');
        return chatTab && chatTab.classList.contains('active');
    }

    setupUIListeners() {
        // Store bound handlers for cleanup

        // Send button
        this._boundHandlers.sendClick = () => this.sendMessage();
        this.sendButton.addEventListener('click', this._boundHandlers.sendClick);

        // Enter key
        this._boundHandlers.inputKeydown = (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                this.sendMessage();
            }
        };
        this.inputField.addEventListener('keydown', this._boundHandlers.inputKeydown);

        // Clipboard paste support for images
        this._boundHandlers.inputPaste = async (e) => {
            const items = e.clipboardData?.items;
            if (!items) return;

            logger.debug('[Chat] Paste event detected, checking for images...');

            for (const item of items) {
                if (item.type.startsWith('image/')) {
                    e.preventDefault(); // Prevent default text paste

                    logger.debug('[Chat] Image pasted from clipboard:', item.type);

                    // Get image file from clipboard
                    const imageFile = item.getAsFile();
                    if (imageFile && this.fileManager) {
                        try {
                            this.fileManager.addFile(imageFile);
                            logger.debug('[Chat] ✓ Pasted image added to attachments');
                        } catch (error) {
                            logger.error('[Chat] ✗ Failed to add pasted image:', error);
                        }
                    }
                }
            }
        };
        this.inputField.addEventListener('paste', this._boundHandlers.inputPaste);

        // Auto-resize textarea as user types
        this._boundHandlers.inputChange = () => this.autoResizeTextarea();
        this.inputField.addEventListener('input', this._boundHandlers.inputChange);

        // Scroll detection (throttled for performance)
        this._boundHandlers.scrollThrottled = rafThrottle(() => this.onScroll());
        this.chatContainer.addEventListener('scroll', this._boundHandlers.scrollThrottled);

        // Scroll to bottom button
        if (this.scrollButton) {
            this._boundHandlers.scrollButtonClick = () => this.scrollToBottom(true);
            this.scrollButton.addEventListener('click', this._boundHandlers.scrollButtonClick);
        }

        // Action expand/collapse (event delegation on chat container)
        this._boundHandlers.containerClick = (e) => {
            // Find the clicked action line (might be nested in spans)
            const actionLine = e.target.closest('.step-action-line');
            if (actionLine) {
                this.toggleActionDetails(actionLine);
            }
        };
        this.chatContainer.addEventListener('click', this._boundHandlers.containerClick);

        // ESC key listener for cancel (interrupt)
        // Note: R key for resume removed - sessions auto-resume on new message
        this._boundHandlers.documentKeydown = (e) => {
            if (e.key === 'Escape') {
                this.handleEscapeKey(e);
            }
        };
        document.addEventListener('keydown', this._boundHandlers.documentKeydown);

        // Mobile keyboard handling - adjust input position when keyboard appears
        this.setupMobileKeyboardHandling();
    }

    setupMobileKeyboardHandling() {
        // Only set up keyboard handling if visualViewport API is available (modern mobile browsers)
        if ('visualViewport' in window) {
            const chatInputContainer = document.querySelector('.chat-input-container');
            if (!chatInputContainer) return;

            // Track the original bottom position
            let originalBottom = null;

            // Store bound handler for cleanup
            this._boundHandlers.viewportResize = () => {
                // Calculate how much the keyboard is taking up
                const viewportHeight = window.visualViewport.height;
                const windowHeight = window.innerHeight;
                const keyboardHeight = windowHeight - viewportHeight;

                if (keyboardHeight > 0) {
                    // Keyboard is visible - move input up
                    if (originalBottom === null) {
                        originalBottom = chatInputContainer.style.bottom || '0';
                    }
                    chatInputContainer.style.bottom = `${keyboardHeight}px`;
                    chatInputContainer.style.position = 'fixed';
                } else {
                    // Keyboard is hidden - restore original position
                    if (originalBottom !== null) {
                        chatInputContainer.style.bottom = originalBottom;
                        originalBottom = null;
                    }
                }
            };
            window.visualViewport.addEventListener('resize', this._boundHandlers.viewportResize);

            logger.debug('[Chat] Mobile keyboard handling initialized');
        }
    }

    /**
     * Auto-resize textarea based on content
     */
    autoResizeTextarea() {
        if (!this.inputField) return;

        // Reset height to minimum to get accurate scrollHeight
        this.inputField.style.height = '28px';

        // Calculate new height based on content
        const scrollHeight = this.inputField.scrollHeight;

        // Set height to content height (CSS max-height will constrain it)
        this.inputField.style.height = `${scrollHeight}px`;
    }

    /**
     * Toggle action details expansion
     */
    toggleActionDetails(actionLine) {
        const actionId = actionLine.getAttribute('data-action-id');
        if (!actionId) return;

        const detailsEl = document.getElementById(actionId);
        if (!detailsEl) return;

        // Toggle expanded class
        if (detailsEl.classList.contains('expanded')) {
            detailsEl.classList.remove('expanded');
        } else {
            detailsEl.classList.add('expanded');
        }
    }

    /**
     * Handle ESC key press - cancel/interrupt agent
     * Note: Pause functionality removed. ESC now cancels the session.
     * To continue, user sends a new message which auto-resumes.
     */
    async handleEscapeKey(event) {
        // Only handle if we're in chat tab and session is running
        if (!this.isChatTabActive()) {
            logger.debug('[Chat] ESC pressed but chat tab not active');
            return;
        }

        logger.debug('[Chat] ESC pressed - sessionStatus:', this.sessionStatus, 'isNewSession:', this.isNewSession);

        if (this.sessionStatus === 'running' && !this.isNewSession) {
            event.preventDefault();
            logger.debug('[Chat] ✅ ESC CANCELLING SESSION');
            await this.cancelSession();
        } else {
            logger.debug('[Chat] ⏭️ ESC ignored - not running or new session');
        }
    }

    /**
     * Cancel the current session via API
     * Note: This replaces the old pauseSession method.
     * Users can send a new message to continue with modifications.
     */
    async cancelSession() {
        const originalStatus = this.sessionStatus;

        try {
            logger.debug('[Chat] 🛑 Calling cancel API for session:', this.sessionId);

            // Optimistic update
            this.sessionStatus = 'cancelled';

            const response = await fetch(`/api/task/sessions/${this.sessionId}/cancel`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' }
            });

            logger.debug('[Chat] Cancel API response status:', response.status);

            if (!response.ok) {
                // Parse error response for better message
                let errorMsg = `HTTP ${response.status}`;
                try {
                    const errorData = await response.json();
                    errorMsg = errorData.detail || errorData.message || errorMsg;
                } catch (e) {
                    // If not JSON, try text
                    try {
                        errorMsg = await response.text() || errorMsg;
                    } catch (e2) {}
                }

                if (response.status === 404) {
                    throw new Error('Session not found');
                } else if (response.status === 400) {
                    throw new Error(errorMsg || 'Cannot cancel: session may already be stopped');
                }
                throw new Error(errorMsg);
            }

            // Show system message
            this.addMessage(this._createMessage('system', '⏹️ Session cancelled. Send a message to continue.'));

            logger.debug('[Chat] ✅ Session cancelled successfully');

        } catch (error) {
            // Rollback on failure
            this.sessionStatus = originalStatus;
            logger.error('[Chat] ❌ Error cancelling session:', error);
            this.showError('Failed to cancel session: ' + error.message);
        }
    }

    /**
     * Create immediate thinking message when user sends a message
     * Shows <agent> thinking: 🧠 instantly
     *
     * MESSAGE FORMAT v2: Uses hierarchical steps structure
     * - steps: Array of {stepNumber, timestamp, thinking, actions, status}
     * - Each thinking update creates a new step
     * - Actions are grouped within their step
     */
    createImmediateThinkingMessage() {
        // Simplified for chunk-based approach:
        // Just mark session as running and prepare for new chunks
        logger.debug('[Chat] 🧠 Session marked as running, ready for chunks');

        // Mark session as running so ESC can interrupt
        this.sessionStatus = 'running';

        // Start new iteration group if not already tracking
        if (this._currentIteration === 0) {
            this._currentIteration = 1;
        }

        // Legacy cleanup
        if (this.currentAgentMessage) {
            this._resetAgentMessageState();
        }

        this.isStreaming = false;
        logger.debug('[Chat] ✅ Session ready for chunks, iteration:', this._currentIteration);
    }
    
    /**
     * Clean up thinking state on cancellation or error.
     * Preserves the thinking message with a "cancelled" visual state instead of removing it.
     * This keeps visibility into what the agent was doing before interruption.
     */
    cleanupThinkingState() {
        logger.debug('[Chat] Cleaning up thinking state');

        // If there's a current thinking message, mark it as cancelled (don't remove)
        if (this.currentAgentMessage && this.currentAgentMessage.status === 'thinking') {
            // Mark as cancelled in state
            this.currentAgentMessage.status = 'cancelled';

            // Update the DOM to show cancelled state
            const messageElement = this.chatContainer?.querySelector(`[data-message-id="${this.currentAgentMessage.id}"]`);
            if (messageElement) {
                // Remove thinking indicators
                const thinkingIndicators = messageElement.querySelectorAll('.thinking-dots, .step-thinking-dots');
                thinkingIndicators.forEach(el => el.remove());

                // Add cancelled indicator if there are iterations to show
                if (this.currentAgentMessage.iterations?.length > 0) {
                    // Re-render with cancelled status
                    messageElement.innerHTML = this.renderAgentMessage(this.currentAgentMessage);
                    messageElement.classList.add('message-cancelled');
                } else {
                    // No meaningful content - remove empty message
                    messageElement.remove();
                    const idx = this.messages.findIndex(m => m.id === this.currentAgentMessage.id);
                    if (idx !== -1) {
                        this.messages.splice(idx, 1);
                    }
                }
            }
        }

        // Reset state (but message remains visible if it had content)
        this._resetAgentMessageState();
    }


    /**
     * Load chat history - delegates to EventStore subscription
     * @deprecated Use subscribeToEventStore() directly. This exists for backwards compatibility.
     */
    async loadHistory() {
        logger.debug('[Chat] loadHistory() called - delegating to EventStore subscription');

        // Subscribe to EventStore if not already subscribed
        if (!this._eventStoreUnsubscribe) {
            this.subscribeToEventStore();
        }

        // Load initial task
        await this.loadInitialTask();

        // Scroll to bottom
        this.scrollToBottom();
    }

    onFeedUpdate(feedEvent, options = {}) {
        logger.debug('[Chat] 📥 Processing event:', feedEvent.type, feedEvent);

        // Skip filtered events (for events coming via chat:feedUpdate event bus)
        if (!chatFilter.shouldShow(feedEvent)) {
            logger.debug('[Chat] ⏭️  Event filtered:', feedEvent.type);
            return;
        }

        // Skip duplicates (using _id for deduplication)
        const eventId = feedEvent._id || `${feedEvent.type}_${feedEvent.timestamp || Date.now()}`;
        if (this._processedEventIds.has(eventId)) {
            logger.debug('[Chat] ⏭️  Event duplicate:', feedEvent.type);
            return;
        }

        // Mark as processed
        this._processedEventIds.add(eventId);

        logger.debug('[Chat] ✅ Event passed filters, processing:', feedEvent.type);

        // Route events to appropriate handlers
        const eventType = feedEvent.type;

        switch (eventType) {
            // User messages
            case 'user_message':
                this.handleUserMessage(feedEvent);
                break;

            // Agent thinking/step events → update current thinking message
            case 'step':
            case 'agent_step':
            case 'planner':
            case 'evaluation':
            case 'task_progress':
                this.handleThinkingUpdate(feedEvent);
                break;

            // Final agent output → complete the thinking message
            case 'agent_message':
            case 'final_message':
            case 'result':
                this.handleFinalMessage(feedEvent);
                break;

            // Session/system status events
            case 'session_halted':
            case 'session_done':
            case 'task_complete':
                this.handleFinalMessage(feedEvent);  // These also contain final text
                break;
            
            // CRITICAL: Handle status events to keep sessionStatus synced
            case 'status':
                this.handleStatusEvent(feedEvent);
                break;
                
            case 'status_update':
            case 'session_start':
            case 'session_completion':
                this.handleSystemMessage(feedEvent);
                break;

            // Technical events (filtered in addMessage, but handle gracefully)
            case 'llm_request':
            case 'tool_execution':
                // Skip - these clutter the chat
                break;

            case 'error':
                this.handleErrorEvent(feedEvent);
                break;

            case 'multi_agent_relationship':
                this.handleMultiAgentRelationship(feedEvent);
                break;

            case 'available_actions':
                // Already handled by updateActionRegistry()
                break;

            // Queue status updates for the queue indicator
            case 'queue_status':
                this.handleQueueStatus(feedEvent);
                break;

            // Session cancelled event from backend (paused/resumed removed)
            case 'session_cancelled':
                this.handleSessionCancelled(feedEvent);
                break;

            // DEPRECATED: These events are no longer emitted but kept for backwards compatibility
            case 'session_paused':
                // Treat paused as cancelled for backwards compatibility
                this.handleSessionCancelled(feedEvent);
                break;

            case 'session_resumed':
                // Sessions now auto-resume on new message - ignore this event
                logger.debug('[Chat] Ignoring deprecated session_resumed event');
                break;

            // Iteration complete - marks end of an iteration with file info
            case 'iteration_complete':
                this.handleIterationComplete(feedEvent);
                break;

            default:
                // Unknown events - check if they have thinking data
                const data = feedEvent.data || {};
                if (data.reasoning || data.thought || data.task_progress || data.actions || data.plan) {
                    this.handleThinkingUpdate(feedEvent);
                }
                break;
        }
    }
    
    handleUserMessage(event) {
        const data = event.data || {};
        const messageText = data.text || data.message || '';

        // Remove from pending queue (feed event confirms it was received)
        this._removeFromPendingQueue(messageText);

        // FIX: Check if a user message with the same text already exists (prevents duplicates)
        // This happens when loadHistory() adds the initial task, then feed events also contain it
        const normalizedText = messageText.trim().toLowerCase();
        const isDuplicate = this.messages.some(msg =>
            msg.type === 'user' &&
            (msg.text || msg.content || '').trim().toLowerCase() === normalizedText
        );

        if (isDuplicate) {
            logger.debug('[Chat] ⏭️ Skipping duplicate user message:', messageText.substring(0, 50));
            return;
        }

        logger.debug('[Chat] 👤 User message from backend:', messageText.substring(0, 50));

        // Start a new iteration group for the upcoming agent response
        this._currentIteration++;
        logger.debug('[Chat] 📊 New iteration group:', this._currentIteration);

        // Trigger auto-collapse of previous groups
        this._checkAutoCollapse();

        // Add the user message with proper seq for ordering
        const userMsg = this._createMessage('user', messageText, {
            id: `msg_user_${event._id || event.timestamp}`,
            timestamp: event._ts_ms ? event._ts_ms / 1000 : (event.timestamp || Date.now() / 1000),
            seq: event._seq || (event._ts_ms || Date.now())  // Use _seq for ordering
        });

        this.addMessage(userMsg);

        // Mark session as running for new task
        this.sessionStatus = 'running';

        // Legacy cleanup (for backwards compatibility)
        if (this.currentAgentMessage) {
            this._resetAgentMessageState();
        }
    }

    /**
     * Handle status events from backend to keep sessionStatus synced
     * This is CRITICAL for proper thinking indicator behavior on session continuation
     */
    handleStatusEvent(event) {
        const data = event.data || {};
        const newStatus = (data.status || '').toLowerCase();
        
        logger.debug('[Chat] 📊 Status event received:', newStatus, 'current:', this.sessionStatus);
        
        if (!newStatus) return;
        
        // Update sessionStatus based on backend status
        const previousStatus = this.sessionStatus;
        
        if (newStatus === 'running' || newStatus === 'active' || newStatus === 'resumed') {
            this.sessionStatus = 'running';

            // If transitioning TO running and we don't have a thinking message, create one
            // This handles the case where session resumes from backend (after completed or cancelled)
            if ((previousStatus === 'completed' || previousStatus === 'cancelled') && !this.currentAgentMessage) {
                logger.debug('[Chat] 🔄 Session resumed from', previousStatus, ', creating thinking message');
                this.createImmediateThinkingMessage();
            }
        } else if (newStatus === 'completed' || newStatus === 'done' || newStatus === 'success') {
            this.sessionStatus = 'completed';
            
            // FIX: When status changes to completed, ensure all thinking indicators are removed
            // This handles cases where handleFinalMessage might have missed them
            if (this.currentAgentMessage && this.currentAgentMessage.status === 'thinking') {
                logger.debug('[Chat] 📊 Status event forcing completion of current message');
                this.currentAgentMessage.status = 'completed';
                
                const msgEl = document.querySelector(`[data-message-id="${this.currentAgentMessage.id}"]`);
                if (msgEl) {
                    msgEl.innerHTML = this.renderAgentMessage(this.currentAgentMessage);
                }
                
                this.currentAgentMessage = null;
                this.currentAgentMessageId = null;
            }
            
            // Also clean up any orphan thinking indicators
            this._cleanupOrphanThinkingIndicators();
        } else if (newStatus === 'cancelled' || newStatus === 'suspended') {
            // Note: 'paused' status removed - use 'cancelled' for user interruption
            this.sessionStatus = 'cancelled';
            this.cleanupThinkingState();
        } else if (newStatus === 'error' || newStatus === 'failed') {
            this.sessionStatus = 'error';
            // Clean up any pending thinking state on error
            this.cleanupThinkingState();
        }
        
        logger.debug('[Chat] 📊 sessionStatus updated:', previousStatus, '→', this.sessionStatus);
    }

    /**
     * Handle queue status updates to show queue indicator in UI
     * Shows pending messages as a compact list
     */
    handleQueueStatus(event) {
        const data = event.data || {};
        const queuedCount = data.queued_count || 0;
        const processing = data.processing || false;

        logger.debug('[Chat] 📨 Queue status update:', queuedCount, 'queued, processing:', processing);

        // Update queue indicator with our pending messages
        this._updateQueueIndicator(processing);
    }

    /**
     * Add a message to the pending queue display
     * Shows immediately in UI before feed event confirms it
     */
    _addToPendingQueue(text) {
        const pendingMsg = {
            id: `pending_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
            text: text,
            timestamp: Date.now()
        };
        this.pendingMessages.push(pendingMsg);
        logger.debug('[Chat] Added to pending queue:', pendingMsg.id, 'total:', this.pendingMessages.length);
        this._updateQueueIndicator(false);
    }

    /**
     * Remove a message from pending queue when feed event arrives
     */
    _removeFromPendingQueue(text) {
        const normalizedText = (text || '').trim().toLowerCase();
        const idx = this.pendingMessages.findIndex(m =>
            (m.text || '').trim().toLowerCase() === normalizedText
        );
        if (idx !== -1) {
            const removed = this.pendingMessages.splice(idx, 1)[0];
            logger.debug('[Chat] Removed from pending queue:', removed.id, 'remaining:', this.pendingMessages.length);
            this._updateQueueIndicator(false);
        }
    }

    /**
     * Update the queue indicator UI with pending messages
     * Compact inline display aligned with input area
     */
    _updateQueueIndicator(processing) {
        let queueIndicator = document.getElementById('chat-queue-indicator');

        const hasPending = this.pendingMessages.length > 0 || processing;

        if (hasPending) {
            if (!queueIndicator) {
                // Create queue indicator if it doesn't exist
                queueIndicator = document.createElement('div');
                queueIndicator.id = 'chat-queue-indicator';
                queueIndicator.className = 'queue-indicator';

                // Insert inside input container, before the wrapper
                const inputContainer = document.querySelector('.chat-input-container');
                const inputWrapper = document.querySelector('.chat-input-wrapper');
                if (inputContainer && inputWrapper) {
                    inputContainer.insertBefore(queueIndicator, inputWrapper);
                }
            }

            // Build compact inline display
            let html = '<span class="queue-header">';
            if (processing) {
                html += '<span class="queue-processing">processing</span>';
            } else {
                html += `<span class="queue-pending">${this.pendingMessages.length} queued</span>`;
            }
            html += '</span>';

            if (this.pendingMessages.length > 0) {
                html += '<ul class="queue-list">';
                for (const msg of this.pendingMessages) {
                    // Truncate long messages
                    const truncated = msg.text.length > 40
                        ? msg.text.substring(0, 40) + '…'
                        : msg.text;
                    html += `<li class="queue-item">${escapeHtml(truncated)}</li>`;
                }
                html += '</ul>';
            }

            queueIndicator.innerHTML = html;
            queueIndicator.style.display = 'flex';
        } else {
            // Hide indicator when queue is empty
            if (queueIndicator) {
                queueIndicator.style.display = 'none';
            }
        }
    }

    /**
     * Handle session cancelled event from backend feed
     * Updates sessionStatus and shows UI feedback
     * Note: This replaces the old handleSessionPaused method
     */
    handleSessionCancelled(event) {
        const data = event.data || {};
        logger.debug('[Chat] 📥 Received session_cancelled event:', data);

        // Update session status
        this.sessionStatus = 'cancelled';

        // Clean up any thinking state
        this.cleanupThinkingState();

        // Only show message if we didn't already show one via API call
        // Check if the last message is already a cancel message
        const messages = this.chatContainer?.querySelectorAll('.chat-message');
        if (messages && messages.length > 0) {
            const lastMsg = messages[messages.length - 1];
            const lastText = lastMsg?.textContent || '';
            if (lastText.includes('Session cancelled')) {
                logger.debug('[Chat] Skipping duplicate cancel message');
                return;
            }
        }

        // Show system message for remote cancel events
        this.addMessage(this._createMessage('system', '⏹️ Session cancelled. Send a message to continue.', {
            id: `msg_system_cancel_${event.timestamp}`,
            timestamp: event.timestamp || (Date.now() / 1000)
        }));
    }

    handleSystemMessage(event) {
        const data = event.data || {};

        // Extract message text from various possible fields
        let messageText = data.text || data.message || data.status || '';

        // Add context based on event type
        if (event.type === 'session_halted') {
            messageText = messageText || 'Session halted';
        } else if (event.type === 'session_done') {
            messageText = messageText || 'Session completed';
        } else if (event.type === 'task_complete') {
            messageText = messageText || 'Task completed';
        }

        // Create system message
        this.addMessage(this._createMessage('system', messageText, {
            id: `msg_system_${event.timestamp}`,
            timestamp: event.timestamp || (Date.now() / 1000)
        }));
    }

    handleErrorEvent(event) {
        logger.debug('[Chat] handleErrorEvent:', event);
        const data = event.data || {};

        // Clean up thinking state on error - stop showing running indicator
        if (this.currentAgentMessage && this.currentAgentMessage.status === 'thinking') {
            this.currentAgentMessage.status = 'error';
            // Re-render to remove thinking indicator
            const msgEl = document.querySelector(`[data-message-id="${this.currentAgentMessage.id}"]`);
            if (msgEl) {
                msgEl.innerHTML = this.renderAgentMessage(this.currentAgentMessage);
            }
        }

        const errorMessage = {
            id: `msg_error_${event.timestamp}`,
            type: 'error',
            timestamp: event.timestamp || (Date.now() / 1000),
            errorType: data.error_type || 'Error',
            errorMessage: data.error_message || 'Unknown error',
            errorStack: data.error_stack,
            recoverable: data.recoverable !== false,
            context: data.context || {}
        };

        this.addMessage(errorMessage);
    }

    handleMultiAgentRelationship(event) {
        logger.debug('[Chat] handleMultiAgentRelationship:', event);
        const data = event.data || {};

        const relationshipMessage = {
            id: `msg_relationship_${event.timestamp}`,
            type: 'multi_agent_relationship',
            timestamp: event.timestamp || (Date.now() / 1000),
            agents: data.agent_details || [],
            orchestratorType: data.orchestrator_type || 'default'
        };

        this.addMessage(relationshipMessage);
    }
    
    handleFinalMessage(event) {
        logger.debug('[Chat] handleFinalMessage called with event:', event);
        const data = event.data || {};

        // Extract the final message text from various possible fields
        let messageText = data.text || data.message || data.result || data.response || '';

        // For task_complete/session_done, extract text if present
        if (!messageText && (event.type === 'task_complete' || event.type === 'session_done')) {
            messageText = data.final_message || data.summary || '';
        }

        logger.debug('[Chat] Final message text:', messageText ? messageText.substring(0, 100) : 'EMPTY');

        // Mark session as completed (disable ESC interrupt)
        this.sessionStatus = 'completed';

        // Create final response chunk if there's text
        if (messageText) {
            const chunkId = `chunk_final_${event._id || event._seq || Date.now()}`;

            const chunk = {
                id: chunkId,
                type: 'agent_chunk',
                chunkType: 'response',
                seq: event._seq || (event._ts_ms || Date.now()),
                timestamp: event._ts_ms ? event._ts_ms / 1000 : (event.timestamp || Date.now() / 1000),
                groupId: `group_${this._currentIteration}`,
                iterationNumber: this._currentIteration,
                data: { text: messageText }
            };

            logger.debug('[Chat] 📝 Creating final response chunk:', chunk.id);
            this.addMessage(chunk);
        }

        // Reset iteration state for next task
        this._resetIterationState();

        // Trigger auto-collapse of completed groups
        this._checkAutoCollapse();

        // Legacy cleanup (for backwards compatibility)
        if (this.currentAgentMessage) {
            this._resetAgentMessageState();
        }
        this._cleanupOrphanThinkingIndicators();
    }
    
    /**
     * Handle thinking update events from the agent
     *
     * MESSAGE FORMAT v3: Creates hierarchical iterations with file tracking
     * - Each event creates/updates an iteration object
     * - Iterations contain: iterationNumber, type, status, reasoning, actions[], files
     * - Supports different iteration types: thinking, browser, filesystem, mcp, mixed, done
     */
    handleThinkingUpdate(event) {
        logger.debug('[Chat] handleThinkingUpdate:', event.type, 'sessionStatus=', this.sessionStatus);

        // GUARD: Don't create new messages if session is marked completed
        if (this.sessionStatus === 'completed') {
            logger.debug('[Chat] Ignoring thinking update - session completed');
            return;
        }

        // Mark session as running
        this.sessionStatus = 'running';

        const data = event.data || {};

        // Update current iteration number for grouping
        const iterationNumber = data.iteration || data.step || this._currentIteration || 1;
        if (iterationNumber > this._currentIteration) {
            this._currentIteration = iterationNumber;
        }

        // Generate unique chunk ID from event
        const chunkId = `chunk_${event._id || event._seq || Date.now()}_${Math.random().toString(36).slice(2, 6)}`;

        // Check for duplicate events (using _id if available)
        if (event._id && this._processedEventIds.has(event._id)) {
            logger.debug('[Chat] ⏭️ Skipping duplicate event:', event._id);
            return;
        }
        if (event._id) {
            this._processedEventIds.add(event._id);
        }

        // Create discrete chunk message
        const chunk = {
            id: chunkId,
            type: 'agent_chunk',
            chunkType: this._determineChunkType(event),
            seq: event._seq || (event._ts_ms || Date.now()),  // Use _seq for ordering
            timestamp: event._ts_ms ? event._ts_ms / 1000 : (event.timestamp || Date.now() / 1000),
            groupId: `group_${iterationNumber}`,
            iterationNumber: iterationNumber,
            data: data
        };

        logger.debug('[Chat] 📝 Creating chunk:', chunk.chunkType, 'seq:', chunk.seq, 'group:', chunk.groupId);

        // Add as discrete message - will be inserted at correct chronological position
        this.addMessage(chunk);

        // Auto-scroll if not locked
        if (!this.scrollLocked) {
            this.scrollToBottom(true);
        }
    }

    /**
     * Handle iteration complete event - finalizes an iteration with file info
     */
    handleIterationComplete(event) {
        const data = event.data || {};
        const iterationNumber = data.iteration || 1;

        if (!this.currentAgentMessage) {
            logger.warn('[Chat] iteration_complete but no current message');
            return;
        }

        // Ensure iterations array exists (safety for older message formats)
        if (!this.currentAgentMessage.iterations) {
            this.currentAgentMessage.iterations = [];
        }

        // Find the iteration
        let iteration = this.currentAgentMessage.iterations.find(
            i => i.iterationNumber === iterationNumber
        );

        if (!iteration) {
            // Create if missing (late event) - actions come from step event, not here
            iteration = {
                iterationNumber,
                timestamp: event.timestamp || (Date.now() / 1000),
                type: data.iteration_type || 'mixed',
                status: data.iteration_status || 'completed',
                reasoning: data.reasoning_summary || '',
                actions: [],  // Actions come from step event
                filesCreated: data.files_created || [],
                filesModified: data.files_modified || [],
                filesRead: data.files_read || [],
                error: data.error || null,
                isDone: data.is_done || false
            };
            this.currentAgentMessage.iterations.push(iteration);
        } else {
            // Update existing iteration with completion status
            iteration.type = data.iteration_type || iteration.type;
            iteration.status = data.iteration_status || 'completed';
            iteration.filesCreated = data.files_created || iteration.filesCreated;
            iteration.filesModified = data.files_modified || iteration.filesModified;
            iteration.filesRead = data.files_read || iteration.filesRead;
            iteration.error = data.error || iteration.error;
            iteration.isDone = data.is_done || iteration.isDone;
            // Note: Don't touch actions - they come from step event only
        }

        logger.debug('[Chat] ✅ Iteration', iterationNumber, 'completed with status:', iteration.status);

        this.updateThinkingDisplay();
    }

    /**
     * Classify iteration type based on actions
     */
    classifyIterationType(actions) {
        if (!actions || actions.length === 0) {
            return 'thinking';
        }

        const services = new Set(actions.map(a => a.service).filter(Boolean));

        // Check for done action
        if (actions.some(a => a.name === 'done')) {
            return 'done';
        }

        if (services.size === 1) {
            const service = services.values().next().value;
            if (service === 'browser') return 'browser';
            if (service === 'filesystem') return 'filesystem';
            if (service === 'mcp') return 'mcp';
        }

        return 'mixed';
    }

    /**
     * Generate a unique key for an action to prevent duplicates
     * Uses service:name:normalized_params for reliable deduplication
     */
    getActionKey(action) {
        const service = action.service || this.detectService(action.name || action.action_type);
        const name = action.name || action.action_type || 'unknown';
        
        // Extract key identifying params (sorted for consistency)
        const params = action.params || {};
        const keyParams = {};
        
        // Priority params that uniquely identify actions
        const priorityKeys = ['url', 'file_path', 'path', 'query', 'text', 'selector', 'element', 'index', 'message'];
        for (const key of priorityKeys) {
            if (params[key] !== undefined) {
                keyParams[key] = params[key];
            }
        }
        
        // If no priority params, use first few params (sorted by key)
        if (Object.keys(keyParams).length === 0) {
            const sortedKeys = Object.keys(params).sort().slice(0, 3);
            for (const key of sortedKeys) {
                keyParams[key] = params[key];
            }
        }
        
        // Create a normalized string with sorted keys
        const sortedParams = Object.keys(keyParams)
            .sort()
            .map(k => `${k}=${String(keyParams[k]).substring(0, 100)}`)
            .join('|');
        
        return `${service}:${name}:${sortedParams}`;
    }
    
    parseAction(action) {
        let service, name, params;

        // Format 1: {service: "x", name: "y", params: {...}} - processed format from AgentStepFormatter
        if (action.service && (action.name || action.action_type)) {
            service = action.service;
            name = action.name || action.action_type;
            // Use params directly if it exists, otherwise collect other keys
            if (action.params && typeof action.params === 'object') {
                params = action.params;
            } else {
                params = {};
                Object.keys(action).forEach(key => {
                    if (key !== 'service' && key !== 'name' && key !== 'action_type' && key !== 'status' && key !== 'params') {
                        params[key] = action[key];
                    }
                });
            }
        }
        // Format 2: {action_name: {params}} - raw format from model_dump()
        else if (typeof action === 'object') {
            const actionKeys = Object.keys(action).filter(k => k !== 'status');
            if (actionKeys.length > 0) {
                const actionName = actionKeys[0];
                name = actionName;
                service = this.detectService(actionName);
                params = typeof action[actionName] === 'object' ? action[actionName] : {};
            } else {
                return null;
            }
        }
        // Format 3: string action name
        else if (typeof action === 'string') {
            name = action;
            service = this.detectService(action);
            params = {};
        }
        else {
            return null;
        }

        return { service, name, params, status: action.status || 'completed' };
    }

    detectService(actionName) {
        if (!actionName || typeof actionName !== 'string') {
            return 'unknown';
        }

        // Normalize action name
        const normalized = actionName.toLowerCase().trim();

        // FIRST: Check action registry from available_actions event (the source of truth)
        if (this.actionRegistry[normalized]) {
            return this.actionRegistry[normalized];
        }

        // FALLBACK: Use heuristics for actions not in registry (backwards compatibility)
        const serviceMap = {
            // Browser actions
            'browser_open_tab': 'browser',
            'browser_close_tab': 'browser',
            'browser_click': 'browser',
            'browser_type': 'browser',
            'browser_navigate': 'browser',
            'browser_go_to_url': 'browser',
            'browser_search_google': 'browser',
            'browser_screenshot': 'browser',
            'open_tab': 'browser',
            'close_tab': 'browser',
            'click': 'browser',
            'type_text': 'browser',
            'navigate': 'browser',
            'screenshot': 'browser',
            'go_to_url': 'browser',
            'search_google': 'browser',

            // Filesystem actions
            'filesystem_write': 'filesystem',
            'filesystem_read': 'filesystem',
            'filesystem_list': 'filesystem',
            'filesystem_delete': 'filesystem',
            'filesystem_create_dir': 'filesystem',
            'write_file': 'filesystem',
            'read_file': 'filesystem',
            'list_files': 'filesystem',
            'delete_file': 'filesystem',
            'create_directory': 'filesystem',
            'list_dir': 'filesystem',

            // Task tool actions
            'create_todo': 'task',
            'task_create_todo': 'task',
            'update_todo': 'task',
            'task_update_todo': 'task',
            'complete_todo': 'task',
            'task_complete_todo': 'task',

            // System/command actions
            'execute_command': 'system',
            'run_command': 'system',
            'bash': 'system',
            'done': 'system',
            'wait_for_response': 'system'
        };

        // Direct match
        if (serviceMap[normalized]) {
            return serviceMap[normalized];
        }

        // Check if action name contains a known service prefix
        if (normalized.includes('browser')) return 'browser';
        if (normalized.includes('filesystem') || normalized.includes('file')) return 'filesystem';
        if (normalized.includes('mcp')) return 'mcp';
        if (normalized.includes('task') || normalized.includes('todo')) return 'task';

        // Extract service from pattern: "service_action" -> "service"
        if (normalized.includes('_')) {
            const parts = normalized.split('_');
            const possibleService = parts[0];

            // Known services
            if (['browser', 'filesystem', 'system', 'mcp', 'task'].includes(possibleService)) {
                return possibleService;
            }

            // Check last part for service indicators
            if (parts.includes('file') || parts.includes('dir') || parts.includes('directory')) {
                return 'filesystem';
            }
            if (parts.includes('url') || parts.includes('tab') || parts.includes('click')) {
                return 'browser';
            }
        }

        // If no match found, use the first part before underscore or return the whole name
        if (normalized.includes('_')) {
            return normalized.split('_')[0];
        }

        return normalized;
    }
    
    /**
     * Update the thinking message display
     *
     * Re-renders the message with updated steps using hierarchical format
     * CRITICAL: Never creates new elements - only updates existing ones
     */
    updateThinkingDisplay() {
        if (!this.currentAgentMessage) {
            logger.debug('[Chat] updateThinkingDisplay: no currentAgentMessage');
            return;
        }

        const msgEl = document.querySelector(`[data-message-id="${this.currentAgentMessage.id}"]`);
        if (!msgEl) {
            // FIX: Don't create duplicates - element should already exist from addMessage()
            // If it doesn't exist, log error but don't create a new one
            logger.error('[Chat] ❌ updateThinkingDisplay: DOM element not found for', this.currentAgentMessage.id);
            return;
        }

        // Re-render using renderAgentMessage for consistent rendering (handles time formatting internally)
        msgEl.innerHTML = this.renderAgentMessage(this.currentAgentMessage);

        logger.debug('[Chat] 🔄 Updated thinking display - steps:', 
                     this.currentAgentMessage.steps?.length || 0,
                     'status:', this.currentAgentMessage.status);

        // Auto-scroll if not locked
        if (!this.scrollLocked) {
            this.scrollToBottom(true);
        }
    }

    onStreamChunk(data) {
        logger.debug('[Chat] Stream chunk received:', data);

        // Extract chunk text from data
        const chunkText = data.chunk || data.text || data.content || '';

        if (!chunkText) {
            logger.warn('[Chat] Stream chunk has no text content');
            return;
        }

        // Create thinking message if needed
        if (!this.currentAgentMessage) {
            logger.debug('[Chat] Creating new thinking message for streaming');
            const messageId = `msg_thinking_${Date.now()}`;
            this.currentAgentMessage = {
                id: messageId,
                type: 'agent',
                thinking: '',
                steps: [],
                status: 'thinking',
                timestamp: Date.now() / 1000
            };
            this.currentAgentMessageId = messageId;
            this.addMessage(this.currentAgentMessage);
        }

        // Append chunk to thinking text
        this.currentAgentMessage.thinking += chunkText;
        this.isStreaming = true;

        logger.debug('[Chat] Thinking message updated, length:', this.currentAgentMessage.thinking.length);

        // Update display - this now updates content directly without full re-render
        this.updateThinkingDisplay();
    }

    addMessage(message, options = {}) {
        // Filter out LLM request messages - they are internal API calls
        if (message.type === 'llm_request') {
            return; // Skip LLM requests only - show tool results
        }

        // Remove empty state if present - ALWAYS hide when messages exist
        if (this.emptyState) {
            this.emptyState.style.display = 'none';
        }

        // Hide config panel if present (only on first message)
        if (this.messages.length === 0) {
            const configPanel = document.getElementById('config-panel');
            if (configPanel) {
                configPanel.style.display = 'none';
            }
        }

        // Add to state
        this.messages.push(message);

        // Trim messages to prevent unbounded memory growth
        this._trimMessages();

        // Render
        this.renderMessage(message);

        // Scroll to bottom (unless disabled)
        if (!options.skipScroll && !this.scrollLocked) {
            this.scrollToBottom(true);
        }
    }

    updateCurrentAgentMessage(updates) {
        if (!this.currentAgentMessage) {
            return;
        }

        // Merge updates
        Object.assign(this.currentAgentMessage, updates);

        // Update DOM
        const msgEl = document.querySelector(`[data-message-id="${this.currentAgentMessage.id}"]`);
        if (msgEl) {
            // Re-render the message
            const newEl = this.createMessageElement(this.currentAgentMessage);
            msgEl.replaceWith(newEl);
        }

        // If completed, clear current (with delay to allow events to process)
        if (this.currentAgentMessage.status === 'completed') {
            setTimeout(() => this._resetAgentMessageState(), 500);
        }
    }

    renderMessage(message) {
        const messageEl = this.createMessageElement(message);

        // Find the correct position to insert based on sequence number (_seq)
        // Use _seq for ordering (server-guaranteed chronological order)
        // Fall back to timestamp * 1000 for messages without _seq
        const existingMessages = this.chatContainer.querySelectorAll('.chat-message');
        let insertBefore = null;

        const msgSeq = message.seq || (message.timestamp ? message.timestamp * 1000 : 0) || 0;

        for (const existing of existingMessages) {
            const existingSeq = parseFloat(existing.dataset.seq) || 0;
            // If we find a message with a later seq, insert before it
            if (existingSeq > msgSeq) {
                insertBefore = existing;
                break;
            }
        }

        if (insertBefore) {
            // Insert at correct chronological position
            this.chatContainer.insertBefore(messageEl, insertBefore);
        } else {
            // Append at end (newest)
            this.chatContainer.appendChild(messageEl);
        }

        // Update chunk grouping if this is a chunk message
        if (message.type === 'agent_chunk') {
            this._updateChunkGroupVisuals();
        }
    }

    createMessageElement(message) {
        const wrapper = document.createElement('div');
        wrapper.className = `chat-message ${message.type}-message`;
        wrapper.dataset.messageId = message.id;
        wrapper.dataset.timestamp = message.timestamp || 0;  // Legacy support
        // Use seq for ordering (server-guaranteed chronological order)
        wrapper.dataset.seq = message.seq || (message.timestamp ? message.timestamp * 1000 : 0) || 0;

        if (message.type === 'user') {
            wrapper.innerHTML = this.renderUserMessage(message);
        } else if (message.type === 'agent') {
            wrapper.innerHTML = this.renderAgentMessage(message);
        } else if (message.type === 'agent_chunk') {
            // Discrete timestamped chunk message
            wrapper.innerHTML = this.renderAgentChunk(message);
            if (message.groupId) {
                wrapper.dataset.groupId = message.groupId;
            }
        } else if (message.type === 'system') {
            wrapper.innerHTML = this.renderSystemMessage(message);
        } else if (message.type === 'llm_request') {
            wrapper.innerHTML = this.renderLLMRequest(message);
        } else if (message.type === 'tool_execution') {
            wrapper.innerHTML = this.renderToolExecution(message);
        } else if (message.type === 'error') {
            wrapper.innerHTML = this.renderErrorMessage(message);
        } else if (message.type === 'multi_agent_relationship') {
            wrapper.innerHTML = this.renderMultiAgentRelationship(message);
        }

        // Apply Prism syntax highlighting to code blocks (if Prism is loaded)
        if (window.Prism) {
            requestAnimationFrame(() => {
                Prism.highlightAllUnder(wrapper);
            });
        }

        return wrapper;
    }

    /**
     * Render a discrete agent chunk message
     * Chunks are individual timestamped events that display in chronological order
     */
    renderAgentChunk(message) {
        const time = this._formatTime(message.timestamp);
        const data = message.data || {};

        switch (message.chunkType) {
            case 'action':
                return this._renderActionChunk(time, data, message);
            case 'thinking':
                return this._renderThinkingChunk(time, data, message);
            case 'response':
                return this._renderResponseChunk(time, data, message);
            case 'error':
                return this._renderErrorChunk(time, data, message);
            case 'file':
                return this._renderFileChunk(time, data, message);
            case 'status':
                return this._renderStatusChunk(time, data, message);
            default:
                // Generic chunk display
                return this._renderGenericChunk(time, data, message);
        }
    }

    _renderActionChunk(time, data, message) {
        const actions = data.actions || [];
        if (actions.length === 0) {
            return this._renderGenericChunk(time, data, message);
        }

        let html = `${this._renderMessageHeader(time, 'agent')}`;
        html += '<div class="chunk-actions">';

        actions.forEach(action => {
            const parsedAction = this.parseAction(action);
            if (parsedAction) {
                html += this.renderStepAction(parsedAction);
            }
        });

        html += '</div>';
        return html;
    }

    _renderThinkingChunk(time, data, message) {
        const reasoning = data.reasoning || data.thought || data.task_progress ||
                         data.next_goal || data.current_task || data.plan || '';

        if (!reasoning) {
            // Empty thinking - show minimal indicator
            return `${this._renderMessageHeader(time, 'agent')} ${this._renderThinkingIndicator()}`;
        }

        return `${this._renderMessageHeader(time, 'agent')}<div class="chunk-thinking"><span class="step-thinking-line">${escapeHtml(reasoning)}</span></div>`;
    }

    _renderResponseChunk(time, data, message) {
        const text = data.text || data.message || data.response || '';
        if (!text) {
            return '';
        }

        return `${this._renderMessageHeader(time, 'agent')}<div class="chunk-response final-response"><span class="message-content">${escapeHtml(text)}</span></div>`;
    }

    _renderErrorChunk(time, data, message) {
        const errorMsg = data.error_message || data.error || data.message || 'Unknown error';
        return `${this._renderMessageHeader(time, 'agent')}<div class="chunk-error iteration-error">${escapeHtml(errorMsg)}</div>`;
    }

    _renderFileChunk(time, data, message) {
        let html = `${this._renderMessageHeader(time, 'agent')}<div class="chunk-files iteration-files">`;

        // Created files
        if (data.files_created?.length > 0) {
            data.files_created.forEach(filePath => {
                const fileName = filePath.split('/').pop();
                html += `<span class="file-chip file-created" title="${escapeHtml(filePath)}"><span class="file-chip-icon">+</span><span class="file-chip-name">${escapeHtml(fileName)}</span></span>`;
            });
        }

        // Modified files
        if (data.files_modified?.length > 0) {
            data.files_modified.forEach(filePath => {
                const fileName = filePath.split('/').pop();
                html += `<span class="file-chip file-modified" title="${escapeHtml(filePath)}"><span class="file-chip-icon">~</span><span class="file-chip-name">${escapeHtml(fileName)}</span></span>`;
            });
        }

        html += '</div>';
        return html;
    }

    _renderStatusChunk(time, data, message) {
        const status = data.status || 'unknown';
        const statusText = status.charAt(0).toUpperCase() + status.slice(1);
        return `${this._renderMessageHeader(time, 'agent')}<div class="chunk-status"><span class="status-indicator status-${status}">${escapeHtml(statusText)}</span></div>`;
    }

    _renderGenericChunk(time, data, message) {
        // For unrecognized chunk types, show a minimal representation
        const summary = data.reasoning || data.task_progress || data.status || '';
        if (summary) {
            return `${this._renderMessageHeader(time, 'agent')}<span class="chunk-generic">${escapeHtml(summary)}</span>`;
        }
        return `${this._renderMessageHeader(time, 'agent')}<span class="chunk-generic text-muted">(processing...)</span>`;
    }

    /**
     * Determine chunk type from event data
     */
    _determineChunkType(event) {
        const data = event.data || {};

        // Check for actions first
        if (data.actions && Array.isArray(data.actions) && data.actions.length > 0) {
            return 'action';
        }

        // Check for file operations
        if ((data.files_created?.length > 0) || (data.files_modified?.length > 0)) {
            return 'file';
        }

        // Check for errors
        if (data.error_message || data.error) {
            return 'error';
        }

        // Check for final response
        if (data.is_done || data.final_message || data.result) {
            return 'response';
        }

        // Check for status change
        if (data.status && ['completed', 'done', 'paused', 'cancelled'].includes(data.status)) {
            return 'status';
        }

        // Default to thinking
        return 'thinking';
    }

    /**
     * Update visual grouping for chunk messages
     */
    _updateChunkGroupVisuals() {
        const chunks = this.chatContainer.querySelectorAll('.agent_chunk-message');
        if (chunks.length === 0) return;

        let currentGroup = null;
        let groupStart = null;

        chunks.forEach((chunk, index) => {
            const groupId = chunk.dataset.groupId;

            // Remove old classes
            chunk.classList.remove('chunk-group-start', 'chunk-group-end', 'chunk-group-middle');

            if (groupId !== currentGroup) {
                // New group starting
                if (groupStart) {
                    // Mark end of previous group
                    groupStart.classList.add('chunk-group-start');
                    chunks[index - 1]?.classList.add('chunk-group-end');
                }
                currentGroup = groupId;
                groupStart = chunk;
            } else {
                // Continuing same group
                chunk.classList.add('chunk-group-middle');
            }
        });

        // Mark the last group
        if (groupStart) {
            groupStart.classList.add('chunk-group-start');
            chunks[chunks.length - 1]?.classList.add('chunk-group-end');
        }

        // Check for auto-collapse after visual update
        this._checkAutoCollapse();
    }

    /**
     * Auto-collapse completed groups with more than 5 chunks
     */
    _checkAutoCollapse() {
        // Group chunks by groupId
        const groups = new Map();
        this.messages.forEach(msg => {
            if (msg.type === 'agent_chunk' && msg.groupId) {
                if (!groups.has(msg.groupId)) {
                    groups.set(msg.groupId, []);
                }
                groups.get(msg.groupId).push(msg);
            }
        });

        // Check each group for auto-collapse
        const currentGroupId = `group_${this._currentIteration}`;
        groups.forEach((chunks, groupId) => {
            // Don't collapse the current active group
            if (groupId === currentGroupId) return;

            // Don't collapse if already collapsed
            const groupInfo = this._chunkGroups.get(groupId);
            if (groupInfo?.collapsed) return;

            // Auto-collapse if more than 5 chunks
            if (chunks.length > 5) {
                this._collapseGroup(groupId, chunks);
            }
        });
    }

    /**
     * Collapse a group of chunks into a summary
     */
    _collapseGroup(groupId, chunks) {
        if (!chunks || chunks.length === 0) return;

        // Find the DOM elements for this group
        const groupElements = this.chatContainer.querySelectorAll(`[data-group-id="${groupId}"]`);
        if (groupElements.length === 0) return;

        // Create summary element
        const firstChunk = chunks[0];
        const lastChunk = chunks[chunks.length - 1];
        const actionCount = chunks.filter(c => c.chunkType === 'action').length;
        const thinkingCount = chunks.filter(c => c.chunkType === 'thinking').length;

        const summaryHtml = `
            <div class="chunk-summary" data-group-id="${groupId}">
                <span class="chunk-summary-icon">▶</span>
                <span class="chunk-summary-text">
                    ${chunks.length} steps: ${actionCount} actions, ${thinkingCount} thoughts
                </span>
                <span class="chunk-summary-time">${this._formatTime(firstChunk.timestamp)} - ${this._formatTime(lastChunk.timestamp)}</span>
                <span class="chunk-summary-expand">(click to expand)</span>
            </div>
        `;

        // Create wrapper for collapsed content
        const wrapper = document.createElement('div');
        wrapper.className = 'chat-message chunk-group-collapsed';
        wrapper.dataset.groupId = groupId;
        wrapper.dataset.seq = firstChunk.seq || 0;
        wrapper.innerHTML = summaryHtml;

        // Add click handler to expand
        wrapper.addEventListener('click', () => this._expandGroup(groupId));

        // Hide individual chunks and insert summary
        const firstElement = groupElements[0];
        firstElement.parentNode.insertBefore(wrapper, firstElement);

        groupElements.forEach(el => {
            el.style.display = 'none';
            el.classList.add('chunk-collapsed-item');
        });

        // Track collapsed state
        this._chunkGroups.set(groupId, { chunks, collapsed: true });

        logger.debug(`[Chat] Collapsed group ${groupId} with ${chunks.length} chunks`);
    }

    /**
     * Expand a collapsed group
     */
    _expandGroup(groupId) {
        // Find summary element
        const summary = this.chatContainer.querySelector(`.chunk-group-collapsed[data-group-id="${groupId}"]`);
        if (!summary) return;

        // Show individual chunks
        const groupElements = this.chatContainer.querySelectorAll(`[data-group-id="${groupId}"].chunk-collapsed-item`);
        groupElements.forEach(el => {
            el.style.display = '';
            el.classList.remove('chunk-collapsed-item');
        });

        // Remove summary
        summary.remove();

        // Update tracking
        const groupInfo = this._chunkGroups.get(groupId);
        if (groupInfo) {
            groupInfo.collapsed = false;
        }

        // Re-apply visual grouping
        this._updateChunkGroupVisuals();

        logger.debug(`[Chat] Expanded group ${groupId}`);
    }

    /**
     * Reset iteration state for new task
     */
    _resetIterationState() {
        this._currentIteration = 0;
    }

    renderUserMessage(message) {
        const time = this._formatTime(message.timestamp);
        // IRC style: [time] <user> message
        return `${this._renderMessageHeader(time, 'user')}<span class="message-content">${escapeHtml(message.text)}</span>`;
    }

    renderAgentMessage(message) {
        const time = this._formatTime(message.timestamp);

        // PRIORITY 0: Use iterations if available (v3 format)
        if (message.iterations && message.iterations.length > 0) {
            let html = this.renderIterations(message, time);

            // Add final response below if completed
            if (message.status === 'completed' && message.text) {
                html += `\n<div class="final-response">${this._renderMessageHeader(time)}<span class="message-content">${escapeHtml(message.text)}</span></div>`;
            }

            return html;
        }

        // PRIORITY 1: If completed message with BOTH steps and final text (v2 format)
        // Show thinking process FIRST, then final response
        if (message.status === 'completed' && message.text && message.steps && message.steps.length > 0) {
            // Render steps (thinking + actions)
            let html = this.renderStepsHierarchical(message, time);

            // Add final response below the thinking process
            html += `\n<div class="final-response">${this._renderMessageHeader(time)}<span class="message-content">${escapeHtml(message.text)}</span></div>`;

            // Add file preview if detected
            const fileOp = this.detectFileOperation(message.text);
            if (fileOp) {
                html += this.renderFilePreview(fileOp);
            }

            return html;
        }

        // PRIORITY 2: If completed with text only (no steps)
        if (message.status === 'completed' && message.text) {
            // Detect file operations in the message
            const fileOp = this.detectFileOperation(message.text);

            // Build base message HTML
            let html = `${this._renderMessageHeader(time)}<span class="message-content">${escapeHtml(message.text)}</span>`;

            // Add file preview if detected
            if (fileOp) {
                html += this.renderFilePreview(fileOp);
            }

            return html;
        }

        // PRIORITY 3: If this message has STEPS (hierarchical structure - v2 format)
        if (message.steps && message.steps.length > 0) {
            return this.renderStepsHierarchical(message, time);
        }

        // FALLBACK: Simple thinking indicator or empty message
        if (message.status === 'thinking') {
            return `${this._renderMessageHeader(time)} ${this._renderThinkingIndicator(true)}`;
        }

        // Final fallback: render text if available
        const messageText = message.text || message.result || message.content;

        if (!messageText) {
            logger.warn('[Chat] Agent message has no text content:', message);
            return `${this._renderMessageHeader(time)}<span class="message-content text-muted">(no response)</span>`;
        }

        return `${this._renderMessageHeader(time)}<span class="message-content">${escapeHtml(messageText)}</span>`;
    }

    /**
     * Render iterations with status indicators and file chips (v3 format)
     */
    renderIterations(message, time) {
        let html = this._renderMessageHeader(time);

        // Show brain loader if still loading
        if (message.showBrainLoader) {
            html += ` ${this._renderThinkingIndicator(true)}`;
            return html;
        }

        html += '<div class="message-iterations">';

        message.iterations.forEach((iteration) => {
            html += `<div class="message-iteration">`;

            // Reasoning line (if present) - bullet point style like steps
            if (iteration.reasoning) {
                html += `<div class="iteration-reasoning">${escapeHtml(iteration.reasoning)}</div>`;
            }

            // Action lines (if present) - uses existing step-action-line rendering
            if (iteration.actions && iteration.actions.length > 0) {
                iteration.actions.forEach(action => {
                    html += this.renderStepAction(action);
                });
            }

            // Error message (if present)
            if (iteration.error) {
                html += `<div class="iteration-error">${escapeHtml(iteration.error)}</div>`;
            }

            // File chips - ONLY if there are files created/modified
            const hasFiles = (iteration.filesCreated?.length > 0) ||
                            (iteration.filesModified?.length > 0);

            if (hasFiles) {
                html += this.renderIterationFileChips(iteration);
            }

            html += '</div>';
        });

        // Thinking indicator at bottom if still active
        if (message.status === 'thinking') {
            html += `<div class="iteration-thinking">${this._renderThinkingIndicator()}</div>`;
        }

        html += '</div>';

        return html;
    }

    /**
     * Render file chips for an iteration - ONLY called when files exist
     */
    renderIterationFileChips(iteration) {
        let html = '<div class="iteration-files">';

        // Created files (green)
        if (iteration.filesCreated?.length > 0) {
            iteration.filesCreated.forEach(filePath => {
                const fileName = filePath.split('/').pop();
                html += `
                    <span class="file-chip file-created" data-path="${escapeHtml(filePath)}" title="${escapeHtml(filePath)}">
                        <span class="file-chip-icon">+</span>
                        <span class="file-chip-name">${escapeHtml(fileName)}</span>
                    </span>
                `;
            });
        }

        // Modified files (yellow)
        if (iteration.filesModified?.length > 0) {
            iteration.filesModified.forEach(filePath => {
                const fileName = filePath.split('/').pop();
                html += `
                    <span class="file-chip file-modified" data-path="${escapeHtml(filePath)}" title="${escapeHtml(filePath)}">
                        <span class="file-chip-icon">~</span>
                        <span class="file-chip-name">${escapeHtml(fileName)}</span>
                    </span>
                `;
            });
        }

        // Read files (blue) - optional display
        if (iteration.filesRead?.length > 0) {
            iteration.filesRead.forEach(filePath => {
                const fileName = filePath.split('/').pop();
                html += `
                    <span class="file-chip file-read" data-path="${escapeHtml(filePath)}" title="${escapeHtml(filePath)}">
                        <span class="file-chip-icon">→</span>
                        <span class="file-chip-name">${escapeHtml(fileName)}</span>
                    </span>
                `;
            });
        }

        html += '</div>';
        return html;
    }

    /**
     * Render IRC-style steps display - clean and minimal (v2 format - backwards compat)
     */
    renderStepsHierarchical(message, time) {
        let html = this._renderMessageHeader(time);

        // Show brain loader if still loading
        if (message.showBrainLoader) {
            html += ` ${this._renderThinkingIndicator(true)}`;
            return html;
        }

        // Render steps in IRC style
        html += '<div class="message-steps">';

        message.steps.forEach((step) => {
            html += '<div class="message-step">';

            // Thinking line (if present) - bullet added via CSS ::before
            if (step.thinking) {
                html += `<div class="step-thinking-line">${escapeHtml(step.thinking)}</div>`;
            }

            // Action lines (if present)
            if (step.actions && step.actions.length > 0) {
                step.actions.forEach(action => {
                    html += this.renderStepAction(action);
                });
            }

            html += '</div>';
        });

        // Add simple thinking dots at bottom if still thinking
        if (message.status === 'thinking') {
            html += `<div class="step-thinking-dots">${this._renderThinkingIndicator()}</div>`;
        }

        html += '</div>';

        return html;
    }

    /**
     * Format a parameter value for display (handles objects, arrays, etc.)
     */
    formatParamValue(value) {
        if (value === null) return 'null';
        if (value === undefined) return 'undefined';

        // If it's an object or array, stringify it
        if (typeof value === 'object') {
            try {
                return JSON.stringify(value);
            } catch (e) {
                return String(value);
            }
        }

        return String(value);
    }

    /**
     * Render single action line - IRC style with arrow and expandable params
     */
    renderStepAction(action) {
        const actionId = `action_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`;
        const service = action.service || '';
        const name = action.name || 'unknown';

        // Build truncated param preview (show 1-2 key params)
        let paramPreview = '';
        let hasParams = false;
        const paramEntries = [];

        if (action.params && Object.keys(action.params).length > 0) {
            hasParams = true;

            // Priority order for which params to show in preview
            const priorityKeys = ['url', 'file_path', 'path', 'query', 'text', 'content', 'selector', 'command'];

            // Find first 2 relevant params for preview
            for (const key of priorityKeys) {
                if (action.params[key] !== undefined) {
                    let value = this.formatParamValue(action.params[key]);
                    // Truncate long values
                    if (value.length > 50) {
                        value = value.substring(0, 50) + '...';
                    }
                    paramEntries.push(`${key}: ${value}`);
                    if (paramEntries.length >= 2) break;
                }
            }

            // If we didn't find priority params, show first 2 params
            if (paramEntries.length === 0) {
                const entries = Object.entries(action.params).slice(0, 2);
                entries.forEach(([key, value]) => {
                    let val = this.formatParamValue(value);
                    if (val.length > 40) {
                        val = val.substring(0, 40) + '...';
                    }
                    paramEntries.push(`${key}: ${val}`);
                });
            }

            if (paramEntries.length > 0) {
                paramPreview = ` <span class="action-params">(${escapeHtml(paramEntries.join(', '))})</span>`;
            }
        }

        // Build the action line with service name if available
        let html = `<div class="step-action-line" data-action-id="${actionId}">`;
        if (service) {
            html += `<span class="action-service">${escapeHtml(service)}</span>`;
            html += `<span class="action-separator">→</span>`;
        }
        html += `<span class="action-name">${escapeHtml(name)}</span>`;
        html += paramPreview;
        html += `</div>`;

        // Build expandable details section
        if (hasParams) {
            html += `<div class="action-details" id="${actionId}">`;
            html += `<div class="action-details-header">Parameters:</div>`;

            // Show ALL params in detail view
            Object.entries(action.params).forEach(([key, value]) => {
                let displayValue = this.formatParamValue(value);
                // Format long values nicely
                if (displayValue.length > 200) {
                    displayValue = displayValue.substring(0, 200) + '... (truncated)';
                }
                html += `<div class="action-param-line">`;
                html += `<span class="action-param-key">${escapeHtml(key)}:</span> `;
                html += `<span class="action-param-value">${escapeHtml(displayValue)}</span>`;
                html += `</div>`;
            });

            html += `</div>`;
        }

        return html;
    }


    renderSystemMessage(message) {
        const time = this._formatTime(message.timestamp);
        // IRC style system message: [time] * message
        // Escape HTML first, then linkify URLs
        let text = escapeHtml(message.text);
        // Convert t.me/xxx and https:// URLs to clickable links
        text = text.replace(/(https?:\/\/[^\s<]+|t\.me\/[^\s<]+)/g, '<a href="https://$1" target="_blank" rel="noopener noreferrer">$1</a>');
        // Fix double https:// for URLs that already had it
        text = text.replace(/href="https:\/\/https:\/\//g, 'href="https://');
        return `<span class="message-time">[${time}]</span> <span class="message-author">*</span> <span class="system-message-content">${text}</span>`;
    }

    renderLLMRequest(message) {
        // Hide LLM requests - they clutter the chat flow
        // Users care about results, not internal API calls
        return '';
    }

    renderToolExecution(message) {
        // FIX (Dec 31, 2025): Actually display tool execution results for user visibility
        const time = this._formatTime(message.timestamp);
        const data = message.data || {};
        const toolName = data.tool_name || 'unknown';
        const actionName = data.action_name || 'unknown';
        const success = data.success !== false;
        const duration = (data.duration_seconds || 0).toFixed(2);
        const preview = data.result_preview || '';
        const error = data.error || '';
        const resultSize = data.result_size || 0;

        const statusIcon = success ? '✅' : '❌';
        const statusClass = success ? 'tool-success' : 'tool-error';

        // Build result content
        let resultHtml = '';
        if (error) {
            resultHtml = `<div class="tool-error-msg">${escapeHtml(error)}</div>`;
        } else if (preview) {
            // Show preview with size indicator if truncated
            const truncatedNote = resultSize > preview.length ? ` <span class="tool-truncated">(showing ${preview.length} of ${resultSize.toLocaleString()} chars)</span>` : '';
            resultHtml = `<div class="tool-result-preview">${escapeHtml(preview)}${truncatedNote}</div>`;
        }

        return `
            <div class="tool-execution-inline ${statusClass}">
                <span class="message-time">[${time}]</span>
                <span class="tool-indicator">${statusIcon}</span>
                <span class="tool-badge">${escapeHtml(toolName)}</span>
                <span class="tool-separator">→</span>
                <span class="tool-action">${escapeHtml(actionName)}</span>
                <span class="tool-duration">(${duration}s)</span>
                ${resultHtml}
            </div>
        `;
    }

    renderErrorMessage(message) {
        const time = this._formatTime(message.timestamp);
        const severityIcon = message.recoverable ? '⚠️' : '❌';
        const severityClass = message.recoverable ? 'warning' : 'critical';

        let html = `
            <span class="message-time">[${time}]</span>
            <span class="message-author error-message">${severityIcon}</span>
            <span class="error-content ${severityClass}">
                <span class="error-type">${escapeHtml(message.errorType)}:</span>
                <span class="error-text">${escapeHtml(message.errorMessage)}</span>
            </span>
        `;

        // Add stack trace if available (collapsed by default)
        if (message.errorStack) {
            html += `
                <div class="error-stack-container">
                    <button class="error-stack-toggle" onclick="this.nextElementSibling.classList.toggle('expanded')">
                        Show stack trace
                    </button>
                    <pre class="error-stack">${escapeHtml(message.errorStack)}</pre>
                </div>
            `;
        }

        return html;
    }

    /**
     * Detect if message contains file operation and extract details
     */
    detectFileOperation(messageText) {
        if (!messageText) return null;

        // Pattern 1: "Created 'filename' with the content 'inline content'" (inline format)
        const inlineMatch = messageText.match(/(?:Successfully created|Created file|Wrote|Created)\s+([^\s]+)\s+with\s+the\s+content\s+['"]([^'"]+)['"]/i);
        if (inlineMatch) {
            const filepath = inlineMatch[1];
            const content = inlineMatch[2];
            return {
                operation: 'create',
                path: filepath,
                content: content,
                language: this.detectLanguage(filepath)
            };
        }

        // Pattern 2: "Created 'filename' with the following content:\n{content}" (multiline format)
        const createMatch = messageText.match(/(?:Successfully created|Created file|Wrote|Created)\s+['"']?([^\s'"]+)['"']?\s+(?:with|containing)/i);
        if (createMatch) {
            const filepath = createMatch[1];

            // Try to extract content after pattern
            // Look for content after "content:" or "following:" followed by newline and content
            const contentMatch = messageText.match(/(?:content|following):\s*\n([\s\S]+?)(?:\n\n|$)/i);
            if (contentMatch) {
                return {
                    operation: 'create',
                    path: filepath,
                    content: contentMatch[1].trim(),
                    language: this.detectLanguage(filepath)
                };
            }
        }

        // Pattern 3: "Modified 'filename'" or "Updated 'filename'"
        const editMatch = messageText.match(/(?:Modified|Updated|Edited)\s+['"']([^'"']+)['"']/i);
        if (editMatch) {
            return {
                operation: 'edit',
                path: editMatch[1],
                language: this.detectLanguage(editMatch[1])
            };
        }

        return null;
    }

    /**
     * Detect programming language from filename
     */
    detectLanguage(filename) {
        if (!filename) return 'text';
        const ext = filename.split('.').pop().toLowerCase();
        const langMap = {
            'js': 'javascript',
            'ts': 'typescript',
            'py': 'python',
            'jsx': 'javascript',
            'tsx': 'typescript',
            'json': 'json',
            'html': 'html',
            'css': 'css',
            'md': 'markdown',
            'sh': 'bash',
            'yaml': 'yaml',
            'yml': 'yaml',
            'xml': 'xml',
            'sql': 'sql',
            'txt': 'text',
            'rb': 'ruby',
            'go': 'go',
            'rs': 'rust',
            'java': 'java',
            'cpp': 'cpp',
            'c': 'c',
            'php': 'php'
        };
        return langMap[ext] || 'text';
    }

    /**
     * Render file preview with syntax highlighting
     */
    renderFilePreview(fileOp) {
        const { operation, path, content, language } = fileOp;

        // Icon based on operation
        const icon = operation === 'create' ? '📄' : '✏️';

        return `
            <div class="file-preview">
                <div class="file-preview-header">
                    <span class="file-preview-icon">${icon}</span>
                    <span class="file-preview-path">${escapeHtml(path)}</span>
                </div>
                ${content ? `
                    <pre class="file-preview-content"><code class="language-${language}">${escapeHtml(content)}</code></pre>
                ` : ''}
            </div>
        `;
    }

    renderMultiAgentRelationship(message) {
        const time = this._formatTime(message.timestamp);
        const agentList = message.agents
            .sort((a, b) => a.sequence_position - b.sequence_position)
            .map(agent => `${agent.name} (${agent.model})`)
            .join(' → ');

        return `
            <span class="message-time">[${time}]</span>
            <span class="message-author system">*</span>
            <span class="system-message-content">
                <span class="relationship-icon">🤖</span>
                Multi-agent session initialized:
                <span class="relationship-agents">${escapeHtml(agentList)}</span>
            </span>
        `;
    }

    async sendMessage() {
        // Rate limiting - prevent rapid message sending
        const canSend = this.messageRateLimiter.call(() => {}, false);
        if (!canSend) {
            logger.debug('[Chat] Rate limited, message sending delayed');
            // Don't show error, just silently queue via rate limiter
        }
        
        // SECURITY: Check if user is owner before allowing message send
        const isOwner = sessionState.isOwner;
        const isAuthenticated = sessionState.isAuthenticated;
        
        if (!isOwner && !this.isNewSession) {
            logger.warn('[Chat] Attempted to send message without ownership');
            const message = isAuthenticated 
                ? "You cannot send messages to this session (you are not the owner)."
                : "Please sign in to interact with this session.";
            this.showError(message);
            return;
        }

        const text = this.inputField.value.trim();

        if (!text) {
            return;
        }

        // Disable input
        this.inputField.disabled = true;
        this.sendButton.disabled = true;

        try {
            // If this is a new session, create it first
            if (this.isNewSession) {
                await this.createSessionAndSendMessage(text);
            } else {
                // Normal message send for existing session
                await this.sendMessageToExistingSession(text);
            }
        } catch (error) {
            logger.error('[Chat] Send error:', error);

            // Reset button text if it's stuck on "Creating session..."
            if (this.sendButton.textContent !== 'Send') {
                this.sendButton.textContent = 'Send';
            }
            
            // Clean up any pending thinking message on error
            this.cleanupThinkingState();

            // Show error with context based on status code
            if (error.status === 403) {
                this.showError(error.message, 'beta-access');
            } else if (error.status === 429) {
                this.showError(error.message || 'Rate limit exceeded. Please try again in a moment.', 'rate-limit');
            } else if (error.status === 402 || error.message?.includes('quota') || error.message?.includes('credits')) {
                this.showError(error.message || 'Insufficient credits. Please deposit funds.', 'quota');
            } else {
                this.showError(error.message, 'error');
            }
        } finally {
            // Re-enable input (only if owner - don't re-enable for non-owners)
            if (isOwner || this.isNewSession) {
                this.inputField.disabled = false;
                this.sendButton.disabled = false;
                this.inputField.focus();
            }
        }
    }

    async createSessionAndSendMessage(message) {
        logger.debug('[Chat] Creating new session with message:', message);

        // Show loading state
        this.sendButton.textContent = 'Creating session...';

        // Get auth token if exists
        const token = localStorage.getItem('auth_token');
        const headers = { 'Content-Type': 'application/json' };
        if (token) {
            headers['Authorization'] = `Bearer ${token}`;
        }

        // Check if we have pending files that need to be uploaded
        const hasPendingFiles = this.fileManager && this.fileManager.hasPendingFiles();
        logger.debug('[Chat] Has pending files:', hasPendingFiles);

        // Get configuration from config panel if available
        let sessionConfig = {
            task: message,  // Store task for display
            model: 'claude-sonnet-4.5',  // Use Claude by default (Anthropic works reliably)
            tools: ['browser', 'filesystem'],
            max_steps: 50,
            temperature: 0.0,
            use_vision: true,
            auto_start: !hasPendingFiles,  // NEW: Don't auto-start if files pending
            wait_for_uploads: hasPendingFiles  // Signal backend to wait
        };

        // If config panel exists, use its configuration
        if (window.configPanel) {
            try {
                const config = window.configPanel.getConfiguration();
                if (config) {
                    sessionConfig = {
                        task: message,
                        ...config,
                        // CRITICAL FIX: Preserve auto_start logic based on pending files
                        // Don't let config panel override this - files MUST upload first!
                        auto_start: hasPendingFiles ? false : (config.auto_start !== undefined ? config.auto_start : true),
                        // Also set wait_for_uploads to signal backend
                        wait_for_uploads: hasPendingFiles
                    };
                    logger.debug('[Chat] Using config from panel:', sessionConfig);
                    if (hasPendingFiles) {
                        logger.debug('[Chat] 🔒 FORCING auto_start=false due to pending files');
                    }
                }
            } catch (error) {
                logger.warn('[Chat] Failed to get configuration from panel, using defaults:', error);
                // Fallback to defaults is already set in sessionConfig init
                this.showError(`Configuration error: ${error.message}. Using default settings.`);
            }
        }

        // NO optimistic add - message will appear from session_start event in feed
        // This prevents duplicates (optimistic + feed echo)
        // Use pending queue for visual feedback instead
        this._addToPendingQueue(message);

        // Show file attachment indicator if files are pending
        if (this.fileManager && this.fileManager.hasPendingFiles()) {
            const fileCount = this.fileManager.pendingFiles.size;
            const fileList = Array.from(this.fileManager.pendingFiles.values()).map(f => f.file.name).join(', ');
            this.addMessage(this._createMessage('system', `📎 Attaching ${fileCount} file(s): ${fileList}`));
        }

        // Clear input immediately
        this.inputField.value = '';
        this.inputField.style.height = '28px'; // Reset height

        // Create session with configuration (thinking message created after session exists)
        const response = await fetch('/api/task/sessions', {
            method: 'POST',
            headers: headers,
            credentials: 'include',
            body: JSON.stringify(sessionConfig)
        });

        if (!response.ok) {
            // Parse error response - handle multiple formats
            let errorMessage = 'Failed to create session';
            let errorDetail = null;

            try {
                const errorData = await response.json();
                // FastAPI returns {detail: "message"} or {detail: {error: "...", message: "..."}}
                if (typeof errorData.detail === 'string') {
                    errorMessage = errorData.detail;
                } else if (errorData.detail && errorData.detail.message) {
                    errorMessage = errorData.detail.message;
                    errorDetail = errorData.detail;
                } else if (errorData.message) {
                    errorMessage = errorData.message;
                }
            } catch (e) {
                // JSON parse failed - use status text
                errorMessage = response.statusText || 'Failed to create session';
            }

            // Create error with additional context
            const error = new Error(errorMessage);
            error.status = response.status;
            error.detail = errorDetail;
            throw error;
        }

        const data = await response.json();
        const newSessionId = data.session_id;

        logger.debug('[Chat] Session created:', newSessionId);

        // Upload files if any pending
        let uploadedFiles = [];
        if (this.fileManager && this.fileManager.hasPendingFiles()) {
            logger.debug('[Chat] Uploading pending files to new session workspace...');
            try {
                uploadedFiles = await this.fileManager.uploadAllFiles(newSessionId);
                logger.debug('[Chat] Files uploaded to workspace:', uploadedFiles);

                // Clear file manager
                this.fileManager.clear();

                // NOTE: File upload notification will come from backend feed event
                // with proper timestamp ordering. No need to add it here.
            } catch (error) {
                logger.error('[Chat] File upload failed:', error);
                this.addMessage(this._createMessage('system', `❌ File upload failed: ${error.message}`));
                uploadedFiles = [];  // Reset on error
            }
        }

        // CRITICAL FIX: Send initial task as first message with attached files
        // This allows agent to start with files already uploaded (and triggers start if auto_start=false)
        if (uploadedFiles.length > 0) {
            logger.debug('[Chat] Sending initial task as message with attached files to trigger start...');

            // Wait a moment for session to be fully initialized
            await new Promise(resolve => setTimeout(resolve, 500));

            const msgResponse = await fetch(`/api/session/${newSessionId}/messages`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    text: message,
                    kind: 'guidance',
                    metadata: {},
                    attached_files: uploadedFiles
                })
            });

            if (!msgResponse.ok) {
                const errorText = await msgResponse.text();
                logger.error('[Chat] Failed to send initial message with files:', errorText);
                throw new Error(`Failed to send message with files: ${msgResponse.status}`);
            } else {
                logger.debug('[Chat] ✓ Initial task sent with attached files - session starting now');
                // Show thinking message after successful message send
                this.createImmediateThinkingMessage();
            }
            // With files: user_message event will come from backend, skipHistory OK
            await this.transitionToActiveSession(newSessionId, { skipHistory: true });
        } else {
            // No files - session will auto-start, show thinking message
            logger.debug('[Chat] No files attached - session will auto-start');
            this.createImmediateThinkingMessage();
            // Without files: need to load history to get initial task from /task API
            await this.transitionToActiveSession(newSessionId, { skipHistory: false });
        }

        // Clear pending queue - session is now active and task is displayed
        // (either from /task API for no-files, or will come via user_message event for files)
        this.pendingMessages = [];
        this._updateQueueIndicator(false);

        // Reset button
        this.sendButton.textContent = 'Send';
    }

    async transitionToActiveSession(sessionId, options = {}) {
        logger.debug('[Chat] Transitioning to active session:', sessionId, 'options:', options);

        // 1. Use centralized session state - this handles URL update and DOM attributes
        sessionState.transitionToActive(sessionId);

        // 2. Update internal state (for backward compatibility)
        this.sessionId = sessionId;
        this.isNewSession = false;

        // 4. Hide config panel if it exists
        if (window.configPanel) {
            window.configPanel.hide();
        }

        // 5. Switch from empty to active state in UI
        this.activateEmptyStateElements();

        // 5. Trigger WebSocket initialization
        // Wait a moment for DOM attributes to update
        setTimeout(() => {
            if (window.triggerSocketInitialization) {
                logger.debug('[Chat] Triggering socket initialization');
                window.triggerSocketInitialization();
            } else {
                logger.warn('[Chat] triggerSocketInitialization not available');
            }
        }, 100);

        // 6. Update sidebar with session ID
        const sessionIdDisplay = document.getElementById('session-id');
        if (sessionIdDisplay) {
            sessionIdDisplay.textContent = sessionId.substring(0, 8) + '...';
        }

        const sessionStatus = document.getElementById('session-status');
        if (sessionStatus) {
            sessionStatus.textContent = 'Initializing';
        }

        // 7. Wait for socket to be ready and setup chat listeners
        if (window.socketReady) {
            try {
                logger.debug('[Chat] Waiting for socket to be ready...');
                const socket = await window.socketReady;
                this.socket = socket;
                this.setupSocketListeners();
                logger.debug('[Chat] Chat socket listeners setup complete');
            } catch (err) {
                logger.error('[Chat] Failed to setup socket listeners:', err);
            }
        } else {
            logger.warn('[Chat] window.socketReady not available');
        }

        // 8. Load chat history (skip if coming from new session creation - we already have the message)
        if (!options.skipHistory) {
            logger.debug('[Chat] Loading chat history...');
            await this.loadHistory();
        } else {
            logger.debug('[Chat] Skipping history load (messages already added optimistically)');
        }

        logger.debug('[Chat] Transition complete');
    }

    activateEmptyStateElements() {
        logger.debug('[Chat] Activating empty state elements');

        // Hide all "when-empty" elements
        document.querySelectorAll('.when-empty').forEach(el => {
            el.style.display = 'none';
        });

        // Show all "when-active" elements
        document.querySelectorAll('.when-active').forEach(el => {
            el.style.display = '';
        });
    }

    async sendMessageToExistingSession(text) {
        // Upload files if any pending
        let attachedFiles = [];
        if (this.fileManager && this.fileManager.hasPendingFiles()) {
            logger.debug('[Chat] Uploading pending files before sending message...');
            try {
                attachedFiles = await this.fileManager.uploadAllFiles(this.sessionId);
                logger.debug('[Chat] Files uploaded successfully:', attachedFiles);

                // NOTE: File upload notification will come from backend feed event
                // with proper timestamp ordering. No need to add it here.
            } catch (error) {
                logger.error('[Chat] File upload failed:', error);
                this.addMessage(this._createMessage('system', `❌ File upload failed: ${error.message}`));
                this.showError('Failed to upload files. Please try again.');
                throw error; // Stop message send if files fail to upload
            }
        }

        // NO optimistic update - message will appear from feed event
        // This prevents duplicates (optimistic + feed echo)

        // Clear input immediately for responsiveness
        this.inputField.value = '';
        this.inputField.style.height = '28px'; // Reset height

        // Clear attached files (they're now uploaded)
        if (this.fileManager) {
            this.fileManager.clear();
        }

        // Add to pending queue display (will be removed when feed event arrives)
        this._addToPendingQueue(text);

        // Send to API
        const response = await fetch(`/api/session/${this.sessionId}/messages`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                text: text,
                kind: 'comment',
                metadata: {},
                attached_files: attachedFiles.length > 0 ? attachedFiles : undefined
            })
        });

        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }

        logger.debug('[Chat] Message sent successfully', attachedFiles.length > 0 ? `with ${attachedFiles.length} file(s)` : '');
    }

    updateStreamingContent(chunk) {
        // Find the current agent message element
        if (!this.currentAgentMessage) {
            return;
        }

        const msgEl = document.querySelector(`[data-message-id="${this.currentAgentMessage.id}"]`);
        if (!msgEl) {
            return;
        }

        const thinkingContent = msgEl.querySelector('.thinking-content');
        if (thinkingContent) {
            thinkingContent.textContent += chunk;

            // Auto-scroll if at bottom
            if (!this.scrollLocked) {
                this.scrollToBottom(true);
            }
        }
    }

    scrollToBottom(smooth = false) {
        this.chatContainer.scrollTo({
            top: this.chatContainer.scrollHeight,
            behavior: smooth ? 'smooth' : 'auto'
        });

        // Hide scroll button
        if (this.scrollButton) {
            this.scrollButton.style.display = 'none';
        }
    }

    onScroll() {
        // Check if user has scrolled up
        const isAtBottom = (
            this.chatContainer.scrollHeight - this.chatContainer.scrollTop
        ) <= (this.chatContainer.clientHeight + 50); // 50px threshold

        this.scrollLocked = !isAtBottom;

        // Show/hide scroll button
        if (this.scrollButton) {
            this.scrollButton.style.display = isAtBottom ? 'none' : 'block';
        }
    }

    showLoadingState(message) {
        // Hide empty state
        if (this.emptyState) {
            this.emptyState.style.display = 'none';
        }

        // Show loading message
        this.addMessage(this._createMessage('system', message, { id: 'msg_loading' }));
    }

    hideLoadingState() {
        // Remove loading message if it exists
        const loadingMsg = document.querySelector('[data-message-id="msg_loading"]');
        if (loadingMsg) {
            loadingMsg.remove();
        }
    }

    showError(message, errorType = 'error') {
        // Remove existing emoji from message to avoid duplicates
        const cleanMessage = message.replace(/^[⚠️❌🔒💰⏱️\s]+/, '');

        // Choose emoji and add helpful context based on error type
        let emoji = '⚠️';
        let helpText = '';

        switch (errorType) {
            case 'beta-access':
                emoji = '🔒';
                helpText = '\n\n💡 Beta access open for DEN holders. Get full instructions at t.me/tmachinrobot';
                break;
            case 'quota':
                emoji = '💰';
                helpText = '\n\n💡 Tip: Check your Profile to view balance and deposit address';
                break;
            case 'rate-limit':
                emoji = '⏱️';
                break;
            case 'error':
            default:
                emoji = '❌';
                break;
        }

        this.addMessage(this._createMessage('system', `${emoji} ${cleanMessage}${helpText}`));
    }

    /**
     * Clean up all event listeners and resources.
     * Call this before destroying the ChatManager instance.
     */
    destroy() {
        if (this._destroyed) {
            logger.debug('[Chat] Already destroyed, skipping');
            return;
        }

        logger.debug('[Chat] Destroying ChatManager, cleaning up resources...');

        // Remove DOM event listeners
        if (this.sendButton && this._boundHandlers.sendClick) {
            this.sendButton.removeEventListener('click', this._boundHandlers.sendClick);
        }
        if (this.inputField) {
            if (this._boundHandlers.inputKeydown) {
                this.inputField.removeEventListener('keydown', this._boundHandlers.inputKeydown);
            }
            if (this._boundHandlers.inputPaste) {
                this.inputField.removeEventListener('paste', this._boundHandlers.inputPaste);
            }
            if (this._boundHandlers.inputChange) {
                this.inputField.removeEventListener('input', this._boundHandlers.inputChange);
            }
        }
        if (this.chatContainer) {
            if (this._boundHandlers.scrollThrottled) {
                this.chatContainer.removeEventListener('scroll', this._boundHandlers.scrollThrottled);
            }
            if (this._boundHandlers.containerClick) {
                this.chatContainer.removeEventListener('click', this._boundHandlers.containerClick);
            }
        }
        if (this.scrollButton && this._boundHandlers.scrollButtonClick) {
            this.scrollButton.removeEventListener('click', this._boundHandlers.scrollButtonClick);
        }
        if (this._boundHandlers.documentKeydown) {
            document.removeEventListener('keydown', this._boundHandlers.documentKeydown);
        }
        if (this._boundHandlers.viewportResize && window.visualViewport) {
            window.visualViewport.removeEventListener('resize', this._boundHandlers.viewportResize);
        }

        // Remove socket event listeners
        if (this.socket) {
            this.socket.off('stream_chunk');
            this.socket.off('streaming_output');
            this.socket.off('stream_update');
            this.socket.off('feed_update');
        }

        // Unsubscribe from EventStore (CRITICAL for cleanup)
        if (this._eventStoreUnsubscribe) {
            this._eventStoreUnsubscribe();
            this._eventStoreUnsubscribe = null;
        }

        // Clear event bus listeners (with proper handler references)
        if (this._boundHandlers.tabActivated) {
            off('tab:activated', this._boundHandlers.tabActivated);
        }

        // Clear state
        this.messages = [];
        this.currentAgentMessage = null;
        this._processedEventIds.clear();
        this._boundHandlers = {};
        this._destroyed = true;

        logger.debug('[Chat] ChatManager destroyed');
    }

    /**
     * Trim messages array to prevent unbounded memory growth.
     * Called after adding messages.
     */
    _trimMessages() {
        if (this.messages.length > ChatManager.MAX_MESSAGES) {
            const excess = this.messages.length - ChatManager.MAX_MESSAGES;
            this.messages = this.messages.slice(excess);
            logger.debug(`[Chat] Trimmed ${excess} old messages, now ${this.messages.length}`);
        }
    }
}

// EventMapper class removed - now handling events directly in ChatManager

// Initialize chat manager when DOM is ready
let chatManager = null;

async function initializeChatManager() {
    // Use centralized session state
    const sessionId = sessionState.sessionId || 'new';

    logger.debug('[Chat] Initializing - sessionId:', sessionId);

    // Create manager immediately, it will handle socket connection internally
    chatManager = new ChatManager(sessionId);
    logger.debug('[Chat] Manager initialized');
}

// Wait for DOM to be ready
logger.debug('[Chat] Module loading, readyState:', document.readyState);

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initializeChatManager);
} else {
    initializeChatManager();
}

logger.debug('[Chat] Module loaded');

export { chatManager };
