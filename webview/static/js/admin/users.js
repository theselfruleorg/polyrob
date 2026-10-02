/**
 * Admin Users List JavaScript
 * Handles user search, filtering, and pagination
 */

(function() {
    'use strict';

    // Use shared utilities
    const { apiCall, formatWallet, formatDate, formatCredits, h, messageRow } = AdminUtils;

    // State
    let currentPage = 0;
    let pageSize = 50;
    let totalUsers = 0;
    let currentFilters = {
        q: '',
        field: 'all',
        tier: '',
        role: ''
    };

    // Load users
    async function loadUsers() {
        const tbody = document.getElementById('users-tbody');
        tbody.innerHTML = '<tr><td colspan="7" class="loading">Loading users</td></tr>';

        try {
            let endpoint;
            const offset = currentPage * pageSize;

            if (currentFilters.q) {
                // Use search endpoint
                const params = new URLSearchParams({
                    q: currentFilters.q,
                    field: currentFilters.field,
                    limit: pageSize,
                    offset: offset
                });
                endpoint = `/admin/users/search?${params}`;
            } else {
                // Use list endpoint with filters
                const params = new URLSearchParams({
                    limit: pageSize,
                    offset: offset
                });
                if (currentFilters.tier) params.append('tier', currentFilters.tier);
                if (currentFilters.role) params.append('role', currentFilters.role);
                endpoint = `/admin/users?${params}`;
            }

            const users = await apiCall(endpoint);

            if (!users || users.length === 0) {
                tbody.innerHTML = '<tr><td colspan="7" class="empty-state">No users found</td></tr>';
                updatePagination(0);
                return;
            }

            // FE16: server fields are written as text, never parsed as HTML.
            const rows = users.map((user) => {
                const blockedClass = user.is_blocked ? 'blocked-indicator' : '';
                return h('tr', null,
                    h('td', { class: `wallet ${blockedClass}`,
                              text: `${user.is_blocked ? '[B] ' : ''}${formatWallet(user.wallet_address)}` }),
                    h('td', null, h('span', { class: `tier tier-${user.tier}`, text: user.tier })),
                    h('td', null, h('span', { class: `role role-${user.role}`, text: user.role })),
                    h('td', { class: 'credits', text: formatCredits(user.balance) }),
                    h('td', null, h('span', { class: 'token-count',
                        text: `${user.den_token_count > 0 ? '\u2714' : '\u2718'} ${user.den_token_count || 0}` })),
                    h('td', { text: formatDate(user.created_at) }),
                    h('td', { class: 'actions' },
                        h('a', { class: 'btn-view', href: `/admin/users/${encodeURIComponent(user.user_id)}`, text: 'View' })));
            });
            tbody.replaceChildren(...rows);

            // Update pagination (estimate total based on results)
            totalUsers = users.length < pageSize ? (currentPage * pageSize + users.length) : ((currentPage + 2) * pageSize);
            updatePagination(users.length);

        } catch (error) {
            console.error('Failed to load users:', error);
            messageRow(tbody, 7, 'empty-state', `Error: ${error.message}`);
        }
    }

    // Update pagination
    function updatePagination(resultsCount) {
        const startNum = currentPage * pageSize + 1;
        const endNum = currentPage * pageSize + resultsCount;

        document.getElementById('pagination-info').textContent =
            `Showing ${startNum}-${endNum} users`;

        const btnPrev = document.getElementById('btn-prev');
        const btnNext = document.getElementById('btn-next');

        btnPrev.disabled = currentPage === 0;
        btnNext.disabled = resultsCount < pageSize;
    }

    // Setup event listeners
    function setupEventListeners() {
        // Search button
        document.getElementById('btn-search').addEventListener('click', () => {
            currentFilters.q = document.getElementById('search-input').value.trim();
            currentFilters.field = document.getElementById('search-field').value;
            currentFilters.tier = document.getElementById('filter-tier').value;
            currentFilters.role = document.getElementById('filter-role').value;
            currentPage = 0;
            loadUsers();
        });

        // Search on Enter
        document.getElementById('search-input').addEventListener('keypress', (e) => {
            if (e.key === 'Enter') {
                document.getElementById('btn-search').click();
            }
        });

        // Filter changes
        ['filter-tier', 'filter-role'].forEach(id => {
            document.getElementById(id).addEventListener('change', () => {
                currentFilters.tier = document.getElementById('filter-tier').value;
                currentFilters.role = document.getElementById('filter-role').value;
                currentPage = 0;
                loadUsers();
            });
        });

        // Pagination
        document.getElementById('btn-prev').addEventListener('click', () => {
            if (currentPage > 0) {
                currentPage--;
                loadUsers();
            }
        });

        document.getElementById('btn-next').addEventListener('click', () => {
            currentPage++;
            loadUsers();
        });
    }

    // Check for URL params on load
    function checkUrlParams() {
        const params = new URLSearchParams(window.location.search);
        if (params.has('q')) {
            const query = params.get('q');
            document.getElementById('search-input').value = query;
            currentFilters.q = query;
        }
    }

    // Initialize
    document.addEventListener('DOMContentLoaded', () => {
        checkUrlParams();
        setupEventListeners();
        loadUsers();
    });

})();
