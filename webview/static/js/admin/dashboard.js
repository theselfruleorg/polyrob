/**
 * Admin Dashboard JavaScript
 * Handles loading dashboard stats, activity feed, and quick search
 */

(function() {
    'use strict';

    // Use shared utilities
    const { apiCall, formatNumber, formatCurrency, formatTime, truncateText, h } = AdminUtils;

    // Load dashboard stats
    async function loadDashboardStats() {
        try {
            const stats = await apiCall('/admin/stats/dashboard');

            // Update user stats
            document.getElementById('stat-total-users').textContent = formatNumber(stats.users.total);
            document.getElementById('stat-new-today').textContent = `${formatNumber(stats.users.new_today)} new today`;

            // Update credits stats
            document.getElementById('stat-total-credits').textContent = formatNumber(stats.credits.total_balance);
            document.getElementById('stat-credits-spent').textContent = `${formatNumber(stats.credits.total_spent)} spent`;

            // Update total revenue stats
            document.getElementById('stat-total-revenue').textContent = formatCurrency(stats.revenue.total_usd);
            document.getElementById('stat-total-breakdown').textContent =
                `x402: ${formatCurrency(stats.revenue.x402_total_usd)} | crypto: ${formatCurrency(stats.revenue.crypto_total_usd)}`;

            // Update MTD revenue stats
            document.getElementById('stat-mtd-revenue').textContent = formatCurrency(stats.revenue.mtd_usd);
            document.getElementById('stat-mtd-breakdown').textContent =
                `x402: ${formatCurrency(stats.revenue.x402_mtd_usd)} | crypto: ${formatCurrency(stats.revenue.crypto_mtd_usd)}`;

            // Render tier breakdown
            renderBreakdown('tier-breakdown', stats.users.by_tier, stats.users.total, 'tier');

            // Render role breakdown
            renderBreakdown('role-breakdown', stats.users.by_role, stats.users.total, 'role');

            // Render alerts
            renderAlerts(stats.alerts);

        } catch (error) {
            console.error('Failed to load dashboard stats:', error);
            document.getElementById('tier-breakdown').innerHTML = '<div class="empty-state">Failed to load stats</div>';
            document.getElementById('role-breakdown').innerHTML = '<div class="empty-state">Failed to load stats</div>';
        }
    }

    // Render breakdown bars
    function renderBreakdown(containerId, data, total, type) {
        const container = document.getElementById(containerId);
        if (!data || Object.keys(data).length === 0) {
            container.innerHTML = '<div class="empty-state">No data</div>';
            return;
        }

        // FE16: server keys are written as text, never parsed as HTML.
        const items = Object.entries(data).map(([key, count]) => {
            const percentage = total > 0 ? (Number(count) / total) * 100 : 0;
            const fill = h('div', { class: `breakdown-bar-fill ${type}-${key}` });
            fill.style.width = `${Number.isFinite(percentage) ? percentage : 0}%`;
            return h('div', { class: 'breakdown-item' },
                h('span', { class: 'breakdown-label', text: key }),
                h('div', { class: 'breakdown-bar' }, fill),
                h('span', { class: 'breakdown-count', text: formatNumber(count) }));
        });
        container.replaceChildren(...items);
    }

    // Render alerts
    function renderAlerts(alerts) {
        const container = document.getElementById('alerts-list');

        if (!alerts || alerts.length === 0) {
            container.innerHTML = '<div class="no-alerts">All systems healthy - no alerts</div>';
            return;
        }

        const items = alerts.map((alert) => {
            const icon = alert.severity === 'high' ? '!' :
                        alert.severity === 'warning' ? '?' : 'i';
            return h('div', { class: `alert-item severity-${alert.severity}` },
                h('span', { class: 'alert-icon', text: icon }),
                h('span', { class: 'alert-message', text: alert.message }),
                h('span', { class: 'alert-count', text: alert.count }));
        });
        container.replaceChildren(...items);
    }

    // Load activity feed
    async function loadActivityFeed() {
        try {
            const events = await apiCall('/admin/activity?limit=20');

            const container = document.getElementById('activity-feed');

            if (!events || events.length === 0) {
                container.innerHTML = '<div class="empty-state">No recent activity</div>';
                return;
            }

            const items = events.map((event) => {
                const type = String(event.event_type || '');
                return h('div', { class: 'activity-item' },
                    h('span', { class: 'activity-time', text: formatTime(event.timestamp) }),
                    h('span', { class: `activity-type ${type}`, text: type.replace('_', ' ') }),
                    h('span', { class: 'activity-action', text: truncateText(event.action, 50) }));
            });
            container.replaceChildren(...items);

        } catch (error) {
            console.error('Failed to load activity feed:', error);
            document.getElementById('activity-feed').innerHTML = '<div class="empty-state">Failed to load activity</div>';
        }
    }

    // Quick search handler
    function setupQuickSearch() {
        const input = document.getElementById('quick-search-input');
        const btn = document.getElementById('quick-search-btn');

        const doSearch = () => {
            const query = input.value.trim();
            if (query) {
                window.location.href = `/admin/users?q=${encodeURIComponent(query)}`;
            }
        };

        btn.addEventListener('click', doSearch);
        input.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') {
                doSearch();
            }
        });
    }

    // Initialize on page load
    document.addEventListener('DOMContentLoaded', () => {
        loadDashboardStats();
        loadActivityFeed();
        setupQuickSearch();

        // Refresh every 30 seconds
        setInterval(() => {
            loadDashboardStats();
            loadActivityFeed();
        }, 30000);
    });

})();
