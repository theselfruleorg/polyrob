import { escapeHtml } from '/static/js/ui-utils.js?v=2';

/* IRC-style session catalog - Load and display sessions in terminal-style list */

// Status icons and colors (paused removed - use cancelled for interruption)
const STATUS_CONFIG = {
    'completed': { icon: '✓', color: '#56e2c2', label: 'DONE' },
    'running': { icon: '⟳', color: '#56c2ff', label: 'ACTIVE' },
    'cancelled': { icon: '⏹', color: '#ffa500', label: 'STOPPED' },
    'suspended': { icon: '⏸', color: '#888', label: 'SUSPENDED' },
    'error': { icon: '✗', color: '#ff6b6b', label: 'ERROR' },
    'failed': { icon: '✗', color: '#ff6b6b', label: 'FAILED' },
    'pending': { icon: '○', color: '#888', label: 'PENDING' }
};

function getStatusDisplay(status) {
    const config = STATUS_CONFIG[status] || STATUS_CONFIG['completed'];
    return `<span style="color: ${config.color}; font-weight: 600;">[${config.icon} ${config.label}]</span>`;
}

function renderIRCSession(session) {
    const statusDisplay = getStatusDisplay(session.status || 'completed');
    const modelInfo = `<span class="irc-model">[${escapeHtml(session.model || 'unknown')}/${escapeHtml(session.provider || 'unknown')}]</span>`;
    const sessionId = `<a href="/session/${session.id}" class="irc-session-id">${session.id.substring(0, 8)}</a>`;
    const stepInfo = `<span class="irc-steps">(${session.steps || 0} steps)</span>`;
    const taskText = escapeHtml(session.task || 'No task description');

    return `
        <div class="irc-session-line" onclick="window.location.href='/session/${session.id}'">
            <span class="irc-timestamp">[${session.created || 'Unknown'}]</span>
            ${statusDisplay}
            ${modelInfo}
            ${sessionId}
            <span class="irc-arrow">→</span>
            <span class="irc-task">${taskText}</span>
            ${stepInfo}
        </div>
    `;
}

fetch('/api/sessions')
    .then((r) => r.json())
    .then((data) => {
        const container = document.getElementById('sessions-container');
        if (!container) return;

        container.innerHTML = '';

        // Handle both array (old format) and object (new format)
        const sessions = Array.isArray(data) ? data : (data.sessions || []);

        if (sessions.length === 0) {
            container.innerHTML = `
                <div class="empty-state">
                    <div class="empty-state-icon">📋</div>
                    <div class="empty-state-text">No sessions yet</div>
                    <div class="empty-state-subtext">Start a new conversation to create your first session</div>
                    <a href="/" class="btn-new-session">Start Chat</a>
                </div>
            `;
            return;
        }

        // Create IRC-style catalog
        const catalogDiv = document.createElement('div');
        catalogDiv.className = 'irc-catalog';

        // Add session lines
        sessions.forEach((session) => {
            const lineDiv = document.createElement('div');
            lineDiv.innerHTML = renderIRCSession(session);
            catalogDiv.appendChild(lineDiv.firstElementChild);
        });

        container.appendChild(catalogDiv);
    })
    .catch((err) => {
        console.error('Failed to load sessions', err);
        const container = document.getElementById('sessions-container');
        if (container) {
            container.innerHTML = `
                <div class="error-state">
                    <div>Failed to load sessions</div>
                    <div class="text-muted text-sm">${err.message}</div>
                </div>
            `;
        }
    });
