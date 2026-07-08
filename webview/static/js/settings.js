/**
 * Settings page - User configuration management
 * Extensible structure for MCP servers, preferences, API keys, etc.
 */

// ========================================
// TAB NAVIGATION
// ========================================

function initTabNavigation() {
    const navItems = document.querySelectorAll('.admin-nav-item');
    const sections = document.querySelectorAll('.settings-section');

    navItems.forEach(item => {
        item.addEventListener('click', () => {
            const sectionId = item.dataset.section;

            // Update nav items
            navItems.forEach(nav => nav.classList.remove('active'));
            item.classList.add('active');

            // Update sections
            sections.forEach(section => {
                section.classList.remove('active');
                if (section.id === `section-${sectionId}`) {
                    section.classList.add('active');
                }
            });

            // Store active tab in localStorage
            localStorage.setItem('settings_active_tab', sectionId);
        });
    });

    // Restore last active tab
    const savedTab = localStorage.getItem('settings_active_tab');
    if (savedTab) {
        const tabButton = document.querySelector(`[data-section="${savedTab}"]`);
        if (tabButton) {
            tabButton.click();
        }
    }
}

// ========================================
// AUTHENTICATION
// ========================================

// Get JWT token from localStorage
function getAuthToken() {
    return localStorage.getItem('auth_token');
}

// Fetch data from API with auth
async function fetchWithAuth(url, options = {}) {
    const token = getAuthToken();

    const headers = {
        'Content-Type': 'application/json',
        ...options.headers
    };
    if (token) {
        headers['Authorization'] = `Bearer ${token}`;
    }

    const response = await fetch(url, {
        ...options,
        headers: headers,
        credentials: 'include'
    });

    if (response.status === 401) {
        localStorage.removeItem('auth_token');
        localStorage.removeItem('wallet_address');
        localStorage.removeItem('tier');
        document.cookie = 'auth_token=; path=/; max-age=0';
        const returnTo = encodeURIComponent(window.location.pathname);
        window.location.href = `/signin?return_to=${returnTo}`;
        return null;
    }

    if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        throw new Error(errorData.detail || `HTTP ${response.status}: ${response.statusText}`);
    }

    return response.json();
}

// State
let userServers = [];
let globalServers = [];
let apiServiceAvailable = true;
let userSettings = {
    mcp_enabled: true,
    include_global_servers: true,
    max_servers: 10
};

// ========================================
// API-SERVICE FEATURE DETECTION
// ========================================
// The MCP/skills/trading endpoints live on the separate POLYROB API service
// (:9000), not on the webview process. On standalone/monitoring deployments
// (local dev, the own_ops VPS console) they 404 — probe once and render an
// honest state instead of console errors + "Error loading your servers".

async function probeApiService() {
    try {
        const r = await fetch('/api/mcp/settings', { credentials: 'include' });
        // 404 = the API surface is not mounted/routed in this deployment.
        // Anything else (200, 401, 403...) means the surface exists.
        return r.status !== 404;
    } catch (_e) {
        return false; // network error -> no API service
    }
}

function renderApiUnavailable() {
    const msg =
        '<div class="empty-state">' +
        '<p>This section needs the POLYROB API service, which is not running in this deployment.</p>' +
        '<p style="color: var(--color-text-muted); font-size: 12px;">' +
        'MCP servers, skills and trading tools are managed by the agent/API process ' +
        '(this console is monitoring-only here).</p>' +
        '</div>';
    ['global-servers', 'user-servers', 'system-skills', 'user-skills'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.innerHTML = msg;
    });
    // The MCP toggles PATCH the same absent API — freeze them.
    ['mcp-enabled', 'include-global'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.disabled = true;
    });
    ['mcp-enabled-label', 'include-global-label', 'max-servers'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.textContent = '—';
    });
    const addServer = document.getElementById('btn-add-server');
    if (addServer) addServer.style.display = 'none';
    const addSkill = document.getElementById('btn-add-skill');
    if (addSkill) addSkill.style.display = 'none';
}

// Load user settings
async function loadSettings() {
    try {
        const data = await fetchWithAuth('/api/mcp/settings');
        if (!data) return;

        userSettings = data;

        // Update UI
        document.getElementById('mcp-enabled').checked = data.mcp_enabled;
        document.getElementById('mcp-enabled-label').textContent = data.mcp_enabled ? 'Yes' : 'No';

        document.getElementById('include-global').checked = data.include_global_servers;
        document.getElementById('include-global-label').textContent = data.include_global_servers ? 'Yes' : 'No';

        document.getElementById('max-servers').textContent = data.max_servers;
    } catch (error) {
        console.error('Error loading settings:', error);
    }
}

// Save settings on toggle
async function saveSettings(key, value) {
    try {
        await fetchWithAuth('/api/mcp/settings', {
            method: 'PATCH',
            body: JSON.stringify({ [key]: value })
        });

        // Update label
        if (key === 'mcp_enabled') {
            document.getElementById('mcp-enabled-label').textContent = value ? 'Yes' : 'No';
        } else if (key === 'include_global_servers') {
            document.getElementById('include-global-label').textContent = value ? 'Yes' : 'No';
        }
    } catch (error) {
        console.error('Error saving settings:', error);
        // Revert toggle
        if (key === 'mcp_enabled') {
            document.getElementById('mcp-enabled').checked = !value;
        } else if (key === 'include_global_servers') {
            document.getElementById('include-global').checked = !value;
        }
    }
}

// Load available servers
async function loadAvailableServers() {
    try {
        const data = await fetchWithAuth('/api/mcp/available');
        if (!data) return;

        globalServers = data.global_servers || [];
        renderGlobalServers();

    } catch (error) {
        console.error('Error loading available servers:', error);
        document.getElementById('global-servers').innerHTML =
            '<div class="empty-state">Error loading global servers</div>';
    }
}

// Load user's servers
async function loadUserServers() {
    try {
        const data = await fetchWithAuth('/api/mcp/servers');
        if (!data) return;

        userServers = data.servers || [];
        renderUserServers();

    } catch (error) {
        console.error('Error loading user servers:', error);
        document.getElementById('user-servers').innerHTML =
            '<div class="empty-state">Error loading your servers</div>';
    }
}

// Render global servers + Polymarket
function renderGlobalServers() {
    const container = document.getElementById('global-servers');

    // Render global MCP servers
    let html = globalServers.map(server => `
        <div class="server-card">
            <div class="server-info">
                <div class="server-name">
                    ${escapeHtml(server.display_name || server.name)}
                    <span class="type-badge">Global</span>
                </div>
                <div class="server-meta">
                    <span>Tool ID: <span class="tool-id">${escapeHtml(server.tool_id)}</span></span>
                </div>
            </div>
        </div>
    `).join('');

    // Crypto-trading cards render only when their status endpoint answered —
    // a 404/error means the tool isn't available in this deployment, so no
    // misleading "Setup Required" card.
    if (polymarketStatus && !polymarketStatus.error) {
        html += renderPolymarketCard();
    }
    if (hyperliquidStatus && !hyperliquidStatus.error) {
        html += renderHyperliquidCard();
    }

    container.innerHTML = html || '<div class="empty-state">No platform servers available</div>';

    // Bind configure buttons after render
    bindPolymarketConfigButton();
    bindHyperliquidConfigButton();
}

// Render Polymarket card for Platform Servers section
function renderPolymarketCard() {
    const status = polymarketStatus || {};
    let statusBadge, statusColor, modeText;

    if (status.configured) {
        if (status.enabled) {
            statusBadge = status.demo_mode ? 'Demo' : 'Trading';
            statusColor = 'var(--color-accent-green)';
        } else {
            statusBadge = 'Disabled';
            statusColor = 'var(--color-text-muted)';
        }
        modeText = status.demo_mode ? 'Read-only market data' : `Wallet: ${status.wallet_address || '...'}`;
    } else {
        statusBadge = 'Setup Required';
        statusColor = 'var(--color-accent-yellow)';
        modeText = 'Configure to access prediction markets';
    }

    return `
        <div class="server-card" id="polymarket-card">
            <div class="server-info">
                <div class="server-name">
                    Polymarket
                    <span class="type-badge" style="background: ${statusColor};">${statusBadge}</span>
                </div>
                <div class="server-meta">
                    <span>Tool ID: <span class="tool-id">polymarket</span></span>
                    <span>${escapeHtml(modeText)}</span>
                </div>
            </div>
            <div class="server-actions">
                <button class="btn-server-action" id="pm-btn-configure">Configure</button>
            </div>
        </div>
    `;
}

// Render user servers
function renderUserServers() {
    const container = document.getElementById('user-servers');

    if (userServers.length === 0) {
        container.innerHTML = `
            <div class="empty-state">
                <p>You haven't added any custom MCP servers yet.</p>
                <p style="color: var(--color-text-muted); font-size: 12px;">Click "+ ADD SERVER" above to add your first MCP server.</p>
            </div>
        `;
        return;
    }

    container.innerHTML = userServers.map(server => `
        <div class="server-card" data-server="${escapeHtml(server.server_name)}">
            <div class="server-info">
                <div class="server-name">
                    ${escapeHtml(server.display_name || server.server_name)}
                    <span class="type-badge">${escapeHtml(server.server_type.toUpperCase())}</span>
                    ${!server.enabled ? '<span class="type-badge" style="background: var(--text-muted);">Disabled</span>' : ''}
                    ${server.auto_reconnect ? '<span class="type-badge" style="background: var(--color-accent-purple);">Auto-Reconnect</span>' : ''}
                </div>
                <div class="server-url">${escapeHtml(server.server_url)}</div>
                <div class="server-meta">
                    <span class="status ${server.auth_status}">
                        ${getStatusIcon(server.auth_status)} ${server.auth_status}
                    </span>
                    <span>Tools: ${server.tools_discovered}</span>
                    <span>Connections: ${server.connection_count}</span>
                    <span>Timeout: ${server.timeout}s</span>
                    <span>Retries: ${server.retry_attempts}×${server.retry_delay}s</span>
                </div>
                <div class="server-meta" style="margin-top: 4px;">
                    <span>Tool ID: <span class="tool-id">${escapeHtml(server.tool_id)}</span></span>
                    ${server.message_endpoint ? `<span>POST: <span class="tool-id">${escapeHtml(server.message_endpoint)}</span></span>` : ''}
                </div>
                ${server.last_error ? `<div class="form-error" style="margin-top: 8px;">${escapeHtml(server.last_error)}</div>` : ''}
            </div>
            <div class="server-actions">
                <button class="btn-server-action btn-test" onclick="testServer('${escapeHtml(server.server_name)}')" title="Test connection">Test</button>
                <button class="btn-server-action" onclick="toggleServer('${escapeHtml(server.server_name)}', ${!server.enabled})" title="${server.enabled ? 'Disable' : 'Enable'}">
                    ${server.enabled ? 'Disable' : 'Enable'}
                </button>
                <button class="btn-server-action btn-delete" onclick="deleteServer('${escapeHtml(server.server_name)}')" title="Delete">Delete</button>
            </div>
        </div>
    `).join('');
}

// Get status icon
function getStatusIcon(status) {
    switch (status) {
        case 'connected': return '<span style="color: var(--accent-green);">&#10003;</span>';
        case 'error': return '<span style="color: var(--accent-red);">&#10007;</span>';
        case 'configured': return '<span style="color: var(--accent-yellow);">&#9679;</span>';
        default: return '<span style="color: var(--text-muted);">&#9679;</span>';
    }
}

// Escape HTML
function escapeHtml(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

// Modal management
function openAddModal() {
    document.getElementById('add-server-modal').classList.remove('hidden');
    document.getElementById('server-name').focus();
}

function closeAddModal() {
    document.getElementById('add-server-modal').classList.add('hidden');
    document.getElementById('add-server-form').reset();
    document.getElementById('form-error').style.display = 'none';
    // Reset advanced options to defaults
    document.getElementById('timeout').value = 30;
    document.getElementById('retry-attempts').value = 3;
    document.getElementById('retry-delay').value = 5;
    document.getElementById('max-concurrent').value = 5;
    document.getElementById('auto-reconnect').checked = true;
    document.getElementById('verify-connection').checked = false;
    document.getElementById('message-endpoint').value = '';
    // Show API key field by default
    document.getElementById('api-key-group').style.display = 'block';
}

// Add server
async function addServer(event) {
    event.preventDefault();

    const formError = document.getElementById('form-error');
    formError.style.display = 'none';

    const data = {
        server_name: document.getElementById('server-name').value.toLowerCase(),
        server_url: document.getElementById('server-url').value,
        server_type: document.getElementById('server-type').value,
        auth_method: document.getElementById('auth-method').value,
        display_name: document.getElementById('display-name').value || null,
        // Advanced options
        timeout: parseInt(document.getElementById('timeout').value) || 30,
        retry_attempts: parseInt(document.getElementById('retry-attempts').value) || 3,
        retry_delay: parseInt(document.getElementById('retry-delay').value) || 5,
        max_concurrent_requests: parseInt(document.getElementById('max-concurrent').value) || 5,
        auto_reconnect: document.getElementById('auto-reconnect').checked,
        verify_connection: document.getElementById('verify-connection').checked
    };

    // Add message_endpoint if provided
    const messageEndpoint = document.getElementById('message-endpoint').value;
    if (messageEndpoint) {
        data.message_endpoint = messageEndpoint;
    }

    const apiKey = document.getElementById('api-key').value;
    if (apiKey && data.auth_method !== 'none') {
        data.api_key = apiKey;
    }

    try {
        await fetchWithAuth('/api/mcp/servers', {
            method: 'POST',
            body: JSON.stringify(data)
        });

        closeAddModal();
        await loadUserServers();

    } catch (error) {
        console.error('Error adding server:', error);
        formError.textContent = error.message || 'Failed to add server';
        formError.style.display = 'block';
    }
}

// Test server connection
async function testServer(serverName) {
    const card = document.querySelector(`[data-server="${serverName}"]`);
    const testBtn = card?.querySelector('.btn-test');

    if (testBtn) {
        testBtn.textContent = 'Testing...';
        testBtn.disabled = true;
    }

    try {
        const result = await fetchWithAuth(`/api/mcp/servers/${serverName}/test`, {
            method: 'POST'
        });

        if (result.success) {
            alert(`Connection successful! Latency: ${result.latency_ms}ms`);
        } else {
            alert(`Connection failed: ${result.error}`);
        }

        await loadUserServers();

    } catch (error) {
        console.error('Error testing server:', error);
        alert(`Test failed: ${error.message}`);
    } finally {
        if (testBtn) {
            testBtn.textContent = 'Test';
            testBtn.disabled = false;
        }
    }
}

// Toggle server enabled/disabled
async function toggleServer(serverName, enabled) {
    try {
        await fetchWithAuth(`/api/mcp/servers/${serverName}`, {
            method: 'PATCH',
            body: JSON.stringify({ enabled })
        });

        await loadUserServers();

    } catch (error) {
        console.error('Error toggling server:', error);
        alert(`Failed to ${enabled ? 'enable' : 'disable'} server: ${error.message}`);
    }
}

// Delete server
async function deleteServer(serverName) {
    if (!confirm(`Are you sure you want to delete "${serverName}"? This cannot be undone.`)) {
        return;
    }

    try {
        await fetchWithAuth(`/api/mcp/servers/${serverName}`, {
            method: 'DELETE'
        });

        await loadUserServers();

    } catch (error) {
        console.error('Error deleting server:', error);
        alert(`Failed to delete server: ${error.message}`);
    }
}

// Toggle API key field visibility based on auth method
function updateApiKeyVisibility() {
    const authMethod = document.getElementById('auth-method').value;
    const apiKeyGroup = document.getElementById('api-key-group');

    if (authMethod === 'none') {
        apiKeyGroup.style.display = 'none';
    } else {
        apiKeyGroup.style.display = 'block';
    }
}

// ========================================
// SKILLS MANAGEMENT
// ========================================

let systemSkills = [];
let userSkills = [];
let viewingSkillId = null;

// Load all skills
async function loadSkills() {
    try {
        const data = await fetchWithAuth('/api/skills');
        if (!data) return;

        systemSkills = data.system || [];
        userSkills = data.user || [];

        renderSystemSkills();
        renderUserSkills();
    } catch (error) {
        console.error('Error loading skills:', error);
        document.getElementById('system-skills').innerHTML =
            '<div class="empty-state">Error loading skills</div>';
        document.getElementById('user-skills').innerHTML =
            '<div class="empty-state">Error loading skills</div>';
    }
}

// Render system skills
function renderSystemSkills() {
    const container = document.getElementById('system-skills');

    if (systemSkills.length === 0) {
        container.innerHTML = '<div class="empty-state">No system skills available</div>';
        return;
    }

    container.innerHTML = systemSkills.map(skill => `
        <div class="server-card" data-skill="${escapeHtml(skill.id)}">
            <div class="server-info">
                <div class="server-name">
                    ${escapeHtml(skill.name || skill.id)}
                    <span class="type-badge">System</span>
                    <span class="type-badge" style="background: var(--color-accent-purple);">P${skill.priority}</span>
                </div>
                <div class="server-meta">
                    ${escapeHtml(skill.description || 'No description')}
                </div>
                <div class="server-meta" style="margin-top: 4px;">
                    ${skill.triggers?.keywords?.length ? `<span>Keywords: ${skill.triggers.keywords.slice(0, 3).join(', ')}${skill.triggers.keywords.length > 3 ? '...' : ''}</span>` : ''}
                    ${skill.triggers?.tool_ids?.length ? `<span>Tools: ${skill.triggers.tool_ids.join(', ')}</span>` : ''}
                </div>
            </div>
            <div class="server-actions">
                <button class="btn-server-action" onclick="viewSkill('${escapeHtml(skill.id)}', 'system')">View</button>
                <button class="btn-server-action btn-add" onclick="forkSkill('${escapeHtml(skill.id)}')">Fork</button>
            </div>
        </div>
    `).join('');
}

// Render user skills
function renderUserSkills() {
    const container = document.getElementById('user-skills');

    if (userSkills.length === 0) {
        container.innerHTML = `
            <div class="empty-state">
                <p>You haven't created any custom skills yet.</p>
                <p style="color: var(--color-text-muted); font-size: 12px;">Click "+ CREATE SKILL" to create your first custom workflow.</p>
            </div>
        `;
        return;
    }

    container.innerHTML = userSkills.map(skill => `
        <div class="server-card" data-skill="${escapeHtml(skill.id)}">
            <div class="server-info">
                <div class="server-name">
                    ${escapeHtml(skill.name || skill.id)}
                    <span class="type-badge" style="background: var(--accent-green);">Custom</span>
                    <span class="type-badge" style="background: var(--color-accent-purple);">P${skill.priority}</span>
                </div>
                <div class="server-meta">
                    ${escapeHtml(skill.description || 'No description')}
                </div>
                <div class="server-meta" style="margin-top: 4px;">
                    ${skill.triggers?.keywords?.length ? `<span>Keywords: ${skill.triggers.keywords.slice(0, 3).join(', ')}${skill.triggers.keywords.length > 3 ? '...' : ''}</span>` : ''}
                    ${skill.triggers?.tool_ids?.length ? `<span>Tools: ${skill.triggers.tool_ids.join(', ')}</span>` : ''}
                </div>
            </div>
            <div class="server-actions">
                <button class="btn-server-action" onclick="editSkill('${escapeHtml(skill.id)}')">Edit</button>
                <button class="btn-server-action btn-delete" onclick="deleteSkill('${escapeHtml(skill.id)}')">Delete</button>
            </div>
        </div>
    `).join('');
}

// Open create skill modal
function openSkillModal(editMode = false) {
    document.getElementById('skill-modal').classList.remove('hidden');
    document.getElementById('skill-modal-title').textContent = editMode ? 'Edit Skill' : 'Create Skill';
    document.getElementById('skill-btn-submit').textContent = editMode ? 'Save Changes' : 'Create Skill';
    document.getElementById('skill-id').disabled = editMode;
    if (!editMode) {
        document.getElementById('skill-id').focus();
    }
}

// Close skill modal
function closeSkillModal() {
    document.getElementById('skill-modal').classList.add('hidden');
    document.getElementById('skill-form').reset();
    document.getElementById('skill-form-error').style.display = 'none';
    document.getElementById('skill-edit-id').value = '';
    document.getElementById('skill-id').disabled = false;
    document.getElementById('skill-priority').value = 5;
}

// View skill (read-only)
async function viewSkill(skillId, type) {
    try {
        const data = await fetchWithAuth(`/api/skills/${skillId}`);
        if (!data) return;

        viewingSkillId = skillId;
        document.getElementById('view-skill-title').textContent = data.name || skillId;
        document.getElementById('view-skill-content').textContent = data.content || 'No content';
        
        // Hide fork button for user skills
        document.getElementById('view-skill-btn-fork').style.display = type === 'system' ? 'block' : 'none';
        
        document.getElementById('view-skill-modal').classList.remove('hidden');
    } catch (error) {
        console.error('Error viewing skill:', error);
        alert('Failed to load skill: ' + error.message);
    }
}

// Close view modal
function closeViewSkillModal() {
    document.getElementById('view-skill-modal').classList.add('hidden');
    viewingSkillId = null;
}

// Edit user skill
async function editSkill(skillId) {
    try {
        const data = await fetchWithAuth(`/api/skills/${skillId}`);
        if (!data) return;

        document.getElementById('skill-edit-id').value = skillId;
        document.getElementById('skill-id').value = skillId;
        document.getElementById('skill-name').value = data.name || '';
        document.getElementById('skill-description').value = data.description || '';
        document.getElementById('skill-content').value = data.content || '';
        document.getElementById('skill-keywords').value = (data.triggers?.keywords || []).join(', ');
        document.getElementById('skill-tools').value = (data.triggers?.tool_ids || []).join(', ');
        document.getElementById('skill-priority').value = data.priority || 5;

        openSkillModal(true);
    } catch (error) {
        console.error('Error loading skill for edit:', error);
        alert('Failed to load skill: ' + error.message);
    }
}

// Fork system skill
async function forkSkill(skillId) {
    if (!confirm(`Fork "${skillId}" to create your own customizable version?`)) {
        return;
    }

    try {
        const result = await fetchWithAuth(`/api/skills/${skillId}/fork`, {
            method: 'POST'
        });

        if (result) {
            alert(`Skill forked! New skill: ${result.id}`);
            closeViewSkillModal();
            await loadSkills();
        }
    } catch (error) {
        console.error('Error forking skill:', error);
        alert('Failed to fork skill: ' + error.message);
    }
}

// Create or update skill
async function saveSkill(event) {
    event.preventDefault();

    const formError = document.getElementById('skill-form-error');
    formError.style.display = 'none';

    const editId = document.getElementById('skill-edit-id').value;
    const isEdit = !!editId;

    const keywords = document.getElementById('skill-keywords').value
        .split(',')
        .map(k => k.trim())
        .filter(k => k);

    const toolIds = document.getElementById('skill-tools').value
        .split(',')
        .map(t => t.trim())
        .filter(t => t);

    const data = {
        id: document.getElementById('skill-id').value.toLowerCase(),
        name: document.getElementById('skill-name').value,
        description: document.getElementById('skill-description').value || '',
        content: document.getElementById('skill-content').value,
        triggers: {
            keywords: keywords,
            tool_ids: toolIds,
            task_patterns: []
        },
        priority: parseInt(document.getElementById('skill-priority').value) || 5
    };

    try {
        if (isEdit) {
            await fetchWithAuth(`/api/skills/${editId}`, {
                method: 'PUT',
                body: JSON.stringify(data)
            });
        } else {
            await fetchWithAuth('/api/skills', {
                method: 'POST',
                body: JSON.stringify(data)
            });
        }

        closeSkillModal();
        await loadSkills();
    } catch (error) {
        console.error('Error saving skill:', error);
        formError.textContent = error.message || 'Failed to save skill';
        formError.style.display = 'block';
    }
}

// Delete user skill
async function deleteSkill(skillId) {
    if (!confirm(`Delete skill "${skillId}"? This cannot be undone.`)) {
        return;
    }

    try {
        await fetchWithAuth(`/api/skills/${skillId}`, {
            method: 'DELETE'
        });

        await loadSkills();
    } catch (error) {
        console.error('Error deleting skill:', error);
        alert('Failed to delete skill: ' + error.message);
    }
}

// Initialize skill event listeners
function initSkillEvents() {
    const addSkillBtn = document.getElementById('btn-add-skill');
    if (addSkillBtn) {
        addSkillBtn.addEventListener('click', () => openSkillModal(false));
    }

    document.getElementById('skill-modal-close')?.addEventListener('click', closeSkillModal);
    document.getElementById('skill-btn-cancel')?.addEventListener('click', closeSkillModal);
    document.getElementById('skill-form')?.addEventListener('submit', saveSkill);

    document.getElementById('view-skill-close')?.addEventListener('click', closeViewSkillModal);
    document.getElementById('view-skill-btn-close')?.addEventListener('click', closeViewSkillModal);
    document.getElementById('view-skill-btn-fork')?.addEventListener('click', () => {
        if (viewingSkillId) forkSkill(viewingSkillId);
    });

    // Close modals on overlay click
    document.getElementById('skill-modal')?.addEventListener('click', (e) => {
        if (e.target.classList.contains('modal-overlay')) closeSkillModal();
    });
    document.getElementById('view-skill-modal')?.addEventListener('click', (e) => {
        if (e.target.classList.contains('modal-overlay')) closeViewSkillModal();
    });
}

// ========================================
// POLYMARKET CONFIGURATION
// ========================================

let polymarketStatus = null;
let hyperliquidStatus = null;

// Load Polymarket status
async function loadPolymarketStatus() {
    try {
        const data = await fetchWithAuth('/api/polymarket/status');
        if (!data) return;

        polymarketStatus = data;
        // Re-render platform servers to include updated Polymarket status
        renderGlobalServers();
    } catch (error) {
        console.error('Error loading Polymarket status:', error);
        polymarketStatus = { configured: false, error: true };
        renderGlobalServers();
    }
}

// Bind Polymarket configure button (called after render)
function bindPolymarketConfigButton() {
    const btn = document.getElementById('pm-btn-configure');
    if (btn) {
        btn.addEventListener('click', openPolymarketModal);
    }
}

// Open Polymarket configuration modal
function openPolymarketModal() {
    const modal = document.getElementById('polymarket-modal');
    modal.classList.remove('hidden');

    // Pre-fill form with current status
    if (polymarketStatus) {
        document.getElementById('pm-demo-mode').checked = polymarketStatus.demo_mode;
        updatePmDemoLabel();
        updatePmWalletFields();

        if (!polymarketStatus.demo_mode) {
            // Show placeholders with masked addresses (don't clear the fields)
            if (polymarketStatus.wallet_address) {
                document.getElementById('pm-wallet-address').value = '';
                document.getElementById('pm-wallet-address').placeholder = polymarketStatus.wallet_address || '0x...';
            }
            if (polymarketStatus.proxy_wallet_address) {
                document.getElementById('pm-proxy-wallet-address').value = '';
                document.getElementById('pm-proxy-wallet-address').placeholder = polymarketStatus.proxy_wallet_address || '0x... (optional)';
            }
            // Set signature type
            const sigTypeSelect = document.getElementById('pm-signature-type');
            if (sigTypeSelect) {
                sigTypeSelect.value = polymarketStatus.signature_type ?? 2;
            }
        }

        // Fill trading limits
        if (polymarketStatus.trading_limits) {
            const limits = polymarketStatus.trading_limits;
            document.getElementById('pm-limit-order').value = limits.max_order_size_usd || 1000;
            document.getElementById('pm-limit-exposure').value = limits.max_total_exposure_usd || 5000;
            document.getElementById('pm-limit-per-market').value = limits.max_position_per_market_usd || 2000;
            document.getElementById('pm-autonomous-trading').checked = limits.enable_autonomous_trading || false;
        }

        // Show delete button if configured
        document.getElementById('pm-btn-delete').style.display = polymarketStatus.configured ? 'inline-block' : 'none';
    }
}

// Close Polymarket modal
function closePolymarketModal() {
    document.getElementById('polymarket-modal').classList.add('hidden');
    document.getElementById('polymarket-form').reset();
    document.getElementById('pm-form-error').style.display = 'none';
    document.getElementById('pm-demo-mode').checked = true;
    updatePmDemoLabel();
    updatePmWalletFields();
}

// Update demo mode label
function updatePmDemoLabel() {
    const checked = document.getElementById('pm-demo-mode').checked;
    document.getElementById('pm-demo-label').textContent = checked ? 'Yes' : 'No';
}

// Show/hide wallet fields based on demo mode
function updatePmWalletFields() {
    const demoMode = document.getElementById('pm-demo-mode').checked;
    document.getElementById('pm-wallet-fields').style.display = demoMode ? 'none' : 'block';
}

// Save Polymarket configuration
async function savePolymarketConfig(event) {
    event.preventDefault();

    const formError = document.getElementById('pm-form-error');
    formError.style.display = 'none';

    const demoMode = document.getElementById('pm-demo-mode').checked;

    const data = {
        demo_mode: demoMode
    };

    if (!demoMode) {
        const walletAddress = document.getElementById('pm-wallet-address').value.trim();
        const proxyWalletAddress = document.getElementById('pm-proxy-wallet-address')?.value.trim();
        const privateKey = document.getElementById('pm-private-key').value.trim();
        const signatureType = parseInt(document.getElementById('pm-signature-type')?.value) || 2;

        // For new setup, require wallet address and private key
        // For updates (already configured), allow partial updates
        if (!walletAddress && !polymarketStatus?.configured) {
            formError.textContent = 'Wallet address is required for trading mode';
            formError.style.display = 'block';
            return;
        }

        if (!privateKey && !polymarketStatus?.configured) {
            formError.textContent = 'Private key is required for trading mode';
            formError.style.display = 'block';
            return;
        }

        // Only include fields that have values (allows partial updates)
        if (walletAddress) {
            data.wallet_address = walletAddress;
        }
        if (proxyWalletAddress) {
            data.proxy_wallet_address = proxyWalletAddress;
        }
        if (privateKey) {
            data.private_key = privateKey;
        }
        data.signature_type = signatureType;

        data.trading_limits = {
            max_order_size_usd: parseInt(document.getElementById('pm-limit-order').value) || 1000,
            max_total_exposure_usd: parseInt(document.getElementById('pm-limit-exposure').value) || 5000,
            max_position_per_market_usd: parseInt(document.getElementById('pm-limit-per-market').value) || 2000,
            enable_autonomous_trading: document.getElementById('pm-autonomous-trading').checked
        };
    }

    try {
        await fetchWithAuth('/api/polymarket/configure', {
            method: 'POST',
            body: JSON.stringify(data)
        });

        closePolymarketModal();
        await loadPolymarketStatus();
    } catch (error) {
        console.error('Error saving Polymarket config:', error);
        formError.textContent = error.message || 'Failed to save configuration';
        formError.style.display = 'block';
    }
}

// Delete Polymarket credentials
async function deletePolymarketCredentials() {
    if (!confirm('Delete your Polymarket credentials? This cannot be undone.')) {
        return;
    }

    try {
        await fetchWithAuth('/api/polymarket/credentials', {
            method: 'DELETE'
        });

        closePolymarketModal();
        await loadPolymarketStatus();
    } catch (error) {
        console.error('Error deleting Polymarket credentials:', error);
        alert('Failed to delete credentials: ' + error.message);
    }
}

// Initialize Polymarket modal event listeners
function initPolymarketEvents() {
    // Modal events (configure button is bound dynamically in renderGlobalServers)
    document.getElementById('pm-modal-close')?.addEventListener('click', closePolymarketModal);
    document.getElementById('pm-btn-cancel')?.addEventListener('click', closePolymarketModal);
    document.getElementById('polymarket-form')?.addEventListener('submit', savePolymarketConfig);
    document.getElementById('pm-btn-delete')?.addEventListener('click', deletePolymarketCredentials);

    document.getElementById('pm-demo-mode')?.addEventListener('change', () => {
        updatePmDemoLabel();
        updatePmWalletFields();
    });

    // Close modal on overlay click
    document.getElementById('polymarket-modal')?.addEventListener('click', (e) => {
        if (e.target.classList.contains('modal-overlay')) closePolymarketModal();
    });
}

// ========================================
// HYPERLIQUID CONFIGURATION
// ========================================

// Render Hyperliquid card for Platform Servers section
function renderHyperliquidCard() {
    const status = hyperliquidStatus || {};
    let statusBadge, statusColor, modeText;

    if (status.configured) {
        if (status.enabled) {
            statusBadge = status.demo_mode ? 'Demo' : 'Trading';
            statusColor = 'var(--color-accent-green)';
        } else {
            statusBadge = 'Disabled';
            statusColor = 'var(--color-text-muted)';
        }
        modeText = status.demo_mode ? 'Read-only market data' : `Wallet: ${status.wallet_address || '...'}`;
    } else {
        statusBadge = 'Setup Required';
        statusColor = 'var(--color-accent-yellow)';
        modeText = 'Configure to access perpetuals & spot trading';
    }

    return `
        <div class="server-card" id="hyperliquid-card">
            <div class="server-info">
                <div class="server-name">
                    Hyperliquid
                    <span class="type-badge" style="background: ${statusColor}">${statusBadge}</span>
                </div>
                <div class="server-meta">
                    <span>Tool ID: <span class="tool-id">hyperliquid</span></span>
                    <span class="server-desc">${modeText}</span>
                </div>
            </div>
            <div class="server-actions">
                <button class="btn btn-secondary btn-sm" id="hl-btn-configure">Configure</button>
            </div>
        </div>
    `;
}

// Load Hyperliquid status
async function loadHyperliquidStatus() {
    try {
        const data = await fetchWithAuth('/api/hyperliquid/status');
        if (!data) return;

        hyperliquidStatus = data;
        renderGlobalServers();
    } catch (error) {
        console.error('Error loading Hyperliquid status:', error);
        hyperliquidStatus = { configured: false, error: true };
        renderGlobalServers();
    }
}

// Bind Hyperliquid configure button (called after render)
function bindHyperliquidConfigButton() {
    const btn = document.getElementById('hl-btn-configure');
    if (btn) {
        btn.addEventListener('click', openHyperliquidModal);
    }
}

// Open Hyperliquid configuration modal
function openHyperliquidModal() {
    const modal = document.getElementById('hyperliquid-modal');
    modal.classList.remove('hidden');

    // Pre-fill form with current status
    if (hyperliquidStatus) {
        document.getElementById('hl-demo-mode').checked = hyperliquidStatus.demo_mode !== false;
        document.getElementById('hl-testnet').checked = hyperliquidStatus.testnet !== false;
        updateHlDemoLabel();
        updateHlWalletFields();

        if (!hyperliquidStatus.demo_mode) {
            if (hyperliquidStatus.wallet_address) {
                document.getElementById('hl-wallet-address').value = '';
                document.getElementById('hl-wallet-address').placeholder = hyperliquidStatus.wallet_address || '0x...';
            }
            if (hyperliquidStatus.agent_wallet_address) {
                document.getElementById('hl-agent-wallet-address').value = '';
                document.getElementById('hl-agent-wallet-address').placeholder = hyperliquidStatus.agent_wallet_address || '0x... (optional)';
            }
        }

        // Fill trading limits
        if (hyperliquidStatus.trading_limits) {
            const limits = hyperliquidStatus.trading_limits;
            document.getElementById('hl-limit-order').value = limits.max_order_size_usd || 1000;
            document.getElementById('hl-limit-leverage').value = limits.max_leverage || 5;
            document.getElementById('hl-autonomous-trading').checked = limits.enable_autonomous_trading || false;
        }

        // Show delete button if configured
        document.getElementById('hl-btn-delete').style.display = hyperliquidStatus.configured ? 'inline-block' : 'none';
    }
}

// Close Hyperliquid modal
function closeHyperliquidModal() {
    document.getElementById('hyperliquid-modal').classList.add('hidden');
    document.getElementById('hyperliquid-form').reset();
    document.getElementById('hl-form-error').style.display = 'none';
    document.getElementById('hl-demo-mode').checked = true;
    updateHlDemoLabel();
    updateHlWalletFields();
}

// Update demo mode label
function updateHlDemoLabel() {
    const checked = document.getElementById('hl-demo-mode').checked;
    document.getElementById('hl-demo-label').textContent = checked ? 'Yes' : 'No';
}

// Show/hide wallet fields based on demo mode
function updateHlWalletFields() {
    const demoMode = document.getElementById('hl-demo-mode').checked;
    document.getElementById('hl-wallet-fields').style.display = demoMode ? 'none' : 'block';
}

// Save Hyperliquid configuration
async function saveHyperliquidConfig(event) {
    event.preventDefault();

    const formError = document.getElementById('hl-form-error');
    formError.style.display = 'none';

    const demoMode = document.getElementById('hl-demo-mode').checked;
    const testnet = document.getElementById('hl-testnet').checked;

    const data = {
        demo_mode: demoMode,
        testnet: testnet
    };

    if (!demoMode) {
        const walletAddress = document.getElementById('hl-wallet-address').value.trim();
        const agentWalletAddress = document.getElementById('hl-agent-wallet-address')?.value.trim();
        const privateKey = document.getElementById('hl-private-key').value.trim();
        const agentPrivateKey = document.getElementById('hl-agent-private-key')?.value.trim();

        if (!walletAddress && !hyperliquidStatus?.configured) {
            formError.textContent = 'Wallet address is required for trading mode';
            formError.style.display = 'block';
            return;
        }

        if (!privateKey && !hyperliquidStatus?.configured) {
            formError.textContent = 'Private key is required for trading mode';
            formError.style.display = 'block';
            return;
        }

        if (walletAddress) data.wallet_address = walletAddress;
        if (agentWalletAddress) data.agent_wallet_address = agentWalletAddress;
        if (privateKey) data.private_key = privateKey;
        if (agentPrivateKey) data.agent_wallet_private_key = agentPrivateKey;

        data.max_order_size_usd = parseInt(document.getElementById('hl-limit-order').value) || 1000;
        data.max_leverage = parseInt(document.getElementById('hl-limit-leverage').value) || 5;
        data.enable_autonomous_trading = document.getElementById('hl-autonomous-trading').checked;
    }

    try {
        await fetchWithAuth('/api/hyperliquid/configure', {
            method: 'POST',
            body: JSON.stringify(data)
        });

        closeHyperliquidModal();
        await loadHyperliquidStatus();
    } catch (error) {
        console.error('Error saving Hyperliquid config:', error);
        formError.textContent = error.message || 'Failed to save configuration';
        formError.style.display = 'block';
    }
}

// Delete Hyperliquid credentials
async function deleteHyperliquidCredentials() {
    if (!confirm('Delete your Hyperliquid credentials? This cannot be undone.')) {
        return;
    }

    try {
        await fetchWithAuth('/api/hyperliquid/credentials', {
            method: 'DELETE'
        });

        closeHyperliquidModal();
        await loadHyperliquidStatus();
    } catch (error) {
        console.error('Error deleting Hyperliquid credentials:', error);
        alert('Failed to delete credentials: ' + error.message);
    }
}

// Initialize Hyperliquid modal event listeners
function initHyperliquidEvents() {
    document.getElementById('hl-modal-close')?.addEventListener('click', closeHyperliquidModal);
    document.getElementById('hl-btn-cancel')?.addEventListener('click', closeHyperliquidModal);
    document.getElementById('hyperliquid-form')?.addEventListener('submit', saveHyperliquidConfig);
    document.getElementById('hl-btn-delete')?.addEventListener('click', deleteHyperliquidCredentials);

    document.getElementById('hl-demo-mode')?.addEventListener('change', () => {
        updateHlDemoLabel();
        updateHlWalletFields();
    });

    // Close modal on overlay click
    document.getElementById('hyperliquid-modal')?.addEventListener('click', (e) => {
        if (e.target.classList.contains('modal-overlay')) closeHyperliquidModal();
    });
}

// ========================================
// INITIALIZATION
// ========================================

// Initialize
async function init() {
    // Initialize tab navigation first
    initTabNavigation();

    // One probe decides whether the API service exists in this deployment;
    // without it, skip every API call and render the honest state instead.
    apiServiceAvailable = await probeApiService();
    if (!apiServiceAvailable) {
        renderApiUnavailable();
    } else {
        // Load all data
        await Promise.all([
            loadSettings(),
            loadAvailableServers(),
            loadUserServers(),
            loadSkills(),
            loadPolymarketStatus(),
            loadHyperliquidStatus()
        ]);
    }

    // Setup MCP event listeners
    document.getElementById('btn-add-server').addEventListener('click', openAddModal);
    document.getElementById('modal-close').addEventListener('click', closeAddModal);
    document.getElementById('btn-cancel').addEventListener('click', closeAddModal);
    document.getElementById('add-server-form').addEventListener('submit', addServer);
    document.getElementById('auth-method').addEventListener('change', updateApiKeyVisibility);

    // Setup Skill event listeners
    initSkillEvents();

    // Setup Polymarket event listeners
    initPolymarketEvents();

    // Setup Hyperliquid event listeners
    initHyperliquidEvents();

    // Close modal on overlay click
    document.getElementById('add-server-modal').addEventListener('click', (e) => {
        if (e.target.classList.contains('modal-overlay')) {
            closeAddModal();
        }
    });

    // Settings toggles
    document.getElementById('mcp-enabled').addEventListener('change', (e) => {
        saveSettings('mcp_enabled', e.target.checked);
    });

    document.getElementById('include-global').addEventListener('change', (e) => {
        saveSettings('include_global_servers', e.target.checked);
    });
}

// Start when page loads
document.addEventListener('DOMContentLoaded', init);
