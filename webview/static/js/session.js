import { on, emit, setText, fetchJSON, escapeHtml, formatCost, sessionState, logger, ExponentialBackoff } from '/static/js/ui-utils.js?v=5';
import { updateSessionInfo } from '/static/js/sidebar-data.js?v=5';
import { loadStats } from '/static/js/stats.js?v=5';
import { loadScreenshot } from '/static/js/screenshot.js?v=5';
import { eventStore } from '/static/js/event-store.js?v=5';
import { feedFilter } from '/static/js/event-filter.js?v=5';

// Initialize session state from DOM
sessionState.init();

// Create socketReady promise for backwards compatibility with chat.js
window.socketReadyResolve = null;
window.socketReady = new Promise((resolve) => {
    window.socketReadyResolve = resolve;
});
logger.debug('[Session] socketReady promise created');

// Listen for tab activation events
on('tab:activated', (event) => {
    const tabId = event.detail.tabId;

    // If the feed tab is activated, scroll to the top of the feed (newest first)
    if (tabId === 'feed-tab') {
        const feedContainer = document.getElementById('feed-container');
        if (feedContainer) {
            feedContainer.scrollTop = 0;
        }
    }
});

// Socket initialization state - using mutex pattern to prevent race conditions
let socketInitialized = false;
let socketInitializing = false;
let socketInitLock = Promise.resolve(); // Mutex lock for initialization

// Create a function to initialize the socket so we can call it when ready
// Uses mutex pattern to prevent race conditions
async function initializeSocket(io) {
    // Acquire lock - wait for any pending initialization
    const previousLock = socketInitLock;
    let releaseLock;
    socketInitLock = new Promise(resolve => { releaseLock = resolve; });
    await previousLock;

    try {
        // Guard against multiple initializations (double-check after acquiring lock)
        if (socketInitialized) {
            logger.debug('[Session] Socket already initialized');
            return;
        }

        if (socketInitializing) {
            logger.debug('[Session] Socket initialization already in progress');
            return;
        }

        // Check if io is properly initialized
        if (!io) {
            logger.error('[Session] Socket.IO not loaded correctly');
            const refreshText = document.getElementById('refresh-text');
            if (refreshText) refreshText.textContent = 'Connection failed - Socket.IO not loaded';
            return;
        }

        // Use centralized session state
        const currentSessionId = sessionState.sessionId;
        const isNew = sessionState.isNew;

        // Don't initialize socket for new sessions (empty state)
        if (!currentSessionId || isNew) {
            logger.debug('[Session] Skipping socket initialization - no active session yet');
            return;
        }

        // Mark as initializing
        socketInitializing = true;

        // Store sessionId in closure for use in handlers
        const sessionId = currentSessionId;
        
    // Use the centralized WS_BASE from constants.js
    const socket = io(window.WS_BASE, {
        path: '/socket.io',
        transports: ['polling', 'websocket'],  // Use polling first, then upgrade to WebSocket
        reconnection: true,
        reconnectionAttempts: 10,
        reconnectionDelay: 1000,
        reconnectionDelayMax: 30000,
    });

    // Expose socket globally for chat.js to access
    window.socket = socket;
    logger.debug('[Session] Socket created and exposed as window.socket');

    // Connection state for UI feedback
    let connectionBackoff = new ExponentialBackoff({ maxRetries: 10 });

    // FeedRenderer - subscribes to EventStore and renders to DOM
    // Defer creation until DOM is ready
    let feedRenderer = null;
    function ensureFeedRenderer() {
        if (!feedRenderer) {
            feedRenderer = createFeedRenderer('feed-container');
        }
        return feedRenderer;
    }

    // Create renderer when DOM is ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', ensureFeedRenderer);
    } else {
        ensureFeedRenderer();
    }

    socket.on('connect', () => {
        logger.info('[Session] WebSocket connected');
        logger.debug('[Session] Emitting join_session for:', sessionId);
        socket.emit('join_session', { session_id: sessionId });

        // Mark socket as fully initialized
        socketInitialized = true;
        socketInitializing = false;
        connectionBackoff.reset();

        // Resolve the socketReady promise so chat.js can initialize
        if (window.socketReadyResolve) {
            window.socketReadyResolve(socket);
            logger.debug('[Session] ✅ Socket ready promise resolved');
        } else {
            logger.warn('[Session] ⚠️  window.socketReadyResolve not found');
        }

        // Update UI to show connected state
        setText('refresh-text', 'Live updates active');
        updateConnectionStatus('connected');
        
        // Emit connection event for other modules
        emit('socket:connected', { sessionId });

        logger.debug('[Session] ✅ WebSocket setup complete, waiting for events...');
    });

    // Handle initial feed from server - use EventStore for storage and ordering
    socket.on('initial_feed', async (payload) => {
        logger.debug('[Session] Received initial_feed, payload length=', payload?.length);
        try {
            // Clear loading indicator
            const loadingEl = document.getElementById('feed-loading');
            if (loadingEl) loadingEl.style.display = 'none';

            const items = JSON.parse(payload);
            logger.debug(`[Session] Parsed initial_feed with ${items.length} events`);

            // Insert batch into EventStore (handles ordering and dedup)
            const inserted = eventStore.insertBatch(items);
            logger.info(`[Session] EventStore: inserted ${inserted.length}/${items.length} events`);

            // Refresh sidebar after initial load
            try {
                const { refreshSidebar } = await import('./sidebar-data.js');
                refreshSidebar('all', true);
                emit('stats:refreshRequest');
            } catch (sidebarErr) {
                logger.warn('[Session] Sidebar refresh error:', sidebarErr.message);
            }
        } catch (err) {
            logger.error('[Session] Failed to parse initial feed:', err);
            showFeedError('Failed to load feed data. Please try refreshing the page.');
        }
    });

    // Handle chunked feed (backwards compatibility) - combine into single batch
    let chunkBuffer = { chunks: [], expected: 0 };

    socket.on('initial_feed_chunk', async (chunkData) => {
        logger.debug('[Session] Received initial_feed_chunk, index=', chunkData.chunk_index, 'of', chunkData.total_chunks);
        try {
            // Clear loading indicator on first chunk
            if (chunkData.chunk_index === 0) {
                const loadingEl = document.getElementById('feed-loading');
                if (loadingEl) loadingEl.style.display = 'none';
                chunkBuffer = { chunks: [], expected: chunkData.total_chunks };
            }

            chunkBuffer.chunks[chunkData.chunk_index] = chunkData.chunk;
            logger.debug(`[Session] Chunk ${chunkData.chunk_index + 1}/${chunkData.total_chunks}`);

            // Process when all chunks received
            if (chunkData.is_last) {
                const allItems = chunkBuffer.chunks.flat();
                const inserted = eventStore.insertBatch(allItems);
                logger.info(`[Session] EventStore: inserted ${inserted.length}/${allItems.length} from chunks`);
                chunkBuffer = { chunks: [], expected: 0 };

                // Refresh sidebar
                try {
                    const { refreshSidebar } = await import('./sidebar-data.js');
                    refreshSidebar('all', true);
                    emit('stats:refreshRequest');
                } catch (sidebarErr) {
                    logger.warn('[Session] Sidebar refresh error:', sidebarErr.message);
                }
            }
        } catch (err) {
            logger.error('[Session] Failed to process chunk:', err);
            chunkBuffer = { chunks: [], expected: 0 };
        }
    });

    socket.on('feed_update', (item) => {
        logger.debug('[Feed] 📨 Received feed_update:', item.type, '_seq=', item._seq);

        // Insert into EventStore (handles dedup and ordering)
        // Chat.js subscribes to EventStore directly - no separate event bus needed
        const inserted = eventStore.insert(item);
        if (inserted) {
            // Update sidebar based on event type
            handleSidebarUpdates(item);
        }
    });

    socket.on('disconnect', (reason) => {
        logger.warn('[Session] WebSocket disconnected:', reason);

        // Reset state to allow reconnection
        socketInitialized = false;
        socketInitializing = false;

        setText('refresh-text', 'Connection lost');
        updateConnectionStatus('disconnected');

        // Emit disconnect event for other modules
        emit('socket:disconnected', { reason });
    });
    
    socket.on('reconnect_attempt', (attemptNumber) => {
        logger.debug(`[Session] Reconnection attempt ${attemptNumber}`);
        setText('refresh-text', `Reconnecting... (${attemptNumber})`);
        updateConnectionStatus('reconnecting');
    });
    
    socket.on('reconnect', async (attemptNumber) => {
        logger.info(`[Session] Reconnected after ${attemptNumber} attempts`);
        setText('refresh-text', 'Live updates active');
        updateConnectionStatus('connected');
        connectionBackoff.reset();

        // Re-join the session room
        socket.emit('join_session', { session_id: sessionId });

        // Delta sync: fetch only events we missed during disconnection
        const lastSeq = eventStore.getLastSeq();
        if (lastSeq > 0) {
            try {
                logger.debug(`[Session] Delta sync: fetching events after _seq=${lastSeq}`);
                const response = await fetch(`/api/session/${sessionId}/feed/events?after_seq=${lastSeq}&limit=500`);
                if (response.ok) {
                    const data = await response.json();
                    const events = data.events || [];
                    if (events.length > 0) {
                        const inserted = eventStore.insertBatch(events);
                        logger.info(`[Session] Delta sync: inserted ${inserted.length} missed events`);
                    }
                }
            } catch (err) {
                logger.warn('[Session] Delta sync failed:', err);
            }
        }

        emit('socket:reconnected', { attemptNumber });
    });
    
    socket.on('reconnect_failed', () => {
        logger.error('[Session] Reconnection failed after all attempts');
        setText('refresh-text', 'Connection lost - please refresh');
        updateConnectionStatus('disconnected');
        emit('socket:reconnectFailed', {});
    });
    
    // Helper to update connection status indicator
    function updateConnectionStatus(status) {
        const indicator = document.getElementById('connection-status');
        if (indicator) {
            indicator.className = `connection-status ${status}`;
        }
    }

    /**
     * FeedRenderer - Renders events from EventStore to DOM
     * Subscribes to EventStore changes and renders filtered events.
     */
    function createFeedRenderer(containerId) {
        logger.debug('[FeedRenderer] Creating renderer for container:', containerId);
        const container = document.getElementById(containerId);
        if (!container) {
            logger.warn('[FeedRenderer] Container not found:', containerId);
            return { render: () => {}, clear: () => {}, destroy: () => {} };
        }
        logger.debug('[FeedRenderer] Container found:', containerId);

        // Track rendered event IDs to avoid re-rendering
        const renderedIds = new Set();

        // Subscribe to EventStore changes - save unsubscribe for cleanup
        const unsubscribe = eventStore.subscribe('all', (change) => {
            if (change.action === 'insert') {
                renderEvent(change.event);
            } else if (change.action === 'batch') {
                renderBatch(change.events);
            } else if (change.action === 'clear') {
                clear();
            }
        });

        // Render any existing events (handles race condition where events loaded before subscription)
        const existingEvents = eventStore.getAll();
        if (existingEvents.length > 0) {
            logger.debug(`[FeedRenderer] Rendering ${existingEvents.length} existing events`);
            renderBatch(existingEvents);
        }

        function renderEvent(event) {
            if (!feedFilter.shouldShow(event)) return;

            const eventId = event._id || `${event.type}_${event.timestamp}`;
            if (renderedIds.has(eventId)) return;
            renderedIds.add(eventId);

            const itemEl = document.createElement('div');
            itemEl.className = 'feed-item';
            itemEl.innerHTML = formatFeedItem(event);
            itemEl.dataset.seq = event._seq || 0;
            itemEl.dataset.id = eventId;

            if (event.type === 'llm_request') {
                itemEl.classList.add('feed-llm-item');
            }

            // Insert in correct position by _seq (newest first = prepend)
            insertBySeq(itemEl, event._seq || 0);
        }

        function renderBatch(events) {
            const toRender = events.filter(e => {
                if (!feedFilter.shouldShow(e)) return false;
                const eventId = e._id || `${e.type}_${e.timestamp}`;
                if (renderedIds.has(eventId)) return false;
                renderedIds.add(eventId);
                return true;
            });

            if (toRender.length === 0) return;

            // Sort by _seq descending (newest first)
            toRender.sort((a, b) => (b._seq || 0) - (a._seq || 0));

            const fragment = document.createDocumentFragment();
            toRender.forEach(event => {
                const itemEl = document.createElement('div');
                itemEl.className = 'feed-item';
                itemEl.innerHTML = formatFeedItem(event);
                itemEl.dataset.seq = event._seq || 0;
                itemEl.dataset.id = event._id || '';

                if (event.type === 'llm_request') {
                    itemEl.classList.add('feed-llm-item');
                }

                fragment.appendChild(itemEl);
            });

            // Prepend all (newest first)
            container.insertBefore(fragment, container.firstChild);
            logger.debug(`[FeedRenderer] Rendered ${toRender.length} events`);
        }

        function insertBySeq(itemEl, seq) {
            // For real-time updates, insert at correct position
            // Events are displayed newest first (descending _seq)
            const children = Array.from(container.children);

            for (let i = 0; i < children.length; i++) {
                const childSeq = parseInt(children[i].dataset.seq || '0', 10);
                if (seq > childSeq) {
                    container.insertBefore(itemEl, children[i]);
                    return;
                }
            }

            // Oldest event, append at end
            container.appendChild(itemEl);
        }

        function clear() {
            container.innerHTML = '';
            renderedIds.clear();
        }

        function destroy() {
            unsubscribe();
            clear();
            logger.debug('[FeedRenderer] Destroyed');
        }

        return { render: renderEvent, renderBatch, clear, destroy };
    }

    function formatFeedItem(item) {
        // Default formatting for unknown types
        if (!item || !item.type) {
            return `<pre class="feed-content">${escapeHtml(JSON.stringify(item, null, 2))}</pre>`;
        }
        
        // Format timestamp if available
        const timestamp = item.timestamp 
            ? new Date(item.timestamp * 1000).toLocaleTimeString() 
            : '';
        
        // Get icon and color based on type
        const typeInfo = getEventTypeInfo(item.type);
        
        let content = '';
        
        // Format content based on event type
        switch (item.type) {
            case 'step': {
                const stepNum = item.step || 0;
                const actions = item.data?.actions || [];
                
                // Improved agent name extraction with more fallbacks
                let agentName = null;
                
                // First try in data.agent_name (main and preferred source)
                if (item.data?.agent_name && item.data.agent_name !== 'Unknown') {
                    agentName = item.data.agent_name;
                }
                // Then try item.agent_name (older format)
                else if (item.agent_name) {
                    agentName = item.agent_name;
                }
                // Then try item.data.name 
                else if (item.data?.name) {
                    agentName = item.data.name;
                }
                // Last fallback - try to extract from agent_id if available
                else if (item.data?.agent_id) {
                    const agentId = item.data.agent_id;
                    // Extract prefix from format like "planner_xyz123"
                    if (agentId.includes('_')) {
                        const prefix = agentId.split('_')[0];
                        // Capitalize first letter for display
                        agentName = prefix.charAt(0).toUpperCase() + prefix.slice(1);
                    } else {
                        agentName = agentId; // Use entire ID if no underscore
                    }
                }
                
                // Final fallback
                if (!agentName) {
                    agentName = 'Agent';
                }
                
                logger.debug(`Step ${stepNum} agent: "${agentName}"`);

                // Detect next goal (preferred) or fallback to generic progress
                const nextGoal = item.data?.next_goal || item.data?.task_progress || '';

                // Extract error information if available
                const errors = item.data?.errors || [];
                const hasErrors = errors && errors.length > 0;

                // Build header: "Step N – Agent"
                const headerTitle = `Step ${stepNum} – ${agentName}`;

                // Compact actions summary for step feed items
                const actionsCountByService = {};
                actions.forEach(action => {
                    const service = detectServiceFromAction(action);
                    actionsCountByService[service] = (actionsCountByService[service] || 0) + 1;
                });
                const actionsSummary = Object.entries(actionsCountByService)
                    .map(([service, count]) => `${service}(${count})`)
                    .join(', ');

                content = `
                    <div class="feed-header">
                        <span class="feed-type"><span class="event-icon">${typeInfo.icon}</span> ${headerTitle}</span>
                        <span class="feed-time">${timestamp}</span>
                    </div>
                    <div class="feed-content compact">
                        ${nextGoal ? `<div class="event-next-goal">${escapeHtml(nextGoal)}</div>` : ''}
                        ${hasErrors ? `
                            <div class="event-error">
                                <div class="event-error-header">Errors:</div>
                                <div class="event-error-content">
                                    ${errors.map(error => `<div class="event-error-item">${escapeHtml(error)}</div>`).join('')}
                                </div>
                            </div>
                        ` : ''}
                        ${actionsSummary ? `<div class="event-actions-summary">${actionsSummary}</div>` : ''}
                    </div>
                `;
                break;
            }
                
            case 'planner':
                content = `
                    <div class="feed-header">
                        <span class="feed-type"><span class="event-icon">${typeInfo.icon}</span> Planner</span>
                        <span class="feed-time">${timestamp}</span>
                    </div>
                    <div class="feed-content">
                        <div class="event-step">Step ${item.step || 0}</div>
                        <div class="event-model">${item.data?.model_name || 'Unknown model'}</div>
                        ${formatPlannerComponents(item.data?.components || {})}
                    </div>
                `;
                break;
                
            case 'evaluation':
                content = `
                    <div class="feed-header">
                        <span class="feed-type"><span class="event-icon">${typeInfo.icon}</span> Evaluation</span>
                        <span class="feed-time">${timestamp}</span>
                    </div>
                    <div class="feed-content">
                        <div class="event-step">Step ${item.step || 0}</div>
                        <div class="event-assessment">${item.data?.assessment || 'No assessment'}</div>
                        ${formatEvaluationDetails(item.data || {})}
                    </div>
                `;
                break;
                
            case 'tool_execution': {
                const toolName = item.data?.tool_name || 'unknown';
                const actionName = item.data?.action_name || 'unknown';
                const success = item.data?.success !== false;
                const duration = item.data?.duration_seconds || 0;
                const parameters = item.data?.parameters || {};
                const error = item.data?.error;
                
                // Format parameters for display (compact)
                const paramsList = Object.entries(parameters)
                    .filter(([key, val]) => val !== null && val !== undefined)
                    .map(([key, val]) => {
                        const valStr = typeof val === 'string' ? val : JSON.stringify(val);
                        const truncated = valStr.length > 100 ? valStr.substring(0, 100) + '...' : valStr;
                        return `${key}=${truncated}`;
                    })
                    .join(', ');
                
                const statusIcon = success ? '✓' : '✗';
                const statusClass = success ? 'success' : 'error';
                
                content = `
                    <div class="feed-header">
                        <span class="feed-type">
                            <span class="event-icon">${typeInfo.icon}</span> 
                            <span class="tool-badge">${toolName}</span> → ${actionName}
                            <span class="status-${statusClass}">${statusIcon}</span>
                        </span>
                        <span class="feed-time">${timestamp} (${duration.toFixed(2)}s)</span>
                    </div>
                    ${paramsList ? `<div class="feed-content compact">
                        <div class="event-parameters">📋 ${escapeHtml(paramsList)}</div>
                    </div>` : ''}
                    ${error ? `<div class="feed-content compact">
                        <div class="event-error">❌ ${escapeHtml(error)}</div>
                    </div>` : ''}
                `;
                break;
            }
                
            case 'llm_request': {
                const tokenCount = item.data?.token_count || 0;
                const costEstimate = item.data?.cost_estimate || 0;
                
                // Determine model name with provider prefix if available
                let modelName = item.data?.model_name || item.data?.model || 'unknown';
                const provider = item.data?.provider || '';
                if (provider && !modelName.toLowerCase().includes(provider.toLowerCase())) {
                    modelName = `${provider} ${modelName}`;
                }

                const summary = `LLM Request ${modelName} Tokens: ${tokenCount.toLocaleString()} Cost: ${formatCost(costEstimate)}`;

                // Single-line minimal block
                content = `
                    <div class="feed-header feed-llm-line">
                        <span class="feed-type"><span class="event-icon">${typeInfo.icon}</span> ${escapeHtml(summary)}</span>
                        <span class="feed-time">${timestamp}</span>
                    </div>
                `;
                break;
            }
                
            case 'status':
                content = `
                    <div class="feed-header">
                        <span class="feed-type"><span class="event-icon">${typeInfo.icon}</span> Status Update</span>
                        <span class="feed-time">${timestamp}</span>
                    </div>
                    <div class="feed-content">
                        <div class="event-status-value">Status: <span class="status-${item.data?.status || 'unknown'}">${item.data?.status || 'Unknown'}</span></div>
                        ${item.data?.previous_status ? `<div class="event-previous-status">Previous: ${item.data.previous_status}</div>` : ''}
                    </div>
                `;
                break;
                
            case 'task_update':
                content = `
                    <div class="feed-header">
                        <span class="feed-type"><span class="event-icon">${typeInfo.icon}</span> Task Update</span>
                        <span class="feed-time">${timestamp}</span>
                    </div>
                    <div class="feed-content">
                        <div class="event-task">${item.data?.task || 'Unknown task'}</div>
                        <div class="event-status-value">Status: ${item.data?.status || 'Unknown'}</div>
                    </div>
                `;
                break;
                
            case 'agent_registration':
                content = `
                    <div class="feed-header">
                        <span class="feed-type"><span class="event-icon">${typeInfo.icon}</span> Agent Registration</span>
                        <span class="feed-time">${timestamp}</span>
                    </div>
                    <div class="feed-content">
                        <div class="event-agent-id">${item.data?.id || item.data?.agent_id || 'Unknown ID'}</div>
                        <div class="event-agent-name">${item.data?.name || item.data?.agent_name || 'Unknown agent'}</div>
                        <div class="event-agent-type">${item.data?.type || item.data?.agent_type || 'Unknown type'}</div>
                    </div>
                `;
                break;
                
            case 'multi_agent_relationship':
            case 'multi_agent_relationship_detailed':
                const data = item.data || {};
                const agentIds = data.agent_ids || data.execution_sequence || [];
                const agentDetails = data.agent_details || [];
                const agentModels = data.agent_models || {};
                
                // Create a comprehensive agent list with models
                const agentsWithModels = agentDetails.map(agent => ({
                    ...agent,
                    model: agent.model || agentModels[agent.id || agent.agent_id] || ''
                }));
                
                content = `
                    <div class="feed-header">
                        <span class="feed-type"><span class="event-icon">${typeInfo.icon}</span> Multi-Agent Setup</span>
                        <span class="feed-time">${timestamp}</span>
                    </div>
                    <div class="feed-content">
                        <div class="event-agent-count">${agentIds.length} agents in collaboration</div>
                        ${data.orchestrator_type ? `<div class="event-orchestrator">Orchestrator: ${data.orchestrator_type}</div>` : ''}
                        ${agentsWithModels.length > 0 ? formatAgentRelationships(agentsWithModels) : ''}
                        ${agentIds.length > 0 && agentsWithModels.length === 0 ? `
                            <div class="event-agents">
                                <div class="event-agents-header">Agent IDs:</div>
                                <div class="event-agents-list">
                                    ${agentIds.map(id => `<div class="event-agent-id">${id}</div>`).join('')}
                                </div>
                            </div>
                        ` : ''}
                    </div>
                `;
                break;
                
            case 'available_actions':
                const services = item.data?.by_service || {};
                const availableActionsHtml = Object.entries(services).map(([serviceName, actions]) => {
                    if (!Array.isArray(actions)) return '';
                    return `
                        <div class="service-section">
                            <div class="service-header">${serviceName}</div>
                            <div class="actions-list">
                                ${actions.map(actionName => `<span class="action-tag" title="${actionName}">${actionName}</span>`).join('')}
                            </div>
                        </div>`;
                }).join('');

                content = `
                    <div class="feed-header">
                        <span class="feed-type"><span class="event-icon">${typeInfo.icon}</span> Available Actions</span>
                        <span class="feed-time">${timestamp}</span>
                    </div>
                    <div class="feed-content">
                        ${availableActionsHtml || '<div class="empty-state">No actions listed</div>'}
                    </div>
                `;
                break;
                
            case 'service_actions':
                // Handle service_actions entries without displaying raw JSON
                const serviceInfo = item.data || {};
                
                // Extract only essential service information
                const serviceName = serviceInfo.service_name || 'Unknown Service';
                const serviceType = serviceInfo.service_type || '';
                const actionCount = serviceInfo.action_count || 0;
                
                // Get only the action names without their full definitions
                const actionsList = Array.isArray(serviceInfo.available_actions) 
                    ? serviceInfo.available_actions
                    : [];
                
                content = `
                    <div class="feed-header">
                        <span class="feed-type"><span class="event-icon">${typeInfo.icon}</span> Service Actions</span>
                        <span class="feed-time">${timestamp}</span>
                    </div>
                    <div class="feed-content">
                        <div class="service-section">
                            <div class="service-header">${serviceName} ${serviceType ? `(${serviceType})` : ''}</div>
                            <div class="actions-list">
                                ${actionsList.map(actionName => `<span class="action-tag" title="${actionName}">${actionName}</span>`).join('')}
                            </div>
                        </div>
                    </div>
                `;
                break;
                
            case 'event': {
                // Format generic events to show their payload in a clean way
                content = `
                    <div class="feed-header">
                        <span class="feed-type"><span class="event-icon">${typeInfo.icon}</span> ${item.data?.event_type || 'Event'}</span>
                        <span class="feed-time">${timestamp}</span>
                    </div>
                    <div class="feed-content">
                        <pre class="code-content">${escapeHtml(JSON.stringify(item.data || {}, null, 2))}</pre>
                    </div>
                `;
                break;
            }
            
            case 'agent_message':
                // Agent sent a message to user (via send_message action)
                const isQuestion = item.data?.wait_for_response === true;
                content = `
                    <div class="feed-header">
                        <span class="feed-type"><span class="event-icon">💬</span> ${isQuestion ? 'Agent Question' : 'Agent Message'}</span>
                        <span class="feed-time">${timestamp}</span>
                    </div>
                    <div class="feed-content agent-message">
                        <div class="message-text">${escapeHtml(item.data?.text || '')}</div>
                        ${isQuestion ? '<div class="message-waiting">⏳ Waiting for your response...</div>' : ''}
                    </div>
                `;
                break;
            
            case 'task_complete':
                // Task completion message (via done action)
                content = `
                    <div class="feed-header">
                        <span class="feed-type"><span class="event-icon">✅</span> Task Complete</span>
                        <span class="feed-time">${timestamp}</span>
                    </div>
                    <div class="feed-content task-complete">
                        <div class="completion-message">${escapeHtml(item.data?.message || 'Task completed')}</div>
                    </div>
                `;
                break;
                
            default:
                // Generic formatting for other types
                let dataContent = '';
                
                // Check if the data is a complex object that needs special handling
                if (item.data && typeof item.data === 'object') {
                    // Handle deep event structures and large objects
                    if (item.type === 'event') {
                        // Filter out registered_functions and other large metadata
                        const simplifiedData = { ...item.data };
                        
                        // Remove any large arrays/objects (special handling for agent communication)
                        if (Array.isArray(simplifiedData.registered_functions)) {
                            simplifiedData.functions_summary = `${simplifiedData.registered_functions.length} registered functions available`;
                            delete simplifiedData.registered_functions;
                        }
                        
                        // Summarize other large props
                        Object.keys(simplifiedData).forEach(key => {
                            if (key !== 'functions_summary') {
                                const value = simplifiedData[key];
                                
                                // Summarize large arrays
                                if (Array.isArray(value) && value.length > 5) {
                                    simplifiedData[key] = `[Array with ${value.length} items]`;
                                }
                                // Summarize large nested objects
                                else if (typeof value === 'object' && value !== null) {
                                    const objStr = JSON.stringify(value);
                                    if (objStr.length > 500) {
                                        const propCount = Object.keys(value).length;
                                        simplifiedData[key] = `[Object with ${propCount} properties]`;
                                    }
                                }
                            }
                        });
                        
                        dataContent = escapeHtml(JSON.stringify(simplifiedData, null, 2));
                    } else {
                        // For other objects, prepare cleaned display version
                        const displayObj = { ...item.data };
                        
                        // Look for large items to summarize
                        Object.keys(displayObj).forEach(key => {
                            const value = displayObj[key];
                            
                            // Summarize large arrays
                            if (Array.isArray(value) && value.length > 5) {
                                displayObj[key] = `[Array with ${value.length} items]`;
                            }
                            // Summarize large nested objects
                            else if (typeof value === 'object' && value !== null) {
                                const objStr = JSON.stringify(value);
                                if (objStr.length > 500) {
                                    const propCount = Object.keys(value).length;
                                    displayObj[key] = `[Object with ${propCount} properties]`;
                                }
                            }
                        });
                        
                        dataContent = escapeHtml(JSON.stringify(displayObj, null, 2));
                    }
                } else if (item.data === null || item.data === undefined) {
                    dataContent = '{}';
                } else {
                    // For primitive data types
                    dataContent = escapeHtml(JSON.stringify(item.data, null, 2));
                }
                
                content = `
                    <div class="feed-header">
                        <span class="feed-type"><span class="event-icon">${typeInfo.icon}</span> ${typeInfo.label}</span>
                        <span class="feed-time">${timestamp}</span>
                    </div>
                    <pre class="feed-content">${dataContent}</pre>
                `;
        }
        
        return content;
    }
    
    function getEventTypeInfo(type) {
        // Map of event types to icons and labels
        const eventTypes = {
            'step': { icon: '🤖', label: 'Agent Step', className: 'event-agent-step' },
            'tool_execution': { icon: '🔧', label: 'Tool Execution', className: 'event-tool-execution' },
            'planner': { icon: '🧠', label: 'Planner', className: 'event-planner' },
            'agent_message': { icon: '💬', label: 'Agent Message', className: 'event-agent-message' },
            'task_complete': { icon: '✅', label: 'Task Complete', className: 'event-task-complete' },
            'evaluation': { icon: '📊', label: 'Evaluation', className: 'event-evaluation' },
            'multi_agent_relationship': { icon: '🔄', label: 'Agent Relationship', className: 'event-relationship' },
            'agent_registration': { icon: '📝', label: 'Agent Registration', className: 'event-registration' },
            'session_start': { icon: '🚀', label: 'Session Start', className: 'event-session-start' },
            'task_update': { icon: '📋', label: 'Task Update', className: 'event-task-update' },
            'llm_request': { icon: '💬', label: 'LLM Request', className: 'event-llm-request' },
            'service_actions': { icon: '🔧', label: 'Service Actions', className: 'event-service-actions' },
            'available_actions': { icon: '⚡', label: 'Available Actions', className: 'event-available-actions' },
            'status': { icon: '🔔', label: 'Status Update', className: 'event-status' }
        };

        return eventTypes[type] || { icon: '📌', label: 'Event', className: 'event-generic' };
    }

    /**
     * Detect service name from action object
     */
    function detectServiceFromAction(action) {
        if (action.service) return action.service;

        const actionName = action.name || action.action_type || '';
        const serviceMap = {
            'open_tab': 'browser',
            'close_tab': 'browser',
            'click': 'browser',
            'type_text': 'browser',
            'scroll': 'browser',
            'go_back': 'browser',
            'refresh': 'browser',
            'search': 'browser',
            'write_file': 'filesystem',
            'read_file': 'filesystem',
            'list_files': 'filesystem',
            'delete_file': 'filesystem',
            'create_directory': 'filesystem'
        };

        return serviceMap[actionName] || 'default';
    }
    
    /**
     * Format action items in the feed
     */
    function formatActions(actions) {
        if (!actions || !actions.length) return '';
        
        return `
            <div class="event-actions">
                <div class="event-actions-header">Actions (${actions.length}):</div>
                <div class="event-actions-list">
                    ${actions.map(action => {
                        const serviceName = action.service || 'default';
                        const actionName = action.name || action.action_type || 'unknown';
                        
                        // Skip rendering params for certain large payload actions
                        const skipParamsDisplay = actionName === 'extract_content' || 
                                               (actionName === 'process_document' && action.content && action.content.length > 1000);
                        
                        // Special handling for open_tab actions
                        const isOpenTab = actionName === 'open_tab';
                        
                        // For open_tab actions, create a more compact representation
                        if (isOpenTab && action.url) {
                            return `
                                <div class="event-action event-action-url">
                                    <div class="event-action-header">
                                        <span class="event-action-service">${serviceName}</span>
                                        <span class="event-action-separator">→</span>
                                        <span class="event-action-name">${actionName}</span>
                                    </div>
                                    <div class="compact-url">
                                        <a href="${action.url}" target="_blank" rel="noopener noreferrer" title="${action.url}">${escapeHtml(action.url)}</a>
                                    </div>
                                </div>
                            `;
                        }
                        
                        // For standard actions, use the normal representation
                        return `
                            <div class="event-action ${isOpenTab ? 'event-action-url' : ''}">
                                <div class="event-action-header">
                                    <span class="event-action-service">${serviceName}</span>
                                    <span class="event-action-separator">→</span>
                                    <span class="event-action-name">${actionName}</span>
                                </div>
                                ${skipParamsDisplay ? 
                                    '<div class="param-summary">Large content parameters omitted</div>' : 
                                    createParamsTable(action)}
                            </div>
                        `;
                    }).join('')}
                </div>
            </div>
        `;
    }
    
    function formatPlannerComponents(components) {
        if (!components || Object.keys(components).length === 0) return '';
        
        return `
            <div class="event-components">
                <div class="event-components-header">Components:</div>
                <div class="event-components-list">
                    ${Object.entries(components).map(([key, value]) => {
                        const formattedKey = key.replace('has_', '').replace(/_/g, ' ');
                        return `
                            <div class="event-component">
                                <span class="event-component-name">${formattedKey}</span>
                                <span class="event-component-value">${value ? '✓' : '✗'}</span>
                            </div>
                        `;
                    }).join('')}
                </div>
            </div>
        `;
    }
    
    function formatEvaluationDetails(evaluation) {
        if (!evaluation) return '';
        
        let details = '';
        
        if (evaluation.strengths && evaluation.strengths.length) {
            details += `
                <div class="event-eval-section">
                    <div class="event-eval-header">Strengths:</div>
                    <ul class="event-eval-list">
                        ${evaluation.strengths.map(item => `<li>${item}</li>`).join('')}
                    </ul>
                </div>
            `;
        }
        
        if (evaluation.weaknesses && evaluation.weaknesses.length) {
            details += `
                <div class="event-eval-section">
                    <div class="event-eval-header">Weaknesses:</div>
                    <ul class="event-eval-list">
                        ${evaluation.weaknesses.map(item => `<li>${item}</li>`).join('')}
                    </ul>
                </div>
            `;
        }
        
        if (evaluation.suggestions && evaluation.suggestions.length) {
            details += `
                <div class="event-eval-section">
                    <div class="event-eval-header">Suggestions:</div>
                    <ul class="event-eval-list">
                        ${evaluation.suggestions.map(item => `<li>${item}</li>`).join('')}
                    </ul>
                </div>
            `;
        }
        
        return details;
    }
    
    function formatAgentRelationships(agents) {
        if (!agents || !agents.length) return '';
        
        return `
            <div class="event-agents">
                <div class="event-agents-header">Agents:</div>
                <div class="event-agents-list">
                    ${agents.map(agent => {
                        return `
                            <div class="event-agent">
                                <span class="event-agent-name">${agent.name || agent.agent_name || 'Unknown'}</span>
                                <span class="event-agent-type">${agent.type || agent.agent_type || 'Unknown'}</span>
                                ${agent.model ? `<span class="event-agent-model">${agent.model}</span>` : ''}
                            </div>
                        `;
                    }).join('')}
                </div>
            </div>
        `;
    }

    function scrollToBottom(element) {
        element.scrollTop = element.scrollHeight;
    }

    function showFeedError(message) {
        const errorEl = document.getElementById('feed-error');
        if (errorEl) {
            errorEl.textContent = message;
            errorEl.style.display = 'block';
        }
    }

    /**
     * Creates a clean, tabular representation of action parameters
     */
    function createParamsTable(action) {
        // Expanded list of keys to skip from display
        const skipKeys = [
            'service', 'name', 'action_type', 'registered_functions', 
            'by_service', 'action_count', 'service_name', 'service_type',
            'available_actions', 'orchestrator_type'
        ];
        
        // If action contains a nested "params" object, merge its keys at the same
        // level for display purposes so we don't show a redundant "params" row.
        const merged = { ...action };
        if (merged.params && typeof merged.params === 'object') {
            Object.assign(merged, merged.params);
            delete merged.params;
        }

        // Filter out service and action internal keys and massive payload objects
        let params = Object.entries(merged).filter(([k, v]) => {
            // Skip system/meta properties
            if (skipKeys.includes(k)) return false;
            
            // Skip large JSON objects (like registered_functions or by_service payloads)
            if (typeof v === 'object' && v !== null) {
                // Check if this is a service definition object
                if (k === 'services' || 
                    (typeof v === 'object' && 
                     (v.service_name || v.service_type || v.available_actions))) {
                    return false;
                }
                
                const jsonStr = JSON.stringify(v);
                // Skip large objects over 1000 chars
                if (jsonStr.length > 1000) return false;
                
                // Skip service-related objects or arrays that contain service definitions
                if (jsonStr.includes('"service":') || 
                    jsonStr.includes('"available_actions":') ||
                    jsonStr.includes('"service_name":') ||
                    jsonStr.includes('"orchestrator_type":')) {
                    return false;
                }
            }
            
            return true;
        });

        // Prioritise specific keys
        const priority = ['next_goal', 'criteria', 'query', 'text', 'file_path', 'url'];
        params.sort(([a], [b]) => {
            const ai = priority.indexOf(a);
            const bi = priority.indexOf(b);
            if (ai === -1 && bi === -1) return 0;
            if (ai === -1) return 1;
            if (bi === -1) return -1;
            return ai - bi;
        });
        
        if (!params.length) {
            // If we've filtered everything out, just show a summary message
            return `<div class="param-summary">Simple action with no additional parameters</div>`;
        }

        // Special handling for file operations
        if (action.name === 'write_file' || action.name === 'read_file') {
            return createFileParamsTable(params);
        }
        
        // Special handling for URL actions
        if (action.name === 'open_tab' || (action.name && action.name.includes('url'))) {
            return createUrlParamsTable(params);
        }
        
        // Default parameter table
        return `
            <table class="params-table">
                <tbody>
                    ${params.map(([key, value]) => {
                        // Format value based on type
                        let displayValue;
                        if (typeof value === 'object' && value !== null) {
                            // For objects, check size first
                            const jsonStr = JSON.stringify(value, null, 2);
                            
                            // For very large objects, show a summary instead
                            if (jsonStr.length > 500) {
                                if (Array.isArray(value)) {
                                    return `
                                        <tr>
                                            <td class="param-name">${escapeHtml(key)}</td>
                                            <td class="param-value"><div class="param-summary">Array with ${value.length} items</div></td>
                                        </tr>
                                    `;
                                } else {
                                    return `
                                        <tr>
                                            <td class="param-name">${escapeHtml(key)}</td>
                                            <td class="param-value"><div class="param-summary">Object with ${Object.keys(value).length} properties</div></td>
                                        </tr>
                                    `;
                                }
                            }
                            
                            // For normal sized objects, format as pretty JSON
                            displayValue = escapeHtml(jsonStr);
                            return `
                                <tr>
                                    <td class="param-name">${escapeHtml(key)}</td>
                                    <td class="param-value"><pre class="json-value">${displayValue}</pre></td>
                                </tr>
                            `;
                        } else {
                            // Format simple values directly
                            const valueStr = String(value);
                            
                            // Check if the value looks like a URL
                            if (isUrl(valueStr)) {
                                return `
                                    <tr>
                                        <td class="param-name">${escapeHtml(key)}</td>
                                        <td class="param-value url-value">
                                            <a href="${valueStr}" target="_blank" rel="noopener noreferrer" title="${valueStr}">${escapeHtml(valueStr)}</a>
                                        </td>
                                    </tr>
                                `;
                            } else {
                                displayValue = escapeHtml(valueStr);
                                return `
                                    <tr>
                                        <td class="param-name">${escapeHtml(key)}</td>
                                        <td class="param-value">${displayValue}</td>
                                    </tr>
                                `;
                            }
                        }
                    }).join('')}
                </tbody>
            </table>
        `;
    }
    
    /**
     * Create a specialized table for file operations
     */
    function createFileParamsTable(params) {
        // Extract file path and content
        let filePath = '';
        let fileContent = '';
        let fileType = '';
        const otherParams = [];
        
        params.forEach(([key, value]) => {
            if (key === 'file_path') {
                filePath = value;
                fileType = value.split('.').pop().toLowerCase();
            } else if (key === 'content' || key === 'file_content') {
                fileContent = value;
            } else {
                otherParams.push([key, value]);
            }
        });
        
        // Start with the simple parameters
        let html = '<table class="params-table">';
        
        // Always show file path first
        if (filePath) {
            html += `
                <tr>
                    <td class="param-name">file_path</td>
                    <td class="param-value file-path">${escapeHtml(filePath)}</td>
                </tr>
            `;
        }
        
        // Add any other parameters except content (which we'll handle separately)
        otherParams.forEach(([key, value]) => {
            const valueStr = String(value);
            
            // Check if the value looks like a URL
            if (isUrl(valueStr)) {
                html += `
                    <tr>
                        <td class="param-name">${escapeHtml(key)}</td>
                        <td class="param-value url-value">
                            <a href="${valueStr}" target="_blank" rel="noopener noreferrer" title="${valueStr}">${escapeHtml(valueStr)}</a>
                        </td>
                    </tr>
                `;
            } else {
                html += `
                    <tr>
                        <td class="param-name">${escapeHtml(key)}</td>
                        <td class="param-value">${escapeHtml(valueStr)}</td>
                    </tr>
                `;
            }
        });
        
        html += '</table>';
        
        // Add file content if present
        if (fileContent) {
            const langClass = fileType === 'md' ? 'code-md' : 
                            fileType === 'json' ? 'code-json' : 
                            fileType === 'html' ? 'code-html' : 
                            fileType === 'css' ? 'code-css' : 
                            fileType === 'js' ? 'code-js' : 
                            fileType === 'py' ? 'code-py' : '';
            
            // Special handling for fileType that might contain URLs
            if (fileType === 'md' || fileType === 'html' || fileType === 'txt') {
                // For text-based content, look for possible URLs to enhance
                html += `
                    <div class="file-content-header">Content:</div>
                    <pre class="file-content ${langClass}"><code>${formatContentWithUrls(fileContent)}</code></pre>
                `;
            } else {
                html += `
                    <div class="file-content-header">Content:</div>
                    <pre class="file-content ${langClass}"><code>${escapeHtml(fileContent)}</code></pre>
                `;
            }
        }
        
        return html;
    }
    
    /**
     * Format content with URL detection and turn URLs into clickable links
     */
    function formatContentWithUrls(content) {
        if (typeof content !== 'string') {
            return escapeHtml(String(content));
        }
        
        // First escape the HTML to prevent XSS
        const escapedContent = escapeHtml(content);
        
        // Simple regex to find URLs in text
        const urlRegex = /https?:\/\/[^\s"<>()]+/g;
        
        // Replace URLs with clickable links
        return escapedContent.replace(urlRegex, (url) => {
            return `<a href="${url}" target="_blank" rel="noopener noreferrer" class="content-url" title="${url}">${url}</a>`;
        });
    }
    
    /**
     * Creates special handling for open_tab or URL-related actions
     * This function needs to properly format URLs without splitting them
     */
    function createUrlParamsTable(params) {
        // Start with a table for parameters
        let html = '<table class="params-table">';
        
        // Process each parameter
        params.forEach(([key, value]) => {
            const valueStr = String(value || '');
            
            // Special handling for URL parameter
            if (key === 'url') {
                html += `
                    <tr>
                        <td class="param-name">url</td>
                        <td class="param-value url-value" colspan="2">
                            <a href="${valueStr}" target="_blank" rel="noopener noreferrer" title="${valueStr}">${escapeHtml(valueStr)}</a>
                        </td>
                    </tr>
                `;
            } 
            // Check if any other value looks like a URL
            else if (isUrl(valueStr)) {
                html += `
                    <tr>
                        <td class="param-name">${escapeHtml(key)}</td>
                        <td class="param-value url-value" colspan="2">
                            <a href="${valueStr}" target="_blank" rel="noopener noreferrer" title="${valueStr}">${escapeHtml(valueStr)}</a>
                        </td>
                    </tr>
                `;
            }
            // Standard parameter
            else {
                html += `
                    <tr>
                        <td class="param-name">${escapeHtml(key)}</td>
                        <td class="param-value">${escapeHtml(valueStr)}</td>
                    </tr>
                `;
            }
        });
        
        html += '</table>';
        return html;
    }

    /**
     * Helper function to check if a string looks like a URL
     * Handles various formats including partial URLs
     */
    function isUrl(str) {
        if (!str || typeof str !== 'string') return false;
        
        // First check for full URLs with common protocols
        if (str.match(/^(https?|ftp):\/\/[^\s/$.?#].[^\s]*$/i)) {
            return true;
        }
        
        // Check for domain patterns that might be URLs without protocols
        if (str.match(/^www\.[a-z0-9]+([\-\.]{1}[a-z0-9]+)*\.[a-z]{2,}(:[0-9]{1,5})?(\/.*)?$/i)) {
            return true;
        }
        
        // Check for domain with common TLDs without www
        if (str.match(/^[a-z0-9]+([\-\.]{1}[a-z0-9]+)*\.(com|org|net|edu|gov|mil|io|co|ai|app)(\/.*)?$/i)) {
            return true;
        }
        
        return false;
    }

    /**
     * Synchronise the INFO sidebar with the latest event.
     * We avoid expensive network calls unless required, updating strings
     * directly when possible. For structural changes, we emit events for the
     * sidebar to handle refreshes.
     */
    function handleSidebarUpdates(item) {
        if (!item || !item.type) return;

        switch (item.type) {
            case 'status':
                if (item.data?.status) {
                    setText('current-task-text', item.data.status);
                    // Also update session status
                    updateSessionStatus(item.data.status);
                    // Request session info refresh via event
                    emit('sidebar:refreshRequest', { kind: 'session' });
                }
                break;

            case 'task_update':
                if (item.data?.task) {
                    setText('task-display', item.data.task);
                }
                if (item.data?.status) {
                    setText('current-task-text', item.data.status);
                    // Request task and session refresh via events
                    emit('sidebar:refreshRequest', { kind: 'task' });
                    emit('sidebar:refreshRequest', { kind: 'session' });
                }
                break;

            case 'agent_registration':
            case 'multi_agent_relationship':
                // Agents list potentially changed → request refresh
                emit('sidebar:refreshRequest', { kind: 'agents' });
                break;

            case 'service_actions':
            case 'available_actions':
                // Services catalogue updated
                emit('sidebar:refreshRequest', { kind: 'services' });
                break;

            case 'llm_request':
                // Stats (tokens / cost) likely changed
                emit('stats:refreshRequest');
                break;

            default:
                break;
        }
    }

    // ------------------------------------------------------------------
    // Fallback: if no WebSocket payload arrives within a short time span
    // (e.g. due to missed emission before join), fetch the latest events via
    // the REST endpoint so the feed is never empty.
    // ------------------------------------------------------------------
    setTimeout(async () => {
        // Check if EventStore already has events (from WS)
        if (eventStore.size > 0) return;

        try {
            const response = await fetch(`/api/session/${sessionId}/feed/events?limit=500`);
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const data = await response.json();

            // Handle new API format { events: [...], last_seq: N }
            const events = data.events || data;
            if (Array.isArray(events) && events.length) {
                const inserted = eventStore.insertBatch(events);
                logger.info(`[Session] REST fallback: inserted ${inserted.length} events`);
            } else {
                logger.warn('[Session] Feed fallback returned no events');
            }
        } catch (err) {
            logger.error('[Session] Feed fallback failed:', err);
        }
    }, 1500); // 1.5-second grace period after page load for faster recovery
    
    } finally {
        // Release the lock - MUST happen regardless of outcome
        if (releaseLock) releaseLock();
    }
}

// Function to trigger socket initialization (can be called externally)
function triggerSocketInitialization() {
    logger.debug('[Session] triggerSocketInitialization called');

    // Use centralized session state
    const currentSessionId = sessionState.sessionId;
    const isNew = sessionState.isNew;

    if (!currentSessionId || isNew) {
        logger.debug('[Session] Cannot initialize - no valid session ID');
        return;
    }

    if (window.io) {
        initializeSocket(window.io);
    } else {
        logger.error('[Session] Socket.IO not available');
    }
}

// Expose function globally for other modules to call
window.triggerSocketInitialization = triggerSocketInitialization;

// Check if Socket.IO is already loaded and initialize if we have an active session
if (window.io) {
    initializeSocket(window.io);
} else {
    // Wait for Socket.IO to load
    window.socketIOReady.then((io) => {
        initializeSocket(io);
    }).catch(err => {
        logger.error('[Session] Failed to load Socket.IO:', err);
        setText('refresh-text', 'Connection failed');
        const feedError = document.getElementById('feed-error');
        if (feedError) {
            feedError.textContent = 'Failed to establish connection. Please refresh the page.';
            feedError.style.display = 'block';
        }
    });
}

// Export functions for other modules
export { initializeSocket };

/**
 * Update the session status in the sidebar
 */
function updateSessionStatus(status) {
    // Format and update the status text if the element exists
    const sessionStatusEl = document.getElementById('session-status');
    if (sessionStatusEl && status) {
        sessionStatusEl.textContent = status.charAt(0).toUpperCase() + status.slice(1);
    }
}

/**
 * New Session functionality
 * Redirects to /new page where user can enter task in chat interface
 */
document.addEventListener('DOMContentLoaded', () => {
    const newSessionBtn = document.getElementById('new-session-btn');

    if (newSessionBtn) {
        newSessionBtn.addEventListener('click', () => {
            // Redirect to new session page (chat.html) where user can type task
            window.location.href = '/new';
        });
    }
});

