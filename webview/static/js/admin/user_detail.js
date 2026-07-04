/**
 * Admin User Detail JavaScript
 * Handles loading user details, token verification, credits, and blocking
 */

(function() {
    'use strict';

    // Use shared utilities
    const { apiCall, formatDateTime, formatCredits, timeAgo, showAlert, showConfirm } = AdminUtils;

    const userId = window.TARGET_USER_ID;
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
            let badges = `
                <span class="badge badge-tier tier-${user.tier}">${user.tier}</span>
                <span class="badge badge-role role-${user.role}">${user.role}</span>
            `;
            document.getElementById('user-badges').innerHTML = badges;

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

            let html = '';
            for (const tx of credits.recent_transactions.slice(0, 10)) {
                const amountClass = tx.amount > 0 ? 'amount-positive' : 'amount-negative';
                const amountPrefix = tx.amount > 0 ? '+' : '';
                html += `
                    <tr>
                        <td>${formatDateTime(tx.timestamp)}</td>
                        <td class="${amountClass}">${amountPrefix}${formatCredits(tx.amount)}</td>
                        <td>${tx.transaction_type}</td>
                        <td>${tx.reason || '-'}</td>
                    </tr>
                `;
            }
            tbody.innerHTML = html;

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
                container.innerHTML = `
                    <span style="color: var(--accent-red);">BLOCKED</span>
                    <div class="block-reason">Reason: ${status.blocked_reason || 'Not specified'}</div>
                    <div class="block-reason">Blocked at: ${formatDateTime(status.blocked_at)}</div>
                    <div class="block-reason">By: ${status.blocked_by || 'Unknown'}</div>
                `;
                btn.textContent = 'Unblock User';
                btn.classList.remove('btn-danger');
                btn.classList.add('btn-success');

                // Add blocked badge
                const badges = document.getElementById('user-badges');
                if (!badges.querySelector('.badge-blocked')) {
                    badges.innerHTML += '<span class="badge badge-blocked">BLOCKED</span>';
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

            let html = '';
            for (const event of events) {
                html += `
                    <div class="audit-item">
                        <span class="audit-time">${formatDateTime(event.timestamp)}</span>
                        <span class="audit-type ${event.event_type}">${event.event_type.replace('_', ' ')}</span>
                        <span class="audit-action">${event.action}</span>
                    </div>
                `;
            }
            container.innerHTML = html;

        } catch (error) {
            console.error('Failed to load audit trail:', error);
            document.getElementById('audit-list').innerHTML = '<div class="empty-state">Failed to load audit trail</div>';
        }
    }

    // Verify token
    async function verifyToken() {
        const btn = document.getElementById('btn-verify-token');
        btn.disabled = true;
        btn.textContent = 'Verifying...';

        try {
            const result = await apiCall(`/admin/users/${userId}/verify-token`, {
                method: 'POST'
            });

            // Update token status display
            const container = document.getElementById('token-status');
            const icon = document.getElementById('token-icon');
            const count = document.getElementById('token-count');
            const verified = document.getElementById('token-verified');
            const tokenIds = document.getElementById('token-ids');

            if (result.new_count > 0) {
                container.classList.add('has-token');
                container.classList.remove('no-token');
                icon.textContent = '✓';
                count.textContent = `${result.new_count} DEN Token(s)`;
            } else {
                container.classList.remove('has-token');
                container.classList.add('no-token');
                icon.textContent = '✗';
                count.textContent = 'No DEN Tokens';
            }

            verified.textContent = `Last verified: ${formatDateTime(result.verified_at)}`;

            if (result.token_ids && result.token_ids.length > 0) {
                tokenIds.textContent = `Token IDs: ${result.token_ids.join(', ')}`;
            } else {
                tokenIds.textContent = '';
            }

            // Show result message
            let message = `Token verification complete.\n`;
            message += `Previous: ${result.previous_count} -> New: ${result.new_count}\n`;
            if (result.tier_changed) {
                message += `Tier changed: ${result.previous_tier} -> ${result.new_tier}\n`;
            }
            if (result.bonuses_granted > 0) {
                message += `Bonuses granted: ${result.bonuses_granted}\n`;
            }
            if (result.bonuses_already_claimed > 0) {
                message += `Bonuses already claimed: ${result.bonuses_already_claimed}`;
            }
            alert(message);

            // Reload data
            loadUserDetails();
            loadCredits();

        } catch (error) {
            console.error('Token verification failed:', error);
            alert('Token verification failed: ' + error.message);
        } finally {
            btn.disabled = false;
            btn.textContent = 'Verify Token';
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
        document.getElementById('btn-verify-token').addEventListener('click', verifyToken);
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
