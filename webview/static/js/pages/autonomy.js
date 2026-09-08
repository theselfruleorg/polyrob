/**
 * Autonomy page (030 WS-C C3) — goal board + cron + the owner pause (031).
 *
 * De-inlined from autonomy.html (WS-A A2 direction: external page scripts so
 * the CSP can drop 'unsafe-inline'). All markup is DOM-built — never string
 * onclick handlers (the S2 stored-XSS pattern is banned).
 *
 * Every write goes through the C3 endpoints, which are thin plumbing over the
 * SAME primitives the CLI/REPL/Telegram owner verbs call. The page renders the
 * server's honest messages verbatim — a halt the runtime cannot see says NOT
 * effective, never a green checkmark.
 */
(function () {
    const root = document.getElementById('autonomy-root');
    const READ_ONLY = !!(root && root.dataset.readOnly);
    const HALT_CONTROLS = !!(root && root.dataset.haltControls);

    function el(tag, cls, text) {
        const node = document.createElement(tag);
        if (cls) node.className = cls;
        if (text != null) node.textContent = text;
        return node;
    }

    async function postJSON(url, body) {
        const r = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body || {}),
        });
        const out = await r.json().catch(() => ({}));
        return { status: r.status, ok: r.ok, body: out };
    }

    // ------------------------------------------------------------------ //
    // Pause (031) — the ONE pause record every loop reads; halt == pause(all)
    // ------------------------------------------------------------------ //

    const SCOPES = ['trading', 'streams', 'planner', 'cron', 'social', 'oversight', 'pings'];
    const haltState = document.getElementById('halt-state');
    const haltActions = document.getElementById('halt-actions');
    const haltMsg = document.getElementById('halt-msg');

    function showHaltMsg(text, isError) {
        haltMsg.textContent = text || '';
        haltMsg.style.display = text ? '' : 'none';
        haltMsg.classList.toggle('webgate-note--error', !!isError);
    }

    function hhmm(ts) {
        if (!ts) return '?';
        return new Date(ts * 1000).toISOString().slice(11, 16) + 'Z';
    }

    function renderHaltState(state) {
        haltState.innerHTML = '';
        if (!state) {
            haltState.appendChild(el('span', 'webgate-empty', 'Pause state unavailable.'));
            return;
        }
        const scopes = state.scopes || [];
        const words = scopes.indexOf('all') >= 0 ? 'everything' : scopes.join(', ');
        const badge = el('span',
            state.paused ? 'webgate-halt-badge halted' : 'webgate-halt-badge running',
            state.paused ? '⏸ PAUSED (' + words + ')' : '▶ RUNNING');
        haltState.appendChild(badge);
        let desc;
        if (state.paused) {
            desc = ' — since ' + hhmm(state.since) + ' by ' + (state.set_by || state.source || '?')
                + ' via ' + (state.via || '-')
                + (state.until ? ', auto-resumes ' + hhmm(state.until) : '')
                + '. Still on: chat, crash/security/credit alerts.';
        } else {
            desc = ' — autonomous dispatch and agent spend are permitted (per posture/caps).';
        }
        haltState.appendChild(el('span', 'text-muted text-sm', desc));
        if (state.env_halt) {
            haltState.appendChild(el('div', 'text-muted text-sm',
                'AUTONOMY_HALT is set in the environment — Resume cannot clear that '
                + '(edit the env file, then restart).'));
        }
        renderHaltButtons(state);
    }

    async function pauseCall(url, body, btn) {
        btn.disabled = true;
        try {
            const r = await postJSON(url, body);
            showHaltMsg(r.body.message || r.body.detail || ('error ' + r.status),
                        !(r.ok && r.body.ok));
        } catch (e) {
            showHaltMsg('request failed: ' + e, true);
        }
        btn.disabled = false;
        loadHalt();
    }

    function renderHaltButtons(state) {
        haltActions.innerHTML = '';
        if (!HALT_CONTROLS) return;
        const all = el('button', 'form-control', 'Pause everything');
        all.addEventListener('click', () => {
            if (!window.confirm('Pause ALL autonomous work (dispatch, streams, cron, '
                    + 'self-wake, social, the dev loops)?')) return;
            pauseCall('/api/webgate/pause', {}, all);
        });
        haltActions.appendChild(all);

        const sel = document.createElement('select');
        sel.className = 'form-control';
        sel.multiple = true;
        sel.size = 3;
        SCOPES.forEach((s) => {
            const o = document.createElement('option');
            o.value = s; o.textContent = s;
            sel.appendChild(o);
        });
        haltActions.appendChild(sel);
        const scoped = el('button', 'form-control', 'Pause selected');
        scoped.addEventListener('click', () => {
            const chosen = Array.from(sel.selectedOptions).map((o) => o.value);
            if (!chosen.length) { showHaltMsg('pick at least one scope', true); return; }
            pauseCall('/api/webgate/pause', { scopes: chosen }, scoped);
        });
        haltActions.appendChild(scoped);

        if (state.paused) {
            const res = el('button', 'form-control', 'Resume');
            res.addEventListener('click', () => pauseCall('/api/webgate/resume', {}, res));
            haltActions.appendChild(res);
        }
    }

    async function loadHalt() {
        try {
            const r = await fetch('/api/webgate/pause');
            renderHaltState(await r.json());
        } catch (e) {
            renderHaltState(null);
        }
    }

    // ------------------------------------------------------------------ //
    // Goals — read + the four C3 write verbs (pause/resume/retry/cancel)
    // ------------------------------------------------------------------ //

    // Which verbs make sense for a status (the server enforces the real
    // transition table — this only avoids rendering a doomed button).
    function goalVerbs(status) {
        if (status === 'blocked') return ['resume', 'retry', 'cancel'];
        if (status === 'ready' || status === 'running' || status === 'triage') {
            return ['pause', 'cancel'];
        }
        return [];
    }

    function goalActionRow(g, reload) {
        const actions = el('div', 'webgate-actions');
        const msg = el('span', 'webgate-action-msg');
        goalVerbs(g.status).forEach(verb => {
            const b = el('button', 'form-control', verb);
            b.addEventListener('click', async () => {
                if (verb === 'cancel' && !window.confirm(
                        'Cancel goal "' + g.title + '"?')) return;
                b.disabled = true;
                msg.textContent = '…'; msg.className = 'webgate-action-msg';
                try {
                    const r = await postJSON(
                        '/api/webgate/goals/' + encodeURIComponent(g.id)
                        + '/' + verb);
                    const out = r.body || {};
                    msg.textContent = (out.message || out.detail || ('error ' + r.status))
                        + (out.warning ? ' — ' + out.warning : '');
                    msg.className = 'webgate-action-msg '
                        + (r.ok && out.ok ? 'ok' : 'err');
                    if (r.ok && out.ok) setTimeout(reload, 900);
                } catch (e) {
                    msg.textContent = 'request failed';
                    msg.className = 'webgate-action-msg err';
                }
                b.disabled = false;
            });
            actions.appendChild(b);
        });
        actions.appendChild(msg);
        return actions;
    }

    async function loadGoals() {
        const listEl = document.getElementById('goals-list');
        try {
            const body = await (await fetch('/api/webgate/goals')).json();
            listEl.innerHTML = '';
            if (body.enabled === false) {
                listEl.appendChild(el('div', 'webgate-empty',
                    'Goals disabled (set GOALS_ENABLED).'));
                return;
            }
            if (!body.goals.length) {
                listEl.appendChild(el('div', 'webgate-empty', 'No goals.'));
                return;
            }
            body.goals.forEach(g => {
                const item = el('div', 'webgate-item');
                item.appendChild(el('span', 'badge', g.status));
                item.appendChild(el('strong', null, g.title));
                const idEl = el('span', 'webgate-row-id', ' ' + String(g.id).slice(0, 8));
                item.appendChild(idEl);
                if (g.body) item.appendChild(el('div', 'text-muted text-sm', g.body));
                if (!READ_ONLY) item.appendChild(goalActionRow(g, loadGoals));
                listEl.appendChild(item);
            });
        } catch (e) {
            listEl.innerHTML = '';
            listEl.appendChild(el('div', 'webgate-empty', 'Could not load goals.'));
        }
    }

    // ------------------------------------------------------------------ //
    // Cron — read + cancel
    // ------------------------------------------------------------------ //

    function cronActionRow(j, reload) {
        const actions = el('div', 'webgate-actions');
        const msg = el('span', 'webgate-action-msg');
        const b = el('button', 'form-control', 'cancel');
        b.addEventListener('click', async () => {
            if (!window.confirm('Cancel cron job "' + j.task + '"?')) return;
            b.disabled = true;
            msg.textContent = '…'; msg.className = 'webgate-action-msg';
            try {
                const r = await postJSON(
                    '/api/webgate/cron/' + encodeURIComponent(j.id) + '/cancel');
                const out = r.body || {};
                msg.textContent = out.message || out.detail || ('error ' + r.status);
                msg.className = 'webgate-action-msg ' + (r.ok && out.ok ? 'ok' : 'err');
                if (r.ok && out.ok) setTimeout(reload, 900);
            } catch (e) {
                msg.textContent = 'request failed';
                msg.className = 'webgate-action-msg err';
            }
            b.disabled = false;
        });
        actions.appendChild(b);
        actions.appendChild(msg);
        return actions;
    }

    async function loadCron() {
        const listEl = document.getElementById('cron-list');
        try {
            const body = await (await fetch('/api/webgate/cron')).json();
            listEl.innerHTML = '';
            if (body.enabled === false) {
                listEl.appendChild(el('div', 'webgate-empty',
                    'Cron disabled (set CRON_ENABLED).'));
                return;
            }
            if (body.error) {
                // 030 D4: a failed read is NOT "no cron jobs".
                const err = el('div', 'error-message error-message--block',
                    'cron unavailable: ' + body.error);
                listEl.appendChild(err);
                return;
            }
            if (!body.jobs.length) {
                listEl.appendChild(el('div', 'webgate-empty', 'No cron jobs.'));
                return;
            }
            body.jobs.forEach(j => {
                const item = el('div', 'webgate-item');
                item.appendChild(el('span', 'badge', j.status));
                item.appendChild(el('strong', null, j.task));
                const meta = el('div', 'text-muted text-sm',
                    j.schedule_spec + (j.next_run_at ? ' · next: ' + j.next_run_at : ''));
                item.appendChild(meta);
                const cancellable = j.status !== 'cancelled' && j.status !== 'done';
                if (!READ_ONLY && cancellable) {
                    item.appendChild(cronActionRow(j, loadCron));
                }
                listEl.appendChild(item);
            });
        } catch (e) {
            listEl.innerHTML = '';
            listEl.appendChild(el('div', 'webgate-empty', 'Could not load cron.'));
        }
    }

    loadHalt(); loadGoals(); loadCron();
    // 019 P3: auto-refresh so a goal that starts RUNNING (or a cron job that
    // fires, or a halt flipped from another seat) shows without a reload.
    setInterval(function () {
        if (document.visibilityState === 'visible') {
            loadHalt(); loadGoals(); loadCron();
        }
    }, 10000);
})();
