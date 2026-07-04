/**
 * Profile page - Display user credits, tier, and deposit info
 */

// Get JWT token from localStorage
function getAuthToken() {
    return localStorage.getItem('auth_token');
}

// Fetch data from API with auth
async function fetchWithAuth(url) {
    const token = getAuthToken();

    // Prepare headers - include token if available in localStorage
    const headers = {};
    if (token) {
        headers['Authorization'] = `Bearer ${token}`;
    }
    // Cookie will be sent automatically by browser

    const response = await fetch(url, {
        headers: headers,
        credentials: 'include'  // Ensure cookies are sent
    });

    if (response.status === 401) {
        // Token expired - clear BOTH localStorage AND cookie
        localStorage.removeItem('auth_token');
        localStorage.removeItem('wallet_address');
        localStorage.removeItem('tier');
        document.cookie = 'auth_token=; path=/; max-age=0';
        // Redirect to signin with return_to parameter
        const returnTo = encodeURIComponent(window.location.pathname);
        window.location.href = `/signin?return_to=${returnTo}`;
        return null;
    }

    // Handle service unavailable (initialization in progress)
    if (response.status === 503) {
        console.warn('Services temporarily unavailable (503)');
        return {
            error: true,
            status: 503,
            message: 'Payment services are initializing. Please wait a moment and refresh.'
        };
    }

    if (!response.ok) {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
    }

    return response.json();
}

// Load user balance and tier info
async function loadBalance() {
    try {
        // Add cache-busting parameter
        const data = await fetchWithAuth('/api/payments/balance?_t=' + Date.now());
        if (!data) return;
        
        // Clear any login prompt
        const loginPrompt = document.getElementById('login-prompt');
        if (loginPrompt) loginPrompt.remove();

        // Check if service is unavailable (503 error)
        if (data.error && data.status === 503) {
            document.getElementById('tier-badge').textContent = 'LOADING...';
            document.getElementById('tier-description').textContent = data.message;
            return;
        }

        // Real API returns: { user_id, balance, lifetime_earned, lifetime_spent, tier, monthly_allowance, allowance_expires_at }
        // Update tier badge
        const tierBadge = document.getElementById('tier-badge');
        tierBadge.textContent = data.tier.toUpperCase();
        tierBadge.className = `tier-badge tier-${data.tier}`;

        // Tier description
        const tierDesc = document.getElementById('tier-description');
        if (data.tier === 'free') {
            tierDesc.innerHTML = '⚠️ Beta access requires DEN token ownership. <a href="https://t.me/tmachinrobot" target="_blank">Get access</a>';
        } else if (data.tier === 'holder') {
            tierDesc.textContent = '✅ Beta Access Granted - DEN Token Holder';
        } else if (data.tier === 'premium') {
            tierDesc.textContent = '✅ Premium Access - DEN Token Holder (3+)';
        }

        // Update balances - real API has flat structure
        document.getElementById('current-balance').textContent = data.balance.toLocaleString();
        document.getElementById('lifetime-earned').textContent = data.lifetime_earned.toLocaleString();
        document.getElementById('lifetime-spent').textContent = data.lifetime_spent.toLocaleString();

    } catch (error) {
        console.error('Error loading balance:', error);
        
        // Check if this is an authentication error
        if (error.status === 401 || error.message?.includes('Authentication required')) {
            // Show login prompt
            showLoginPrompt();
            return;
        }
        
        document.getElementById('tier-badge').textContent = 'ERROR';
        document.getElementById('tier-description').textContent = 'Failed to load tier information';
    }
}

// Show login prompt for unauthenticated users
function showLoginPrompt() {
    const tierSection = document.getElementById('tier-badge')?.parentElement;
    if (!tierSection) return;
    
    // Create login prompt
    const promptDiv = document.createElement('div');
    promptDiv.id = 'login-prompt';
    promptDiv.style.cssText = 'background: #fff3cd; border: 1px solid #ffc107; border-radius: 8px; padding: 20px; margin: 20px 0; text-align: center;';
    promptDiv.innerHTML = `
        <h3 style="color: #856404; margin-top: 0;">🔐 Sign In Required</h3>
        <p style="color: #856404;">Please sign in with your wallet to view your balance and credits.</p>
        <a href="/signin" style="display: inline-block; background: #007bff; color: white; padding: 10px 20px; border-radius: 5px; text-decoration: none; margin-top: 10px;">
            Sign In with Wallet
        </a>
    `;
    
    // Insert before tier section
    tierSection.parentElement.insertBefore(promptDiv, tierSection);
    
    // Hide tier info
    document.getElementById('tier-badge').textContent = 'SIGN IN REQUIRED';
    document.getElementById('tier-description').textContent = 'Please sign in to view your tier';
}

// Load deposit address
async function loadDepositAddress() {
    try {
        // Add cache-busting parameter
        const data = await fetchWithAuth('/api/payments/deposit-address?_t=' + Date.now());
        if (!data) return;

        const addressEl = document.getElementById('deposit-address');
        addressEl.textContent = data.deposit_address;

        // Generate QR code
        const qrContainer = document.getElementById('qr-code');
        qrContainer.innerHTML = '';  // Clear loading message

        // Use QRCode library (loaded from CDN)
        if (window.QRCode) {
            new QRCode(qrContainer, {
                text: data.deposit_address,
                width: 200,
                height: 200,
                colorDark: '#000000',
                colorLight: '#ffffff'
            });
        } else {
            qrContainer.textContent = 'QR code library not loaded';
        }

        // Copy button
        document.getElementById('copy-address-btn').addEventListener('click', async () => {
            try {
                await navigator.clipboard.writeText(data.deposit_address);

                const btn = document.getElementById('copy-address-btn');
                const originalText = btn.textContent;
                btn.textContent = 'COPIED!';
                setTimeout(() => {
                    btn.textContent = originalText;
                }, 2000);
            } catch (error) {
                console.error('Failed to copy:', error);
                alert('Failed to copy address');
            }
        });

    } catch (error) {
        console.error('Error loading deposit address:', error);
        document.getElementById('deposit-address').textContent = 'Error loading address';
        document.getElementById('qr-code').textContent = 'QR code unavailable';
    }
}

// Transaction pagination state
let transactionState = {
    loaded: 0,
    total: 0,
    hasMore: true,
    loading: false,
    pageSize: 100
};

// Load ALL transactions (merged credit transactions and crypto deposits)
async function loadAllTransactions(append = false) {
    if (transactionState.loading) return;
    transactionState.loading = true;

    try {
        const offset = append ? transactionState.loaded : 0;

        // Fetch both types of transactions in parallel with pagination support
        const [txResponse, cryptoDeposits] = await Promise.all([
            fetchWithAuth(`/api/payments/transactions?limit=${transactionState.pageSize}&offset=${offset}&paginated=true&_t=${Date.now()}`),
            fetchWithAuth(`/api/payments/deposits?limit=${transactionState.pageSize}&offset=${offset}&_t=${Date.now()}`)
        ]);

        if (!txResponse || !cryptoDeposits) {
            transactionState.loading = false;
            return;
        }

        const tbody = document.getElementById('transactions-body');

        // Handle paginated response format
        const creditTransactions = txResponse.transactions || txResponse;
        transactionState.total = txResponse.total || creditTransactions.length;
        transactionState.hasMore = txResponse.has_more || false;

        // Combine all transactions
        const allTransactions = [];

        // Add credit transactions
        if (creditTransactions && creditTransactions.length > 0) {
            creditTransactions.forEach(tx => {
                allTransactions.push({
                    timestamp: new Date(tx.timestamp).getTime(),
                    date: new Date(tx.timestamp).toLocaleString(),
                    type: tx.transaction_type,
                    amount: tx.amount,
                    details: tx.reason || '-',
                    balanceAfter: tx.balance_after,
                    isDeposit: false
                });
            });
        }

        // Add crypto deposits
        if (cryptoDeposits && cryptoDeposits.length > 0) {
            cryptoDeposits.forEach(deposit => {
                const details = `<span class="chain-badge">${deposit.chain.toUpperCase()}</span> ${deposit.amount} ${deposit.token_symbol} ($${deposit.amount_usd.toFixed(2)})`;
                const statusClass = deposit.status === 'confirmed' ? 'status-confirmed' : 'status-pending';

                allTransactions.push({
                    timestamp: new Date(deposit.detected_at).getTime(),
                    date: new Date(deposit.detected_at).toLocaleString(),
                    type: 'deposit',
                    amount: deposit.credits_purchased,
                    details: `${details} <span class="${statusClass}">${deposit.status}</span>`,
                    balanceAfter: '-',
                    isDeposit: true
                });
            });
        }

        // Sort by timestamp (newest first)
        allTransactions.sort((a, b) => b.timestamp - a.timestamp);

        if (allTransactions.length === 0 && !append) {
            tbody.innerHTML = '<tr><td colspan="5" class="empty-row">No transactions yet</td></tr>';
            transactionState.loading = false;
            return;
        }

        // Render transactions
        const txHtml = allTransactions.map(tx => {
            // Determine if this is a positive transaction (credits added)
            const isPositive = tx.type === 'purchase' || tx.type === 'allowance' ||
                               tx.type === 'deposit' || tx.type === 'welcome' ||
                               tx.type === 'den_allowance' || tx.type === 'refund' ||
                               tx.type === 'admin_grant' || tx.amount > 0;

            const amountClass = isPositive ? 'tx-positive' : 'tx-negative';
            const amountPrefix = isPositive ? '+' : '';
            const displayAmount = Math.abs(tx.amount);

            return `
                <tr>
                    <td>${tx.date}</td>
                    <td><span class="tx-type tx-type-${tx.type}">${tx.type}</span></td>
                    <td class="${amountClass}">${amountPrefix}${displayAmount.toLocaleString()}</td>
                    <td>${tx.details}</td>
                    <td>${tx.balanceAfter !== '-' ? tx.balanceAfter.toLocaleString() : '-'}</td>
                </tr>
            `;
        }).join('');

        if (append) {
            // Remove "load more" row if exists and append new transactions
            const loadMoreRow = tbody.querySelector('.load-more-row');
            if (loadMoreRow) loadMoreRow.remove();
            tbody.insertAdjacentHTML('beforeend', txHtml);
        } else {
            tbody.innerHTML = txHtml;
        }

        // Update pagination state
        transactionState.loaded = offset + allTransactions.length;

        // Add "load more" button if there are more transactions
        if (transactionState.hasMore) {
            const remaining = transactionState.total - transactionState.loaded;
            tbody.insertAdjacentHTML('beforeend', `
                <tr class="load-more-row">
                    <td colspan="5" style="text-align: center; padding: 15px;">
                        <button onclick="loadAllTransactions(true)" class="btn-new-session" style="padding: 8px 20px;">
                            LOAD MORE (${remaining} remaining)
                        </button>
                    </td>
                </tr>
            `);
        }

        // Show total count
        updateTransactionCount();

    } catch (error) {
        console.error('Error loading transactions:', error);
        if (!append) {
            document.getElementById('transactions-body').innerHTML =
                '<tr><td colspan="5" class="error-row">Error loading transactions</td></tr>';
        }
    } finally {
        transactionState.loading = false;
    }
}

// Update transaction count display
function updateTransactionCount() {
    const header = document.querySelector('.profile-section-header:last-of-type');
    if (header && header.textContent.includes('Transaction')) {
        const countSpan = header.querySelector('.tx-count') || document.createElement('span');
        countSpan.className = 'tx-count';
        countSpan.style.cssText = 'font-size: 12px; color: var(--text-muted); margin-left: 10px;';
        countSpan.textContent = `(${transactionState.loaded}${transactionState.hasMore ? '+' : ''} of ${transactionState.total})`;
        if (!header.querySelector('.tx-count')) {
            header.appendChild(countSpan);
        }
    }
}

// Load and display wallet address from localStorage
function loadWalletAddress() {
    const walletAddress = localStorage.getItem('wallet_address');
    const walletEl = document.getElementById('wallet-address');

    if (walletAddress) {
        walletEl.textContent = walletAddress;
        walletEl.style.cursor = 'pointer';
        walletEl.title = 'Click to copy';

        // Add click-to-copy functionality
        walletEl.addEventListener('click', async () => {
            try {
                await navigator.clipboard.writeText(walletAddress);
                const originalText = walletEl.textContent;
                walletEl.textContent = 'Copied to clipboard!';
                walletEl.style.color = 'var(--accent-blue)';

                setTimeout(() => {
                    walletEl.textContent = originalText;
                    walletEl.style.color = '';
                }, 2000);
            } catch (error) {
                console.error('Failed to copy wallet address:', error);
            }
        });
    } else {
        walletEl.textContent = 'No wallet connected';
        walletEl.style.color = 'var(--text-muted)';
    }
}

// Initialize page
async function init() {
    // Middleware already validated auth - no need to re-check

    // Load wallet address immediately (no async needed)
    loadWalletAddress();

    // Load all data
    await Promise.all([
        loadBalance(),
        loadDepositAddress(),
        loadAllTransactions()
    ]);

    // Auto-refresh every 2 minutes (avoid rate limits)
    setInterval(() => {
        loadBalance();
        loadAllTransactions();
    }, 120000);
}

// Start when page loads
document.addEventListener('DOMContentLoaded', init);
