/**
 * Config page (030 WS-C C3 — the UI for the 018 P3 endpoints).
 *
 * GET  /api/webgate/config?query=…        — searchable list, both namespaces
 * GET  /api/webgate/config/{key}/explain  — provenance chain on demand
 * PATCH /api/webgate/config/{key}         — write per the API's own rules
 *
 * The page renders the server's refusals honestly: CONSOLE_UNWRITABLE flags
 * show a "CLI-only" badge and no editor (the server 403s them regardless);
 * env-flag edits render only when the console posture allows them
 * (data-flags-writable, mirroring the PATCH endpoint's rule); guarded prefs
 * confirm before applying. External file by design (the CSP is being
 * tightened); DOM-built rows — never string onclick handlers.
 */
(function () {
    const root = document.getElementById('config-root');
    const READ_ONLY = !!(root && root.dataset.readOnly);
    const FLAGS_WRITABLE = !!(root && root.dataset.flagsWritable);
    const listEl = document.getElementById('config-list');
    const countEl = document.getElementById('config-count');
    const queryEl = document.getElementById('config-query');

    function el(tag, cls, text) {
        const node = document.createElement(tag);
        if (cls) node.className = cls;
        if (text != null) node.textContent = text;
        return node;
    }

    function badge(text, cls) {
        return el('span', 'webgate-cfg-badge' + (cls ? ' ' + cls : ''), text);
    }

    function editable(item) {
        if (READ_ONLY || !item.console_writable) return false;
        if (item.namespace === 'pref') return true;
        return FLAGS_WRITABLE; // env flags need the owner console posture
    }

    async function saveValue(item, input, status) {
        const value = input.value;
        if (item.sensitivity === 'guarded' && !window.confirm(
                item.key + ' is a guarded setting.\n\nApply '
                + JSON.stringify(value) + '?')) {
            status.textContent = 'cancelled';
            status.className = 'webgate-action-msg';
            return;
        }
        status.textContent = '…'; status.className = 'webgate-action-msg';
        const body = { value: value };
        if (item.sensitivity === 'guarded') body.confirm = true;
        try {
            const r = await fetch('/api/webgate/config/'
                + encodeURIComponent(item.key), {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            const out = await r.json().catch(() => ({}));
            if (r.status === 202 && out.ok) {
                status.textContent = 'queued for review — see /pending';
                status.className = 'webgate-action-msg';
            } else if (r.ok && out.ok) {
                status.textContent = out.message
                    || ('saved' + (out.applies ? ' (' + out.applies + ')' : ''));
                status.className = 'webgate-action-msg ok';
            } else {
                // Render the server's refusal verbatim — never invent success.
                status.textContent = out.message || out.error || out.detail
                    || ('error ' + r.status);
                status.className = 'webgate-action-msg err';
            }
        } catch (e) {
            status.textContent = 'request failed';
            status.className = 'webgate-action-msg err';
        }
    }

    async function toggleExplain(item, holder, btn) {
        if (holder.dataset.loaded) {
            holder.style.display = holder.style.display === 'none' ? '' : 'none';
            return;
        }
        btn.disabled = true;
        try {
            const r = await fetch('/api/webgate/config/'
                + encodeURIComponent(item.key) + '/explain');
            const out = await r.json().catch(() => ({}));
            holder.innerHTML = '';
            if (!r.ok) {
                holder.appendChild(el('div', 'webgate-empty',
                    out.error || ('explain failed (' + r.status + ')')));
            } else {
                if (out.description) {
                    holder.appendChild(el('div', 'webgate-cfg-desc', out.description));
                }
                const meta = el('div', 'text-muted text-sm',
                    'kind: ' + (out.kind || '?')
                    + ' · applies: ' + (out.applies || '?')
                    + ' · sensitivity: ' + (out.sensitivity || '?')
                    + ' · enforcement: ' + (out.enforcement || '?'));
                holder.appendChild(meta);
                const chain = el('div', 'webgate-cfg-chain');
                (out.chain || []).forEach(link => {
                    chain.appendChild(el('div', 'webgate-cfg-chain-row',
                        link.origin + ' → ' + link.value));
                });
                if (!(out.chain || []).length) {
                    chain.appendChild(el('div', 'text-muted text-sm',
                        '(no provenance chain)'));
                }
                holder.appendChild(chain);
            }
            holder.dataset.loaded = '1';
            holder.style.display = '';
        } catch (e) {
            holder.textContent = 'explain failed';
            holder.style.display = '';
        }
        btn.disabled = false;
    }

    function row(item) {
        const r = el('div', 'webgate-item webgate-cfg-row');

        const head = el('div', 'webgate-cfg-head');
        head.appendChild(el('strong', 'webgate-cfg-key', item.key));
        head.appendChild(badge(item.namespace === 'pref' ? 'pref' : 'flag'));
        if (item.group) head.appendChild(badge(item.group));
        if (item.sensitivity === 'guarded') head.appendChild(badge('guarded', 'guarded'));
        if (item.secret) head.appendChild(badge('secret', 'guarded'));
        if (!item.console_writable) head.appendChild(badge('CLI-only', 'cli-only'));
        r.appendChild(head);

        const valueLine = el('div', 'webgate-cfg-value');
        valueLine.appendChild(el('span', null, item.value === '' ? '(unset)' : item.value));
        valueLine.appendChild(el('span', 'webgate-cfg-source', ' · ' + item.source));
        r.appendChild(valueLine);

        if (item.description) {
            r.appendChild(el('div', 'text-muted text-sm', item.description));
        }

        const actions = el('div', 'webgate-actions');
        const status = el('span', 'webgate-action-msg');
        const explainHolder = el('div', 'webgate-cfg-explain');
        explainHolder.style.display = 'none';

        const explainBtn = el('button', 'form-control', 'Explain');
        explainBtn.addEventListener('click',
            () => toggleExplain(item, explainHolder, explainBtn));
        actions.appendChild(explainBtn);

        if (editable(item)) {
            const input = el('input', 'form-control webgate-cfg-input');
            input.type = 'text';
            if (item.secret) {
                input.placeholder = '(masked — enter a new value to replace)';
            } else {
                input.value = item.value;
            }
            const save = el('button', 'form-control', 'Save');
            save.addEventListener('click', () => saveValue(item, input, status));
            actions.appendChild(input);
            actions.appendChild(save);
        } else if (!READ_ONLY && !item.console_writable) {
            // Honest refusal, up front — the server 403s these at any posture.
            actions.appendChild(el('span', 'text-muted text-sm',
                'not writable from the console — use the local CLI '
                + '(polyrob config set)'));
        } else if (!READ_ONLY && item.namespace === 'flag' && !FLAGS_WRITABLE) {
            actions.appendChild(el('span', 'text-muted text-sm',
                'env-flag writes need the owner console (local/own_ops)'));
        }
        actions.appendChild(status);
        r.appendChild(actions);
        r.appendChild(explainHolder);
        return r;
    }

    let seq = 0;
    async function load() {
        const mySeq = ++seq;
        const q = (queryEl.value || '').trim();
        try {
            const r = await fetch('/api/webgate/config'
                + (q ? '?query=' + encodeURIComponent(q) : ''));
            const body = await r.json();
            if (mySeq !== seq) return; // a newer search superseded this one
            const items = body.settings || [];
            listEl.innerHTML = '';
            countEl.style.display = '';
            countEl.textContent = items.length + ' setting(s)'
                + (q ? ' matching "' + q + '"' : '');
            if (!items.length) {
                listEl.appendChild(el('div', 'webgate-empty',
                    'No settings match' + (q ? ' "' + q + '"' : '') + '.'));
                return;
            }
            let lastGroup = null;
            items.forEach(item => {
                const group = (item.namespace === 'pref' ? 'preferences · ' : '')
                    + (item.group || 'other');
                if (group !== lastGroup) {
                    listEl.appendChild(el('div', 'webgate-cfg-group', group));
                    lastGroup = group;
                }
                listEl.appendChild(row(item));
            });
        } catch (e) {
            if (mySeq !== seq) return;
            listEl.innerHTML = '';
            listEl.appendChild(el('div', 'webgate-empty',
                'Could not load the config catalog.'));
        }
    }

    let timer = null;
    queryEl.addEventListener('input', () => {
        clearTimeout(timer);
        timer = setTimeout(load, 250);
    });
    load();
})();
