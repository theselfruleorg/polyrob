/**
 * Finance page — Invoices section (030 WS-C C3).
 *
 * Lists the agent's receivables via GET /api/webgate/invoices (the same
 * tenant-scoped modules.x402.invoicing seam the CLI/REPL/Telegram listings
 * read) and offers Settle → POST /api/webgate/invoices/{id}/settle (owner
 * attestation, mirroring `polyrob owner settle`). External file by design
 * (CSP tightening); DOM-built rows — never string onclick handlers.
 */
(function () {
    const section = document.getElementById('invoices-section');
    if (!section) return;
    const READ_ONLY = !!section.dataset.readOnly;
    const listEl = document.getElementById('invoices-list');
    const noteEl = document.getElementById('invoices-note');
    const statusSel = document.getElementById('invoice-status');

    function el(tag, cls, text) {
        const node = document.createElement(tag);
        if (cls) node.className = cls;
        if (text != null) node.textContent = text;
        return node;
    }

    function money(n) { return '$' + (Number(n) || 0).toFixed(2); }

    function showNote(text) {
        noteEl.textContent = text || '';
        noteEl.style.display = text ? '' : 'none';
    }

    async function settle(inv, btn, msg) {
        // The tx hash is optional — a blank answer settles by attestation only.
        const tx = window.prompt(
            'Settle invoice ' + inv.request_id + ' (' + money(inv.amount_usd)
            + ')?\n\nOptional: paste the on-chain tx hash (leave empty for '
            + 'attestation only).', '');
        if (tx === null) return; // cancelled
        btn.disabled = true;
        msg.textContent = '…'; msg.className = 'webgate-action-msg';
        try {
            const r = await fetch('/api/webgate/invoices/'
                + encodeURIComponent(inv.request_id) + '/settle', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(tx.trim() ? { tx_hash: tx.trim() } : {}),
            });
            const out = await r.json().catch(() => ({}));
            msg.textContent = out.message || out.detail || ('error ' + r.status);
            msg.className = 'webgate-action-msg ' + (r.ok && out.ok ? 'ok' : 'err');
            if (out.note) showNote('⚠ ' + out.note);
            if (r.ok && out.ok) setTimeout(load, 900);
        } catch (e) {
            msg.textContent = 'request failed';
            msg.className = 'webgate-action-msg err';
        }
        btn.disabled = false;
    }

    function row(inv) {
        const item = el('div', 'webgate-item');
        item.appendChild(el('span', 'badge', inv.status));
        item.appendChild(el('strong', null, money(inv.amount_usd)));
        item.appendChild(el('span', null, ' — ' + (inv.purpose || '(no purpose)')));
        item.appendChild(el('span', 'webgate-row-id', ' ' + String(inv.request_id)));
        const meta = [];
        if (inv.payer_contact) meta.push('billed to ' + inv.payer_contact);
        if (inv.created_at) meta.push('created ' + inv.created_at);
        if (inv.completed_at) meta.push('completed ' + inv.completed_at);
        if (meta.length) item.appendChild(el('div', 'text-muted text-sm', meta.join(' · ')));
        if (!READ_ONLY && inv.status === 'pending') {
            const actions = el('div', 'webgate-actions');
            const msg = el('span', 'webgate-action-msg');
            const btn = el('button', 'form-control', 'Settle');
            btn.addEventListener('click', () => settle(inv, btn, msg));
            actions.appendChild(btn);
            actions.appendChild(msg);
            item.appendChild(actions);
        }
        return item;
    }

    async function load() {
        try {
            const status = statusSel ? statusSel.value : '';
            const r = await fetch('/api/webgate/invoices'
                + (status ? '?status=' + encodeURIComponent(status) : ''));
            const body = await r.json();
            listEl.innerHTML = '';
            showNote(body.note ? '⚠ ' + body.note : '');
            if (body.error) {
                // 030 D4: a failed read is NOT "no invoices".
                listEl.appendChild(el('div', 'webgate-empty',
                    'Invoices unavailable: ' + body.error));
                return;
            }
            if (!body.invoices || !body.invoices.length) {
                listEl.appendChild(el('div', 'webgate-empty',
                    'No invoices' + (status ? ' (' + status + ')' : '') + '.'));
                return;
            }
            body.invoices.forEach(inv => listEl.appendChild(row(inv)));
        } catch (e) {
            listEl.innerHTML = '';
            listEl.appendChild(el('div', 'webgate-empty', 'Could not load invoices.'));
        }
    }

    if (statusSel) statusSel.addEventListener('change', load);
    load();
})();
