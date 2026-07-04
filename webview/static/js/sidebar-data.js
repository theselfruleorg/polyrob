/* Sidebar data management for WebView */
import { setText, fetchJSON, on, emit, sessionState, logger } from '/static/js/ui-utils.js?v=2';

// Refresh state tracking
const REFRESH_INTERVAL = 15000; // 15 seconds
let refreshTimer = null;
let isRefreshing = false;
let lastRefreshTime = {
    session: 0,
    task: 0,
    agents: 0,
    services: 0,
    skills: 0
};

// Create debounce cache to prevent overlapping requests
const _inflightTokens = {
    task: 0,
    agents: 0,
    services: 0,
    session: 0,
    skills: 0
};

function _nextToken(kind) {
    _inflightTokens[kind] = (_inflightTokens[kind] || 0) + 1;
    return _inflightTokens[kind];
}

function _isLatest(kind, token) {
    return token === _inflightTokens[kind];
}

// Track in-flight requests
const _inflightRequests = {
    agents: null,
    services: null,
    task: null,
    session: null,
    skills: null
};

document.addEventListener('DOMContentLoaded', function() {
    // Initialize sidebar data once
    refreshSidebar('all');
    
    // Set up auto-refresh with a single timer that handles all updates
    refreshTimer = setInterval(() => {
        // Only refresh if page is visible and we're not already refreshing
        if (document.visibilityState === 'visible' && !isRefreshing) {
            refreshSidebar('all');
        }
    }, REFRESH_INTERVAL);
    
    // Listen for specific refresh requests from other modules
    on('sidebar:refreshRequest', (event) => {
        const kind = event.detail?.kind || 'all';
        refreshSidebar(kind);
    });
    
    // Pause refreshes when page is hidden
    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'hidden') {
            logger.debug('[Sidebar] Page hidden, pausing sidebar refreshes');
        } else {
            logger.debug('[Sidebar] Page visible, resuming sidebar refreshes');
            // Refresh immediately when becoming visible again
            refreshSidebar('all');
        }
    });
});

/**
 * Central refresh function that manages all sidebar data updates
 * @param {string} kind - What to refresh: 'all', 'session', 'task', 'agents', or 'services'
 * @param {boolean} force - Force refresh regardless of timing
 */
function refreshSidebar(kind = 'all', force = false) {
    // Don't try to load data for new sessions (empty state)
    const sessionId = sessionState.sessionId;
    
    if (!sessionId || sessionState.isNew) {
        logger.debug('[Sidebar] Skipping refresh - no active session yet');
        return;
    }

    if (isRefreshing && !force) return;

    logger.debug(`[Sidebar] Refreshing sidebar: ${kind}`);
    isRefreshing = true;

    const now = Date.now();
    const debounceTime = 2000; // 2 second debounce
    
    try {
        if (kind === 'all' || kind === 'session') {
            if (force || now - lastRefreshTime.session > debounceTime) {
                updateSessionInfo();
                lastRefreshTime.session = now;
            }
        }
        
        if (kind === 'all' || kind === 'task') {
            if (force || now - lastRefreshTime.task > debounceTime) {
                loadTask();
                lastRefreshTime.task = now;
            }
        }
        
        if (kind === 'all' || kind === 'agents') {
            if (force || now - lastRefreshTime.agents > debounceTime) {
                loadAgents();
                lastRefreshTime.agents = now;
            }
        }
        
        if (kind === 'all' || kind === 'services') {
            if (force || now - lastRefreshTime.services > debounceTime) {
                loadServices();
                lastRefreshTime.services = now;
            }
        }
        
        if (kind === 'all' || kind === 'skills') {
            if (force || now - lastRefreshTime.skills > debounceTime) {
                loadSkills();
                lastRefreshTime.skills = now;
            }
        }
    } finally {
        isRefreshing = false;
    }
}

/**
 * Update session information in the sidebar
 */
function updateSessionInfo() {
    const sessionId = sessionState.sessionId;
    if (!sessionId) return;
    
    const token = _nextToken('session');
    logger.debug('[Sidebar] Updating session info for', sessionId);
    
    // Don't start a new request if one is already in progress
    if (_inflightRequests.session) {
        logger.debug('[Sidebar] Session info request already in progress, skipping');
        return;
    }
    
    // Update session ID (should already be in the template, but just in case)
    const sessionIdEl = document.getElementById('session-id');
    if (sessionIdEl) {
        sessionIdEl.textContent = sessionId;
    }

    // Get session status and update it
    try {
        // Set in-flight request
        _inflightRequests.session = true;
        
        // Use fetchJSON utility with proper error handling
        fetchJSON(`/api/session/${sessionId}/status`)
            .then(data => {
                // Clear in-flight request
                _inflightRequests.session = null;
                
                // Guard against race
                if (!_isLatest('session', token)) {
                    logger.debug('[Sidebar] Discarding stale session response');
                    return;
                }
                
                logger.debug('[Sidebar] Session status response:', data);
                const statusEl = document.getElementById('session-status');
                if (statusEl) {
                    if (data && data.status) {
                        // Format the status: capitalize first letter
                        const formattedStatus = data.status.charAt(0).toUpperCase() + data.status.slice(1);
                        statusEl.textContent = formattedStatus;
                        logger.debug('[Sidebar] Updated session status to:', formattedStatus);
                        
                        // Add status class for styling
                        statusEl.className = ''; // Clear existing classes
                        if (data.status === 'running' || data.status === 'active') {
                            statusEl.classList.add('status-running');
                        } else if (data.status === 'completed' || data.status === 'success') {
                            statusEl.classList.add('status-completed');
                        } else if (data.status === 'failed' || data.status === 'error') {
                            statusEl.classList.add('status-failed');
                        }
                    } else {
                        statusEl.textContent = 'Unknown';
                        logger.debug('[Sidebar] No status in response, setting to Unknown');
                    }
                }
                
                // Also update session duration if available
                if (data && data.created_at) {
                    updateSessionDuration(data.created_at);
                }
                
                // Emit event that session info is updated
                emit('sidebar:sessionUpdated', { status: data?.status });
            })
            .catch(err => {
                // Clear in-flight request on error
                _inflightRequests.session = null;
                
                logger.warn('[Sidebar] Failed to fetch session status:', err);
                // Set a fallback status on error
                const statusEl = document.getElementById('session-status');
                if (statusEl) {
                    statusEl.textContent = 'Unknown';
                }
            });
    } catch (e) {
        // Clear in-flight request
        _inflightRequests.session = null;
        logger.warn('[Sidebar] Error in updateSessionInfo:', e);
    }
}

/**
 * Update the session duration display
 * @param {number} createdAt - Timestamp when session was created
 */
function updateSessionDuration(createdAt) {
    const durationEl = document.getElementById('session-duration');
    if (!durationEl || !createdAt) return;
    
    // Calculate duration
    const now = Date.now() / 1000;
    const startTime = Number(createdAt);
    const duration = now - startTime;
    
    // Format duration
    let formattedDuration = '';
    if (duration < 60) {
        formattedDuration = `${Math.floor(duration)}s`;
    } else if (duration < 3600) {
        const minutes = Math.floor(duration / 60);
        const seconds = Math.floor(duration % 60);
        formattedDuration = `${minutes}m ${seconds}s`;
    } else {
        const hours = Math.floor(duration / 3600);
        const minutes = Math.floor((duration % 3600) / 60);
        formattedDuration = `${hours}h ${minutes}m`;
    }
    
    durationEl.textContent = formattedDuration;
    logger.debug('[Sidebar] Updated session duration:', formattedDuration);
    
    // Schedule next update in 1 second
    setTimeout(() => updateSessionDuration(createdAt), 1000);
}

/**
 * Load agents information for the session
 */
async function loadAgents() {
    const sessionId = sessionState.sessionId;
    if (!sessionId) return;
    
    const token = _nextToken('agents');
    const agentsContainer = document.getElementById('agents-container');
    
    if (!agentsContainer) return;
    
    // Don't start a new request if one is already in progress
    if (_inflightRequests.agents) {
        logger.debug('[Sidebar] Agents request already in progress, skipping');
        return;
    }
    
    try {
        // Store current HTML before updating to avoid flicker
        const currentContent = agentsContainer.innerHTML;
        
        // Add cache-busting parameter to force a fresh load without caching
        const timestamp = new Date().getTime();
        const url = `/api/session/${sessionId}/agents?nocache=${timestamp}`;
        
        // Set the in-flight request
        _inflightRequests.agents = url;
        
        // Fetch agents data using the utility function with the non-cached URL
        const result = await fetchJSON(url);
        
        // Clear in-flight request now that it's done
        _inflightRequests.agents = null;
        
        // Guard against race – if another call was triggered afterwards we
        // ignore this stale result.
        if (!_isLatest('agents', token)) {
            logger.debug('[Sidebar] Discarding stale agents response');
            return;
        }
        
        logger.debug('[Sidebar] Agents data:', result);
        
        // Update the agents container
        if (result.agents && result.agents.length > 0) {
            const agentsHtml = result.agents.map(agent => {
                // Extract agent data using consistent field names
                // Support both field naming conventions for compatibility
                const agentId = agent.id || agent.agent_id || 'unknown';
                const agentName = agent.name || agent.agent_name || '';
                
                // Properly format agent type - ensure full agent type name
                let agentType = agent.type || agent.agent_type || 'Unknown';
                
                // Add "Agent" suffix if missing and not already containing "Agent"
                if (!agentType.includes("Agent") && agentType !== "Unknown") {
                    // Special handling for common agent types
                    if (agentType.toLowerCase() === "planner") {
                        agentType = "PlannerAgent";
                    } else if (agentType.toLowerCase() === "executor") {
                        agentType = "ExecutorAgent";
                    } else if (agentType.toLowerCase() === "evaluator") {
                        agentType = "EvaluatorAgent";
                    } else if (agentType.toLowerCase() === "orchestrator") {
                        agentType = "SessionOrchestrator";
                    } else {
                        agentType = `${agentType}Agent`;
                    }
                }
                
                // Get model name - check both field naming conventions
                let agentModel = agent.model || agent.model_name || '';
                
                // Clean up "Unknown" or empty values
                if (!agentModel || agentModel === 'Unknown' || agentModel === 'None') {
                    agentModel = '';
                }
                
                // Log model information for debugging
                logger.debug(`[Sidebar] Agent "${agentName}" model: "${agentModel}", type: ${agentType}`);
                
                // Determine agent role/badge based on name or type - use generic approach
                // Extract role from agent name - simply use agent name or type as badge
                let role = '';
                if (agentName) {
                    role = agentName.toLowerCase();
                } else if (agentType && agentType !== 'Unknown') {
                    role = agentType.toLowerCase().replace('agent', '').replace('session', '');
                } else {
                    role = 'agent';
                }
                
                // Use role name as the display title, properly formatted
                let roleDisplay = role.charAt(0).toUpperCase() + role.slice(1);
                
                // Properly clean up common agent roles for display
                if (roleDisplay === "Executor") roleDisplay = "Executor";
                if (roleDisplay === "Planner") roleDisplay = "Planner";
                if (roleDisplay === "Evaluator") roleDisplay = "Evaluator";
                if (roleDisplay === "Orchestrator") roleDisplay = "Orchestrator";
                
                return `
                    <div class="agent-item">
                        <div class="agent-info-row">
                            <div class="agent-role">${roleDisplay}</div>
                            <div class="agent-type">${agentType}</div>
                            ${agentModel ? `<div class="agent-model" title="${agentModel}">${agentModel}</div>` : ''}
                        </div>
                    </div>
                `;
            }).join('');
            
            // Compare with existing content before updating to avoid DOM flicker
            const newContent = agentsHtml;
            if (newContent !== currentContent) {
                agentsContainer.innerHTML = newContent;
            }
        } else {
            // Only update if necessary
            if (currentContent !== '<div class="empty-state">No agents available</div>') {
                agentsContainer.innerHTML = '<div class="empty-state">No agents available</div>';
            }
        }
        
        // Emit event that agents are updated
        emit('sidebar:agentsUpdated');
    } catch (error) {
        // Clear in-flight request on error
        _inflightRequests.agents = null;
        
        logger.error('Error loading agents:', error);
        // Only update with error if not already showing an error
        if (!agentsContainer.innerHTML.includes('error-message')) {
            agentsContainer.innerHTML = `<div class="error-message">Error loading agents: ${error.message}</div>`;
        }
    }
}

/**
 * Load services information for the session
 */
async function loadServices() {
    const sessionId = sessionState.sessionId;
    if (!sessionId) return;
    
    const token = _nextToken('services');
    const servicesContainer = document.getElementById('services-container');
    
    if (!servicesContainer) return;
    
    // Don't start a new request if one is already in progress
    if (_inflightRequests.services) {
        logger.debug('[Sidebar] Services request already in progress, skipping');
        return;
    }
    
    try {
        // Store current HTML before updating to avoid flicker
        const currentContent = servicesContainer.innerHTML;
        
        // Add cache-busting parameter to force a fresh load without caching
        const timestamp = new Date().getTime();
        const url = `/api/session/${sessionId}/services?nocache=${timestamp}`;
        
        // Set the in-flight request
        _inflightRequests.services = url;
        
        // Fetch services data using the utility function with the non-cached URL
        const result = await fetchJSON(url);
        
        // Clear in-flight request now that it's done
        _inflightRequests.services = null;
        
        // Guard against race – if another call was triggered afterwards we
        // ignore this stale result.
        if (!_isLatest('services', token)) {
            logger.debug('[Sidebar] Discarding stale services response');
            return;
        }
        
        // Update the services container
        if (result.services && result.services.length > 0) {
            // Sort services by name (safely)
            const sortedServices = [...result.services].sort((a, b) => {
                const nameA = (a.name || 'Unknown').toString();
                const nameB = (b.name || 'Unknown').toString();
                return nameA.localeCompare(nameB);
            });
            
            let servicesHtml = '';
            
            // Create service tiles with name and description only (no action count)
            for (const service of sortedServices) {
                const serviceName = service.name || 'Unknown';
                const rawDesc = service.description || '';
                const hasCustomDesc = rawDesc && rawDesc.toLowerCase() !== 'no description available';
                
                servicesHtml += `
                    <div class="service-tile sidebar-tile">
                        <div class="service-name">${serviceName}</div>
                        ${hasCustomDesc ? `<div class="service-description">${rawDesc}</div>` : ''}
                    </div>
                `;
            }
            
            // Only update DOM if content has changed
            if (servicesHtml !== currentContent) {
                servicesContainer.innerHTML = servicesHtml;
            }
        } else {
            // Only update if necessary
            if (currentContent !== '<div class="empty-state">No services available</div>') {
                servicesContainer.innerHTML = '<div class="empty-state">No services available</div>';
            }
        }
        
        // Emit event that services are updated
        emit('sidebar:servicesUpdated');
    } catch (error) {
        // Clear in-flight request on error
        _inflightRequests.services = null;
        
        logger.error('Error loading services:', error);
        // Only update with error if not already showing an error
        if (!servicesContainer.innerHTML.includes('error-message')) {
            servicesContainer.innerHTML = `<div class="error-message">Error loading services: ${error.message}</div>`;
        }
    }
}

/**
 * Load the task information
 */
async function loadTask() {
    const sessionId = sessionState.sessionId;
    if (!sessionId) return;
    
    const token = _nextToken('task');
    const taskDisplay = document.getElementById('task-display');
    const currentTaskText = document.getElementById('current-task-text');
    
    if (!taskDisplay) return;

    // Don't start a new request if one is already in progress
    if (_inflightRequests.task) {
        logger.debug('[Sidebar] Task request already in progress, skipping');
        return;
    }
    
    try {
        logger.debug(`[Sidebar] Fetching task data for session ${sessionId}`);
        
        // Store current content to avoid flicker
        const currentContent = taskDisplay.textContent;
        
        // Set the in-flight request
        _inflightRequests.task = true;
        
        fetch('/api/session/' + sessionId + '/task')
            .then(response => {
                logger.debug(`[Sidebar] Direct fetch response:`, response);
                if (!response.ok) {
                    logger.error(`[Sidebar] Task API returned error:`, response.status);
                    throw new Error(`Failed to fetch task: ${response.statusText}`);
                }
                return response.json();
            })
            .then(data => {
                // Clear in-flight request
                _inflightRequests.task = null;
                
                // Guard against race – if another call was triggered afterwards we
                // ignore this stale result.
                if (!_isLatest('task', token)) {
                    logger.debug('[Sidebar] Discarding stale task response');
                    return;
                }
                
                logger.debug(`[Sidebar] Task API response:`, data);
                
                // Handle different response formats and statuses
                if (data && data.status === "ok" && data.task) {
                    // We have a valid initial task
                    logger.debug(`[Sidebar] Task found: "${data.task}"`);
                    
                    // Store the initial task once we retrieve it, to avoid showing dynamic task updates
                    if (!window.initialTaskSet) {
                        // Only update if the content has changed
                        if (taskDisplay.textContent !== data.task) {
                            taskDisplay.textContent = data.task;
                            
                            // Update page title with task
                            document.title = `Session: ${sessionId} - ${data.task.substring(0, 30)}${data.task.length > 30 ? '...' : ''}`;
                            
                            // Mark that we've set the initial task
                            window.initialTaskSet = true;
                            
                            // Store task in localStorage to preserve between page refreshes
                            try {
                                localStorage.setItem(`task_${sessionId}`, data.task);
                            } catch (e) {
                                logger.error('[Sidebar] Error storing task in localStorage:', e);
                            }
                        }
                    } else {
                        logger.debug('[Sidebar] Initial task already set, not updating with new task data');
                    }
                    
                    // Always update the current task text (status) - this is independent of the task description
                    if (currentTaskText) {
                        // For the status, we can use either current_status if available, or status
                        const statusText = data.current_status || data.status || 'Running';
                        if (currentTaskText.textContent !== statusText) {
                            currentTaskText.textContent = statusText;
                        }
                    }
                } 
                else if (data && data.status === "not_found") {
                    // Check if we have a stored task first
                    const storedTask = localStorage.getItem(`task_${sessionId}`);
                    if (storedTask) {
                        logger.debug(`[Sidebar] Using stored task: "${storedTask}"`);
                        taskDisplay.textContent = storedTask;
                        window.initialTaskSet = true;
                    } else {
                        // No task found but response was valid
                        logger.debug(`[Sidebar] No task data available`);
                        if (taskDisplay.textContent !== 'No task specified') {
                            taskDisplay.textContent = 'No task specified';
                            
                            // Update page title with just session ID
                            document.title = `Session: ${sessionId}`;
                        }
                    }
                }
                else if (data && data.task === "") {
                    // Check if we have a stored task first
                    const storedTask = localStorage.getItem(`task_${sessionId}`);
                    if (storedTask) {
                        logger.debug(`[Sidebar] Using stored task: "${storedTask}"`);
                        taskDisplay.textContent = storedTask;
                        window.initialTaskSet = true;
                    } else {
                        // Empty task string
                        logger.debug(`[Sidebar] Empty task string received`);
                        if (taskDisplay.textContent !== 'No task specified') {
                            taskDisplay.textContent = 'No task specified';
                            
                            // Update page title with just session ID
                            document.title = `Session: ${sessionId}`;
                        }
                    }
                }
                else {
                    // Check if we have a stored task first 
                    const storedTask = localStorage.getItem(`task_${sessionId}`);
                    if (storedTask) {
                        logger.debug(`[Sidebar] Using stored task: "${storedTask}"`);
                        taskDisplay.textContent = storedTask;
                        window.initialTaskSet = true;
                    } else {
                        // Some other response format
                        logger.debug(`[Sidebar] Unexpected task response format`);
                        if (data && typeof data === "string" && data.trim().length > 0) {
                            // If data is a non-empty string, use it as the task
                            logger.debug(`[Sidebar] Task found: "${data}"`);
                            if (taskDisplay.textContent !== data) {
                                taskDisplay.textContent = data;
                                window.initialTaskSet = true;
                                
                                // Store task in localStorage
                                try {
                                    localStorage.setItem(`task_${sessionId}`, data);
                                } catch (e) {
                                    logger.error('[Sidebar] Error storing task in localStorage:', e);
                                }
                            }
                        } else {
                            if (taskDisplay.textContent !== 'No task specified') {
                                taskDisplay.textContent = 'No task specified';
                                
                                // Update page title with just session ID
                                document.title = `Session: ${sessionId}`;
                            }
                        }
                    }
                }
                
                // Emit event that task is updated
                emit('sidebar:taskUpdated');
            })
            .catch(error => {
                // Clear in-flight request on error
                _inflightRequests.task = null;
                
                logger.error(`[Sidebar] Error fetching task:`, error);
                // Check if we have a stored task
                const storedTask = localStorage.getItem(`task_${sessionId}`);
                if (storedTask) {
                    logger.debug(`[Sidebar] Using stored task on error: "${storedTask}"`);
                    taskDisplay.textContent = storedTask;
                } else if (taskDisplay.textContent !== 'Error loading task information') {
                    taskDisplay.textContent = 'Error loading task information';
                }
            });
    } catch (error) {
        // Clear in-flight request on error
        _inflightRequests.task = null;
        
        logger.error('[Sidebar] Error loading task:', error);
        // Check for stored task on error
        const storedTask = localStorage.getItem(`task_${sessionId}`);
        if (storedTask) {
            logger.debug(`[Sidebar] Using stored task on error: "${storedTask}"`);
            taskDisplay.textContent = storedTask;
        } else if (taskDisplay.textContent !== 'Error loading task information') {
            taskDisplay.textContent = 'Error loading task information';
        }
    }
}

/**
 * Load skills information for the session
 */
async function loadSkills() {
    const sessionId = sessionState.sessionId;
    if (!sessionId) return;
    
    const token = _nextToken('skills');
    const skillsContainer = document.getElementById('skills-container');
    
    if (!skillsContainer) return;
    
    // Don't start a new request if one is already in progress
    if (_inflightRequests.skills) {
        logger.debug('[Sidebar] Skills request already in progress, skipping');
        return;
    }
    
    try {
        // Store current HTML before updating to avoid flicker
        const currentContent = skillsContainer.innerHTML;
        
        // Add cache-busting parameter to force a fresh load without caching
        const timestamp = new Date().getTime();
        const url = `/api/session/${sessionId}/skills?nocache=${timestamp}`;
        
        // Set the in-flight request
        _inflightRequests.skills = url;
        
        // Fetch skills data
        const result = await fetchJSON(url);
        
        // Clear in-flight request now that it's done
        _inflightRequests.skills = null;
        
        // Guard against race
        if (!_isLatest('skills', token)) {
            logger.debug('[Sidebar] Discarding stale skills response');
            return;
        }
        
        logger.debug('[Sidebar] Skills data:', result);
        
        // Update the skills container
        if (result.skills && result.skills.length > 0) {
            const skillsHtml = result.skills.map(skill => {
                const skillId = skill.id || skill.skill_id || 'unknown';
                const skillName = skill.name || skillId;
                const isUserSkill = skill.type === 'user' || skill.is_user_skill;
                
                return `
                    <div class="service-tile sidebar-tile">
                        <div class="service-name">
                            ${skillName}
                            ${isUserSkill ? '<span class="skill-badge custom">Custom</span>' : ''}
                        </div>
                    </div>
                `;
            }).join('');
            
            // Only update DOM if content has changed
            if (skillsHtml !== currentContent) {
                skillsContainer.innerHTML = skillsHtml;
            }
        } else {
            // Only update if necessary
            if (currentContent !== '<div class="empty-state compact">No skills loaded</div>') {
                skillsContainer.innerHTML = '<div class="empty-state compact">No skills loaded</div>';
            }
        }
        
        // Emit event that skills are updated
        emit('sidebar:skillsUpdated');
    } catch (error) {
        // Clear in-flight request on error
        _inflightRequests.skills = null;
        
        // Skills endpoint may not exist yet, just show empty state
        logger.debug('[Sidebar] Skills API not available:', error.message);
        if (!skillsContainer.innerHTML.includes('No skills loaded')) {
            skillsContainer.innerHTML = '<div class="empty-state compact">No skills loaded</div>';
        }
    }
}

// Export functions for use in other modules
export { refreshSidebar, updateSessionInfo, loadTask, loadAgents, loadServices, loadSkills };