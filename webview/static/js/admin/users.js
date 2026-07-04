/**
 * Admin Users List JavaScript
 * Handles user search, filtering, and pagination
 */

(function() {
    'use strict';

    // Use shared utilities
    const { apiCall, formatWallet, formatDate, formatCredits } = AdminUtils;

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

            let html = '';
            for (const user of users) {
                const blockedClass = user.is_blocked ? 'blocked-indicator' : '';
                html += `
                    <tr>
                        <td class="wallet ${blockedClass}">
                            ${user.is_blocked ? '[B] ' : ''}${formatWallet(user.wallet_address)}
                        </td>
                        <td>
                            <span class="tier tier-${user.tier}">${user.tier}</span>
                        </td>
                        <td>
                            <span class="role role-${user.role}">${user.role}</span>
                        </td>
                        <td class="credits">${formatCredits(user.balance)}</td>
                        <td>
                            <span class="token-count">
                                ${user.den_token_count > 0 ? '&#10004;' : '&#10008;'} ${user.den_token_count || 0}
                            </span>
                        </td>
                        <td>${formatDate(user.created_at)}</td>
                        <td class="actions">
                            <a href="/admin/users/${user.user_id}" class="btn-view">View</a>
                        </td>
                    </tr>
                `;
            }
            tbody.innerHTML = html;

            // Update pagination (estimate total based on results)
            totalUsers = users.length < pageSize ? (currentPage * pageSize + users.length) : ((currentPage + 2) * pageSize);
            updatePagination(users.length);

        } catch (error) {
            console.error('Failed to load users:', error);
            tbody.innerHTML = `<tr><td colspan="7" class="empty-state">Error: ${error.message}</td></tr>`;
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
