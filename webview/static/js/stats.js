/* Stats view for session statistics */
import { fetchJSON, formatCost, on, emit, logger, sessionState } from '/static/js/ui-utils.js?v=2';

// Stats refresh state
let statsRefreshTimer = null;
let isStatsTabActive = false;
let isRefreshing = false;
const STATS_REFRESH_INTERVAL = 10000; // 10 seconds
const STATS_DEBOUNCE_TIME = 2000; // 2 second minimum between refreshes
let lastRefreshTime = 0;

document.addEventListener('DOMContentLoaded', function() {
    // Initialize stats tab functionality
    initStats();
    
    // Listen for tab activation events
    on('tab:activated', (event) => {
        const tabId = event.detail.tabId;
        
        if (tabId === 'stats') {
            // Stats tab was activated
            isStatsTabActive = true;
            // Load stats immediately if it's been a while
            const now = Date.now();
            if (now - lastRefreshTime > STATS_DEBOUNCE_TIME) {
                loadStats();
            }
            // Start auto-refresh only when tab is active
            startStatsRefresh();
        } else {
            // Stats tab was deactivated, suspend auto-refresh
            isStatsTabActive = false;
            stopStatsRefresh();
        }
    });
    
    // Listen for stats refresh requests from other modules
    on('stats:refreshRequest', () => {
        // Only refresh if debounce time has passed or we're forced
        const now = Date.now();
        if (now - lastRefreshTime > STATS_DEBOUNCE_TIME && !isRefreshing) {
            loadStats();
        }
    });
});

async function initStats() {
    // No specific initialization needed besides setting up event listeners
    logger.debug('[Stats] Stats tab initialization complete');
}

/**
 * Start the stats auto-refresh timer if not already running
 */
function startStatsRefresh() {
    // Clear any existing timer first
    stopStatsRefresh();
    
    // Only start if the stats tab is currently active
    if (isStatsTabActive) {
        statsRefreshTimer = setInterval(() => {
            // Only refresh if not already refreshing and page is visible
            if (!isRefreshing && document.visibilityState === 'visible') {
                loadStats();
            }
        }, STATS_REFRESH_INTERVAL);
        logger.debug('[Stats] Stats auto-refresh started');
    }
}

/**
 * Stop the stats auto-refresh timer
 */
function stopStatsRefresh() {
    if (statsRefreshTimer) {
        clearInterval(statsRefreshTimer);
        statsRefreshTimer = null;
        logger.debug('[Stats] Stats auto-refresh stopped');
    }
}

/**
 * Load statistics for the current session and update the UI
 */
async function loadStats() {
    const sessionId = sessionState.sessionId;
    
    // Don't refresh stats if tab is not active
    if (!isStatsTabActive && document.querySelector('.tab-button[data-tab="stats"]')) {
        logger.debug('[Stats] Stats refresh skipped - tab not active');
        return;
    }
    
    // Don't try to load stats for new sessions (empty state)
    if (!sessionId || sessionState.isNew) {
        logger.debug('[Stats] Skipping stats load - no active session yet');
        return;
    }

    // Prevent overlapping requests
    if (isRefreshing) {
        logger.debug('[Stats] Stats refresh already in progress');
        return;
    }

    isRefreshing = true;
    lastRefreshTime = Date.now();

    logger.debug(`[Stats] Loading stats for session ${sessionId}`);
    
    try {
        // Add cache-busting parameter to avoid stale data
        const timestamp = Date.now();
        const result = await fetchJSON(`/api/session/${sessionId}/stats?_=${timestamp}`);
        
        logger.debug('[Stats] Stats loaded:', result);
        
        if (result && result.status === 'ok' && result.data) {
            const stats = result.data;
            
            // Enhanced debug logging for token/cost values
            logger.debug('[Stats] Token count:', stats.total_tokens);
            logger.debug('[Stats] Cost USD:', stats.cost_usd);
            logger.debug('[Stats] LLM calls:', stats.llm_calls);
            logger.debug('[Stats] Models used:', stats.models_used);
            
            // Ensure data is valid before updating UI
            // Set defaults for missing values
            if (stats.total_tokens === undefined) stats.total_tokens = 0;
            if (stats.cost_usd === undefined) stats.cost_usd = 0;
            if (stats.llm_calls === undefined) stats.llm_calls = 0;
            if (stats.actions === undefined) stats.actions = 0;
            if (stats.feed_entries === undefined) stats.feed_entries = 0;
            if (!stats.models_used) stats.models_used = [];
            if (!stats.top_services) stats.top_services = [];
            if (!stats.top_actions) stats.top_actions = [];
            
            // Update the detailed stats tab (always render, handles empty state)
            updateDetailedStatsTab(stats);
            
            // Emit event that stats were successfully updated
            emit('stats:updated', { stats });
        } else {
            logger.warn('[Stats] Invalid stats response:', result);
            // Show empty state on invalid response
            showEmptyState();
        }
    } catch (error) {
        logger.error('[Stats] Error loading stats:', error);
        // Don't clear stats on error to avoid flickering
    } finally {
        isRefreshing = false;
    }
}

/**
 * Show empty state when no stats available
 */
function showEmptyState() {
    const statsContent = document.getElementById('stats-content');
    if (!statsContent) return;
    
    // Hide loading indicator
    const statsLoading = document.getElementById('stats-loading');
    if (statsLoading) statsLoading.style.display = 'none';
    
    statsContent.innerHTML = `
        <div class="stats-container">
            <div class="stats-box">
                <div class="empty-state">
                    <p>No statistics available yet.</p>
                    <p>Stats will appear once the session starts processing.</p>
                </div>
            </div>
        </div>
    `;
    statsContent.style.display = 'block';
}

/**
 * Update the detailed stats tab with model information
 * @param {Object} stats - Statistics object from the API
 */
function updateDetailedStatsTab(stats) {
    const statsContent = document.getElementById('stats-content');
    if (!statsContent) return;
    
    // Hide loading indicator
    const statsLoading = document.getElementById('stats-loading');
    if (statsLoading) statsLoading.style.display = 'none';
    
    // Create detailed stats HTML
    const userCost = stats.cost_usd || 0;
    const credits = stats.cost_breakdown ? stats.cost_breakdown.credits_estimated : Math.ceil(userCost / 0.01);

    let html = `
        <div class="stats-container">
            <div class="stats-box">
                <h3>Cost Summary</h3>
                <div class="stats-item">
                    <div class="stats-label">💰 Total Cost:</div>
                    <div class="stats-value highlight">${formatCost(userCost)}</div>
                </div>
                <div class="stats-item">
                    <div class="stats-label">Credits Used:</div>
                    <div class="stats-value">${credits.toLocaleString()}</div>
                </div>
                ${stats.cost_breakdown ? `
                <div class="stats-item">
                    <div class="stats-label">Provider (API) Cost:</div>
                    <div class="stats-value">${formatCost(stats.cost_breakdown.api_cost_usd || stats.api_cost_usd || 0)}</div>
                </div>
                <div class="stats-item">
                    <div class="stats-label">Markup:</div>
                    <div class="stats-value">${formatCost(stats.cost_breakdown.markup_usd || 0)}</div>
                </div>` : ''}

                <div class="stats-divider"></div>

                <div class="stats-item">
                    <div class="stats-label">Total LLM Calls:</div>
                    <div class="stats-value">${(stats.llm_calls || 0).toLocaleString()}</div>
                </div>
                <div class="stats-item">
                    <div class="stats-label">Total Tokens:</div>
                    <div class="stats-value">${(stats.total_tokens || 0).toLocaleString()}</div>
                </div>
                <div class="stats-item">
                    <div class="stats-label">Actions Performed:</div>
                    <div class="stats-value">${(stats.actions || 0).toLocaleString()}</div>
                </div>
            </div>
            
            <div class="stats-box">
                <h3>Per-Model Breakdown</h3>
                ${stats.models_used && stats.models_used.length > 0 ? stats.models_used.map(model => `
                    <div class="model-item">
                        <div class="model-header">
                            <span class="model-name">${model.name}</span>
                            <span class="model-count">${model.count} calls</span>
                        </div>
                        <div class="model-details">
                            <div class="model-stat">
                                <span class="label">Tokens:</span>
                                <span class="value">${model.tokens ? model.tokens.toLocaleString() : '0'}</span>
                            </div>
                            <div class="model-stat">
                                <span class="label">Cost:</span>
                                <span class="value highlight">${formatCost(model.cost)}</span>
                            </div>
                            ${model.api_cost ? `
                            <div class="model-stat">
                                <span class="label">API cost:</span>
                                <span class="value">${formatCost(model.api_cost)}</span>
                            </div>` : ''}
                        </div>
                    </div>
                `).join('') : '<div class="empty-state">No model usage data available</div>'}
            </div>

            <div class="stats-box">
                <h3>Tools Summary</h3>
                <div class="chart-container">
                    ${stats.top_services && stats.top_services.length > 0 ? stats.top_services.map(tool => {
                        // Calculate percentage for visual bar
                        const totalTools = stats.top_services.reduce((sum, t) => sum + t.count, 0);
                        const percentage = Math.min(100, Math.max(5, (tool.count / totalTools) * 100));
                        
                        return `
                            <div class="chart-item">
                                <div class="chart-label">${tool.name}</div>
                                <div class="chart-bar-container">
                                    <div class="chart-bar" style="width: ${percentage}%"></div>
                                    <div class="chart-value">${tool.count}</div>
                                </div>
                            </div>
                        `;
                    }).join('') : '<div class="empty-state">No tool usage data available</div>'}
                </div>
            </div>
                
            <div class="stats-box">
                <h3>Actions Breakdown</h3>
                <div class="actions-grid">
                    ${stats.detailed_actions ? 
                        `<table class="actions-table">
                            <thead>
                                <tr>
                                    <th>Action</th>
                                    <th>Count</th>
                                    <th>Percentage</th>
                                </tr>
                            </thead>
                            <tbody>
                                ${Object.entries(stats.detailed_actions).sort((a, b) => b[1] - a[1]).map(([action, count]) => {
                                    const percentage = Math.round((count / (stats.actions || 1)) * 100);
                                    return `
                                        <tr>
                                            <td>${action}</td>
                                            <td>${count}</td>
                                            <td>
                                                <div class="mini-bar-container">
                                                    <div class="mini-bar" style="width: ${percentage}%"></div>
                                                    <span class="mini-bar-text">${percentage}%</span>
                                                </div>
                                            </td>
                                        </tr>
                                    `;
                                }).join('')}
                            </tbody>
                        </table>` 
                    : 
                        (stats.top_actions && stats.top_actions.length > 0 ? 
                            `<table class="actions-table">
                                <thead>
                                    <tr>
                                        <th>Action</th>
                                        <th>Count</th>
                                        <th>Percentage</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    ${stats.top_actions.map(action => {
                                        const percentage = Math.round((action.count / (stats.actions || 1)) * 100);
                                        return `
                                            <tr>
                                                <td>${action.name}</td>
                                                <td>${action.count}</td>
                                                <td>
                                                    <div class="mini-bar-container">
                                                        <div class="mini-bar" style="width: ${percentage}%"></div>
                                                        <span class="mini-bar-text">${percentage}%</span>
                                                    </div>
                                                </td>
                                            </tr>
                                        `;
                                    }).join('')}
                                </tbody>
                            </table>`
                        : 
                            '<div class="empty-state">No action data available</div>'
                        )
                    }
                </div>
            </div>
        </div>
    `;
    
    // Update the content and show it
    statsContent.innerHTML = html;
    statsContent.style.display = 'block';
}

// Export for use in other modules
export { loadStats, startStatsRefresh, stopStatsRefresh }; 