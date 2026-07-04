/**
 * Admin Activity Log JavaScript
 * Handles activity/audit log filtering and pagination
 */

(function() {
    'use strict';

    // Use shared utilities
    const { apiCall, formatDateTime, truncateText, formatId } = AdminUtils;

    // State
    let currentPage = 0;
    let pageSize = 50;
    let currentFilters = {
        event_type: '',
        actor_id: '',
        target_id: ''
    };

    // Alias for consistency with existing code
    const formatTimestamp = formatDateTime;
    const truncate = truncateText;

    // Load activity events
    async function loadActivity() {
        const tbody = document.getElementById('activity-tbody');
        tbody.innerHTML = '<tr><td colspan="7" class="loading">Loading activity</td></tr>';

        try {
            const params = new URLSearchParams({
                limit: pageSize,
                offset: currentPage * pageSize
            });

            if (currentFilters.event_type) {
                params.append('event_type', currentFilters.event_type);
            }

            if (currentFilters.actor_id) {
                params.append('actor_id', currentFilters.actor_id);
            }

            if (currentFilters.target_id) {
                params.append('target_id', currentFilters.target_id);
            }

            const events = await apiCall(`/admin/activity?${params}`);

            if (!events || events.length === 0) {
                tbody.innerHTML = '<tr><td colspan="7" class="empty-state">No activity found</td></tr>';
                updatePagination(0);
                return;
            }

            let html = '';
            for (const event of events) {
                const successClass = event.success ? 'success' : 'failure';
                html += `
                    <tr>
                        <td class="timestamp">${formatTimestamp(event.timestamp)}</td>
                        <td>
                            <span class="event-type ${event.event_type}">${event.event_type.replace('_', ' ')}</span>
                        </td>
                        <td class="actor" title="${event.actor_id || ''}">${formatId(event.actor_id)}</td>
                        <td class="target" title="${event.target_id || ''}">${formatId(event.target_id)}</td>
                        <td class="action" title="${event.action || ''}">${truncate(event.action, 40)}</td>
                        <td class="ip">${event.actor_ip || '-'}</td>
                        <td>
                            <span class="success-indicator ${successClass}" title="${event.success ? 'Success' : 'Failed'}"></span>
                        </td>
                    </tr>
                `;
            }
            tbody.innerHTML = html;

            updatePagination(events.length);
            updateStats(events);

        } catch (error) {
            console.error('Failed to load activity:', error);
            tbody.innerHTML = `<tr><td colspan="7" class="empty-state">Error: ${error.message}</td></tr>`;
        }
    }

    // Update pagination
    function updatePagination(resultsCount) {
        const startNum = currentPage * pageSize + 1;
        const endNum = currentPage * pageSize + resultsCount;

        document.getElementById('pagination-info').textContent =
            `Showing ${resultsCount > 0 ? startNum : 0}-${endNum} events`;

        const btnPrev = document.getElementById('btn-prev');
        const btnNext = document.getElementById('btn-next');

        btnPrev.disabled = currentPage === 0;
        btnNext.disabled = resultsCount < pageSize;
    }

    // Update stats based on loaded events
    function updateStats(events) {
        // Count event types from the loaded data
        const counts = {
            total: events.length,
            auth_success: 0,
            auth_failure: 0,
            tier_change: 0,
            credit_ops: 0
        };

        for (const event of events) {
            if (event.event_type === 'auth_success') counts.auth_success++;
            if (event.event_type === 'auth_failure') counts.auth_failure++;
            if (event.event_type === 'tier_change') counts.tier_change++;
            if (event.event_type === 'credit_add' || event.event_type === 'credit_deduct') counts.credit_ops++;
        }

        document.getElementById('stat-total-events').textContent = counts.total;
        document.getElementById('stat-auth-success').textContent = counts.auth_success;
        document.getElementById('stat-auth-failure').textContent = counts.auth_failure;
        document.getElementById('stat-tier-changes').textContent = counts.tier_change;
        document.getElementById('stat-credit-ops').textContent = counts.credit_ops;
    }

    // Setup event listeners
    function setupEventListeners() {
        // Apply filter button
        document.getElementById('btn-apply-filter').addEventListener('click', () => {
            currentFilters.event_type = document.getElementById('filter-event-type').value;
            currentFilters.actor_id = document.getElementById('filter-actor').value.trim();
            currentFilters.target_id = document.getElementById('filter-target').value.trim();
            currentPage = 0;
            loadActivity();
        });

        // Clear filter button
        document.getElementById('btn-clear-filter').addEventListener('click', () => {
            document.getElementById('filter-event-type').value = '';
            document.getElementById('filter-actor').value = '';
            document.getElementById('filter-target').value = '';
            currentFilters = { event_type: '', actor_id: '', target_id: '' };
            currentPage = 0;
            loadActivity();
        });

        // Event type dropdown change
        document.getElementById('filter-event-type').addEventListener('change', () => {
            currentFilters.event_type = document.getElementById('filter-event-type').value;
            currentPage = 0;
            loadActivity();
        });

        // Pagination
        document.getElementById('btn-prev').addEventListener('click', () => {
            if (currentPage > 0) {
                currentPage--;
                loadActivity();
            }
        });

        document.getElementById('btn-next').addEventListener('click', () => {
            currentPage++;
            loadActivity();
        });

        // Enter key on filter inputs
        ['filter-actor', 'filter-target'].forEach(id => {
            document.getElementById(id).addEventListener('keypress', (e) => {
                if (e.key === 'Enter') {
                    document.getElementById('btn-apply-filter').click();
                }
            });
        });
    }

    // Initialize
    document.addEventListener('DOMContentLoaded', () => {
        setupEventListeners();
        loadActivity();

        // Auto-refresh every 60 seconds
        setInterval(loadActivity, 60000);
    });

})();
