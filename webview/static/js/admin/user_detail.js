/**
 * Admin User Detail JavaScript
 * Handles loading user details, credits, and blocking
 */

(function() {
    'use strict';

    // Use shared utilities
    const { apiCall, formatDateTime, formatCredits, timeAgo, showAlert, showConfirm, h } = AdminUtils;

    // 043 phase 5 (R6): de-inlined — the value crosses on the DOM node's
    // data-user-id attribute (was window.TARGET_USER_ID from an inline script,
    // which the console CSP now forbids).
    const userIdNode = document.getElementById('user-id-display');
    const userId = (userIdNode && userIdNode.dataset.userId)
        || (window.TARGET_USER_ID || '');
    let userData = null;

    // Format wallet with more characters for detail view
    function formatWallet(address) {
        if (!address) return '-';
        return `${address.slice(0, 10)}...${address.slice(-8)}`;
    }

    // Load user details
    async function loadUserDetails() {
        try {
            const user = await apiCall(`/admin/users/${userId}`);
            userData = user;

            // Update header
            document.getElementById('user-wallet').textContent = formatWallet(user.wallet_address);
            document.getElementById('user-id-display').textContent = `User ID: ${user.user_id}`;

            // Update badges
            // FE16: server fields are written as text, never parsed as HTML.
            document.getElementById('user-badges').replaceChildren(
                h('span', { class: `badge badge-tier tier-${user.tier}`, text: user.tier }),
                ' ',
                h('span', { class: `badge badge-role role-${user.role}`, text: user.role }));

            // Update identity info
            document.getElementById('info-email').textContent = user.email || '-';
            document.getElementById('info-chain').textContent = user.current_wallet_chain || 'ethereum';
            document.getElementById('info-created').textContent = formatDateTime(user.created_at);

            // Update role/tier dropdowns
            document.getElementById('select-role').value = user.role;
            document.getElementById('select-tier').value = user.tier;

        } catch (error) {
            console.error('Failed to load user:', error);
            alert('Failed to load user: ' + error.message);
        }
    }

    // Load user credits
    async function loadCredits() {
        try {
            const credits = await apiCall(`/admin/users/${userId}/credits`);

            document.getElementById('info-balance').textContent = formatCredits(credits.balance);
            document.getElementById('info-lifetime-earned').textContent = formatCredits(credits.lifetime_earned);
            document.getElementById('info-lifetime-spent').textContent = formatCredits(credits.lifetime_spent);

            // Render transactions
            const tbody = document.getElementById('transactions-tbody');
            if (!credits.recent_transactions || credits.recent_transactions.length === 0) {
                tbody.innerHTML = '<tr><td colspan="4" class="empty-state">No transactions</td></tr>';
                return;
            }

            const rows = credits.recent_transactions.slice(0, 10).map((tx) => {
                const amountClass = tx.amount > 0 ? 'amount-positive' : 'amount-negative';
                const amountPrefix = tx.amount > 0 ? '+' : '';
                return h('tr', null,
                    h('td', { text: formatDateTime(tx.timestamp) }),
                    h('td', { class: amountClass, text: `${amountPrefix}${formatCredits(tx.amount)}` }),
                    h('td', { text: tx.transaction_type }),
                    h('td', { text: tx.reason || '-' }));
            });
            tbody.replaceChildren(...rows);

        } catch (error) {
            console.error('Failed to load credits:', error);
        }
    }

    // Load block status
    async function loadBlockStatus() {
        try {
            const status = await apiCall(`/admin/users/${userId}/block-status`);

            const container = document.getElementById('block-status');
            const btn = document.getElementById('btn-block');

            if (status.is_blocked) {
                container.classList.add('blocked');
                container.replaceChildren(
                    h('span', { style: 'color: var(--accent-red);', text: 'BLOCKED' }),
                    h('div', { class: 'block-reason', text: `Reason: ${status.blocked_reason || 'Not specified'}` }),
                    h('div', { class: 'block-reason', text: `Blocked at: ${formatDateTime(status.blocked_at)}` }),
                    h('div', { class: 'block-reason', text: `By: ${status.blocked_by || 'Unknown'}` }));
                btn.textContent = 'Unblock User';
                btn.classList.remove('btn-danger');
                btn.classList.add('btn-success');

                // Add blocked badge
                const badges = document.getElementById('user-badges');
                if (!badges.querySelector('.badge-blocked')) {
                    badges.appendChild(h('span', { class: 'badge badge-blocked', text: 'BLOCKED' }));
                }
            } else {
                container.classList.remove('blocked');
                container.innerHTML = '<span style="color: var(--accent-green);">Not blocked</span>';
                btn.textContent = 'Block User';
                btn.classList.remove('btn-success');
                btn.classList.add('btn-danger');
            }

        } catch (error) {
            console.error('Failed to load block status:', error);
        }
    }

    // Load sessions summary
    async function loadSessions() {
        try {
            const sessions = await apiCall(`/admin/users/${userId}/sessions?limit=10`);

            document.getElementById('sessions-total').textContent = formatCredits(sessions.total_sessions);
            document.getElementById('sessions-spent').textContent = formatCredits(sessions.total_spent);
            document.getElementById('info-total-sessions').textContent = formatCredits(sessions.total_sessions);

        } catch (error) {
            console.error('Failed to load sessions:', error);
        }
    }

    // Load audit trail
    async function loadAuditTrail() {
        try {
            const events = await apiCall(`/admin/users/${userId}/audit?limit=20`);

            const container = document.getElementById('audit-list');

            if (!events || events.length === 0) {
                container.innerHTML = '<div class="empty-state">No audit events</div>';
                return;
            }

            const items = events.map((event) => {
                const type = String(event.event_type || '');
                return h('div', { class: 'audit-item' },
                    h('span', { class: 'audit-time', text: formatDateTime(event.timestamp) }),
                    h('span', { class: `audit-type ${type}`, text: type.replace('_', ' ') }),
                    h('span', { class: 'audit-action', text: event.action }));
            });
            container.replaceChildren(...items);

        } catch (error) {
            console.error('Failed to load audit trail:', error);
            document.getElementById('audit-list').innerHTML = '<div class="empty-state">Failed to load audit trail</div>';
        }
    }

    // Save role
    async function saveRole() {
        const newRole = document.getElementById('select-role').value;
        try {
            await apiCall(`/admin/users/${userId}/role`, {
                method: 'POST',
                body: JSON.stringify({ role: newRole })
            });
            alert('Role updated successfully');
            loadUserDetails();
            loadAuditTrail();
        } catch (error) {
            alert('Failed to update role: ' + error.message);
        }
    }

    // Save tier
    async function saveTier() {
        const newTier = document.getElementById('select-tier').value;
        try {
            await apiCall(`/admin/users/${userId}/tier`, {
                method: 'POST',
                body: JSON.stringify({ tier: newTier })
            });
            alert('Tier updated successfully');
            loadUserDetails();
            loadAuditTrail();
        } catch (error) {
            alert('Failed to update tier: ' + error.message);
        }
    }

    // Add credits
    async function addCredits() {
        const amount = parseInt(document.getElementById('credits-amount').value);
        const reason = document.getElementById('credits-reason').value;

        if (!amount || amount <= 0) {
            alert('Please enter a valid amount');
            return;
        }

        try {
            await apiCall(`/admin/users/${userId}/credits/add`, {
                method: 'POST',
                body: JSON.stringify({
                    amount: amount,
                    reason: reason || 'Admin grant',
                    transaction_type: 'admin_grant'
                })
            });
            alert(`Added ${amount} credits successfully`);
            document.getElementById('credits-amount').value = '';
            document.getElementById('credits-reason').value = '';
            loadCredits();
            loadAuditTrail();
        } catch (error) {
            alert('Failed to add credits: ' + error.message);
        }
    }

    // Deduct credits
    async function deductCredits() {
        const amount = parseInt(document.getElementById('credits-amount').value);
        const reason = document.getElementById('credits-reason').value;

        if (!amount || amount <= 0) {
            alert('Please enter a valid amount');
            return;
        }

        try {
            await apiCall(`/admin/users/${userId}/credits/deduct`, {
                method: 'POST',
                body: JSON.stringify({
                    amount: amount,
                    reason: reason || 'Admin deduction',
                    transaction_type: 'admin_deduct'
                })
            });
            alert(`Deducted ${amount} credits successfully`);
            document.getElementById('credits-amount').value = '';
            document.getElementById('credits-reason').value = '';
            loadCredits();
            loadAuditTrail();
        } catch (error) {
            alert('Failed to deduct credits: ' + error.message);
        }
    }

    // Block/unblock user
    async function toggleBlock() {
        const status = await apiCall(`/admin/users/${userId}/block-status`);

        if (status.is_blocked) {
            // Unblock
            if (confirm('Are you sure you want to unblock this user?')) {
                try {
                    await apiCall(`/admin/users/${userId}/unblock`, { method: 'POST' });
                    alert('User unblocked successfully');
                    loadBlockStatus();
                    loadAuditTrail();
                } catch (error) {
                    alert('Failed to unblock user: ' + error.message);
                }
            }
        } else {
            // Show block modal
            document.getElementById('block-modal').classList.remove('hidden');
        }
    }

    // Confirm block
    async function confirmBlock() {
        const reason = document.getElementById('block-reason').value.trim();
        if (!reason) {
            alert('Please enter a reason for blocking');
            return;
        }

        try {
            await apiCall(`/admin/users/${userId}/block`, {
                method: 'POST',
                body: JSON.stringify({ reason: reason })
            });
            document.getElementById('block-modal').classList.add('hidden');
            document.getElementById('block-reason').value = '';
            alert('User blocked successfully');
            loadBlockStatus();
            loadAuditTrail();
        } catch (error) {
            alert('Failed to block user: ' + error.message);
        }
    }

    // Setup event listeners
    function setupEventListeners() {
        document.getElementById('btn-save-role').addEventListener('click', saveRole);
        document.getElementById('btn-save-tier').addEventListener('click', saveTier);
        document.getElementById('btn-add-credits').addEventListener('click', addCredits);
        document.getElementById('btn-deduct-credits').addEventListener('click', deductCredits);
        document.getElementById('btn-block').addEventListener('click', toggleBlock);

        // Modal controls
        document.getElementById('modal-close').addEventListener('click', () => {
            document.getElementById('block-modal').classList.add('hidden');
        });
        document.getElementById('btn-cancel-block').addEventListener('click', () => {
            document.getElementById('block-modal').classList.add('hidden');
        });
        document.getElementById('btn-confirm-block').addEventListener('click', confirmBlock);
    }

    // Initialize
    document.addEventListener('DOMContentLoaded', () => {
        setupEventListeners();
        loadUserDetails();
        loadCredits();
        loadBlockStatus();
        loadSessions();
        loadAuditTrail();
    });

})();
