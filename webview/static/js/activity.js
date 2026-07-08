/* Global activity terminal (/activity).
 *
 * Data flow: GET /api/activity/backfill once, then Socket.IO room "activity"
 * (join_activity -> activity_snapshot + activity_event). Dedup by event id,
 * client buffer capped, DOM capped. No frameworks, no inline scripts (CSP).
 */
(function () {
    'use strict';

    var MAX_BUFFER = 5000;
    var MAX_DOM = 2000;
    var MAX_VISIBLE_KINDS = 8;

    var state = {
        seen: new Set(),
        kinds: new Set(),
        kindsOff: new Set(),
        kindsExpanded: false,
        paused: false,
        follow: true,
        text: '',
        pending: [],
        count: 0,
        lastDay: '',
        panelSession: null,
        hadConnection: false,
        reconnected: false
    };

    var els = {};

    function $(id) { return document.getElementById(id); }

    function p2(n) { return (n < 10 ? '0' : '') + n; }

    function fmtTime(ts) {
        var d = new Date((ts || 0) * 1000);
        return p2(d.getHours()) + ':' + p2(d.getMinutes()) + ':' + p2(d.getSeconds());
    }

    function fmtDay(ts) {
        var d = new Date((ts || 0) * 1000);
        return d.getFullYear() + '-' + p2(d.getMonth() + 1) + '-' + p2(d.getDate());
    }

    function fmtFull(ts) {
        var d = new Date((ts || 0) * 1000);
        return fmtDay(ts) + ' ' + fmtTime(ts) + ' (' + d.toISOString() + ')';
    }

    function matchesFilters(ev) {
        if (state.kindsOff.has(ev.kind)) return false;
        if (!state.text) return true;
        var hay = (ev.kind + ' ' + (ev.session_id || '') + ' ' + (ev.user_id || '') + ' ' +
                   (ev.summary || '')).toLowerCase();
        return hay.indexOf(state.text) !== -1;
    }

    function registerKind(kind) {
        if (state.kinds.has(kind)) return;
        state.kinds.add(kind);
        var chip = document.createElement('span');
        chip.className = 'kind-toggle';
        chip.textContent = kind;
        chip.dataset.kind = kind;
        chip.addEventListener('click', function () {
            if (state.kindsOff.has(kind)) { state.kindsOff.delete(kind); chip.classList.remove('off'); }
            else { state.kindsOff.add(kind); chip.classList.add('off'); }
            applyFilters();
        });
        els.kinds.appendChild(chip);
        updateKindOverflow();
    }

    // Collapse the chip row once it outgrows the toolbar (P1-8): chips beyond
    // MAX_VISIBLE_KINDS hide behind a "+N more" toggle.
    function updateKindOverflow() {
        var chips = els.kinds.querySelectorAll('.kind-toggle:not(.kind-more)');
        var over = chips.length - MAX_VISIBLE_KINDS;
        var i;
        if (over <= 0) {
            if (els.kindsMore) els.kindsMore.style.display = 'none';
            for (i = 0; i < chips.length; i++) chips[i].style.display = '';
            return;
        }
        if (!els.kindsMore) {
            els.kindsMore = document.createElement('span');
            els.kindsMore.className = 'kind-toggle kind-more';
            els.kindsMore.addEventListener('click', function () {
                state.kindsExpanded = !state.kindsExpanded;
                updateKindOverflow();
            });
        }
        els.kinds.appendChild(els.kindsMore); // keep it last
        els.kindsMore.style.display = '';
        els.kindsMore.textContent = state.kindsExpanded ? '− less' : '+' + over + ' more';
        for (i = 0; i < chips.length; i++) {
            chips[i].style.display = (state.kindsExpanded || i < MAX_VISIBLE_KINDS) ? '' : 'none';
        }
    }

    function applyFilters() {
        var lines = els.stream.querySelectorAll('.activity-line');
        for (var i = 0; i < lines.length; i++) {
            var line = lines[i];
            var ev = line._ev;
            line.style.display = (ev && matchesFilters(ev)) ? '' : 'none';
        }
    }

    function renderLine(ev) {
        var line = document.createElement('div');
        line.className = 'activity-line' +
            ((ev.kind === 'error' || ev.kind === 'tool_denied' || ev.kind === 'tool_timeout')
                ? ' level-error' : '');
        line._ev = ev;
        line.title = fmtFull(ev.ts);

        var t = document.createElement('span');
        t.className = 't';
        t.textContent = fmtTime(ev.ts) + ' ';
        line.appendChild(t);

        var badge = document.createElement('span');
        badge.className = 'badge src-' + (ev.source || 'feed');
        badge.textContent = ev.kind || '?';
        line.appendChild(badge);

        if (ev.session_id) {
            var sess = document.createElement('a');
            sess.className = 'sess';
            sess.textContent = (ev.user_id ? ev.user_id + '/' : '') + ev.session_id.slice(0, 8);
            sess.href = '/session/' + encodeURIComponent(ev.session_id);
            sess.addEventListener('click', function (e) {
                e.preventDefault();
                e.stopPropagation();
                openSessionPanel(ev.session_id, ev.user_id);
            });
            line.appendChild(sess);
        }

        var sum = document.createElement('span');
        sum.className = 'sum';
        sum.textContent = ev.summary || '';
        line.appendChild(sum);

        line.addEventListener('click', function () { togglePayload(line, ev); });
        if (!matchesFilters(ev)) line.style.display = 'none';
        return line;
    }

    function togglePayload(line, ev) {
        var next = line.nextElementSibling;
        if (next && next.classList.contains('activity-payload')) { next.remove(); return; }
        var pre = document.createElement('pre');
        pre.className = 'activity-payload';
        try { pre.textContent = JSON.stringify(ev.payload, null, 2); }
        catch (e) { pre.textContent = String(ev.payload); }
        line.after(pre);
    }

    // Append one rendered line, inserting a day-separator row whenever the
    // calendar day changes (P1-5 — backfill spans days and HH:MM:SS alone
    // reads as shuffled).
    function appendLine(ev) {
        var day = fmtDay(ev.ts);
        if (day !== state.lastDay) {
            state.lastDay = day;
            var sep = document.createElement('div');
            sep.className = 'activity-day-sep';
            sep.textContent = '─── ' + day + ' ───';
            els.stream.appendChild(sep);
        }
        els.stream.appendChild(renderLine(ev));
        feedPanel(ev);
    }

    function hint(msg) {
        var line = document.createElement('div');
        line.className = 'activity-hint';
        line.textContent = '⟲ ' + msg;
        els.stream.appendChild(line);
        if (state.follow) els.stream.scrollTop = els.stream.scrollHeight;
    }

    function append(ev) {
        if (!ev || !ev.id || state.seen.has(ev.id)) return;
        state.seen.add(ev.id);
        state.count += 1;
        if (state.seen.size > MAX_BUFFER) {
            // Set has insertion order — drop the oldest half to stay bounded.
            var it = state.seen.values();
            for (var i = 0; i < MAX_BUFFER / 2; i++) state.seen.delete(it.next().value);
        }
        registerKind(ev.kind);
        if (state.paused) { state.pending.push(ev); return; }
        appendLine(ev);
        while (els.stream.childElementCount > MAX_DOM) els.stream.firstElementChild.remove();
        if (state.follow) els.stream.scrollTop = els.stream.scrollHeight;
    }

    function flushPending() {
        var pending = state.pending;
        state.pending = [];
        for (var i = 0; i < pending.length; i++) {
            appendLine(pending[i]);
        }
        while (els.stream.childElementCount > MAX_DOM) els.stream.firstElementChild.remove();
        if (state.follow) els.stream.scrollTop = els.stream.scrollHeight;
    }

    // --- session drill-down panel ---------------------------------------- //

    // Compact client-side mirror of the server's summarize() for raw feed
    // events (the per-session feed API returns raw JSON, not activity events).
    function summarizeFeed(kind, d) {
        d = d || {};
        function snip(v, n) { return String(v || '').replace(/\s+/g, ' ').slice(0, n); }
        if (kind === 'tool_execution') {
            return 'tool ' + (d.tool_name || d.action || d.name || '?') +
                (d.success === false ? ' FAILED' : ' ok');
        }
        if (kind === 'llm_request') {
            return 'llm ' + (d.model_name || d.model || '?') + ' ' +
                (d.token_count || d.total_tokens || 0) + 'tk';
        }
        if (kind === 'step') {
            var note = snip(d.task_progress || d.reasoning, 80);
            return 'step ' + (d.iteration || d.step || '?') + (note ? ': ' + note : '');
        }
        if (kind === 'session_start') return 'session started: ' + snip(d.task || d.task_description, 90);
        if (kind === 'session_completion' || kind === 'task_complete') return 'session completed';
        if (kind === 'status') return 'status → ' + (d.status || '?');
        if (kind === 'error') return 'ERROR: ' + snip(d.error_message || d.message || d.error, 120);
        return kind;
    }

    function isErrorKind(kind) {
        return kind === 'error' || kind === 'tool_denied' || kind === 'tool_timeout';
    }

    function panelFeedRow(kind, ts, summary) {
        var row = document.createElement('div');
        row.className = 'feed-line' + (isErrorKind(kind) ? ' level-error' : '');
        row.textContent = fmtTime(ts) + '  ' + summary;
        return row;
    }

    // Tail-follow inside the panel (P1-9): live activity events for the open
    // session append to the panel as they stream in.
    function feedPanel(ev) {
        if (!state.panelSession || ev.session_id !== state.panelSession) return;
        els.panelBody.appendChild(panelFeedRow(ev.kind, ev.ts, ev.summary || ev.kind));
        while (els.panelBody.childElementCount > 200) els.panelBody.firstElementChild.remove();
        els.panelBody.scrollTop = els.panelBody.scrollHeight;
    }

    function openSessionPanel(sessionId, userId) {
        els.panel.classList.remove('hidden');
        state.panelSession = sessionId;
        els.panelTitle.textContent = (userId ? userId + '/' : '') + sessionId;
        els.panelOpen.href = '/session/' + encodeURIComponent(sessionId);
        els.panelBody.textContent = 'loading…';

        var statusP = fetch('/api/session/' + encodeURIComponent(sessionId) + '/status')
            .then(function (r) { return r.ok ? r.json() : {}; }).catch(function () { return {}; });
        var feedP = fetch('/api/session/' + encodeURIComponent(sessionId) + '/feed/events?limit=30')
            .then(function (r) { return r.ok ? r.json() : { events: [] }; })
            .catch(function () { return { events: [] }; });

        Promise.all([statusP, feedP]).then(function (results) {
            var status = results[0] || {};
            var feed = (results[1] && (results[1].events || results[1].feed)) || [];
            els.panelBody.textContent = '';

            var meta = document.createElement('div');
            var statusText = String(status.status || 'unknown');
            meta.className = 'meta status-' + statusText.toLowerCase();
            meta.textContent = 'status: ' + statusText +
                (status.task ? ' — ' + String(status.task).slice(0, 140) : '');
            els.panelBody.appendChild(meta);

            if (!feed.length) {
                var empty = document.createElement('div');
                empty.className = 'meta';
                empty.textContent = 'no feed events readable';
                els.panelBody.appendChild(empty);
                return;
            }
            var recent = feed.slice(-30);
            for (var i = 0; i < recent.length; i++) {
                var raw = recent[i] || {};
                var kind = raw.type || raw.event_type || 'event';
                var ts = raw._ts_ms ? raw._ts_ms / 1000 : (raw.timestamp || 0);
                var data = (raw.data && typeof raw.data === 'object') ? raw.data : raw;
                els.panelBody.appendChild(panelFeedRow(kind, ts, summarizeFeed(kind, data)));
            }
            els.panelBody.scrollTop = els.panelBody.scrollHeight;
        });
    }

    // --- transport --------------------------------------------------------- //

    function backfill() {
        return fetch('/api/activity/backfill?limit=300')
            .then(function (r) { return r.ok ? r.json() : { events: [] }; })
            .then(function (body) { (body.events || []).forEach(append); })
            .catch(function () { /* stream still attaches */ });
    }

    function connect() {
        var attach = function () {
            if (typeof io === 'undefined') return;
            var socket = io();
            socket.on('connect', function () {
                els.conn.classList.add('on');
                state.reconnected = state.hadConnection;
                state.hadConnection = true;
                socket.emit('join_activity', {});
            });
            socket.on('disconnect', function () { els.conn.classList.remove('on'); });
            socket.on('activity_snapshot', function (events) {
                events = events || [];
                // Reconnect gap detection (P1-10): the snapshot is the server's
                // ring buffer; if nothing in it overlaps what we've already
                // seen, more events happened offline than the buffer holds.
                if (state.reconnected && state.count > 0 && events.length) {
                    var overlap = 0;
                    for (var i = 0; i < events.length; i++) {
                        if (events[i] && state.seen.has(events[i].id)) overlap++;
                    }
                    if (overlap === 0) {
                        hint('stream resumed — some events may have been missed; reload for full backfill');
                    }
                }
                state.reconnected = false;
                events.forEach(append);
            });
            socket.on('activity_event', append);
            socket.on('error', function (err) {
                var line = document.createElement('div');
                line.className = 'activity-line level-error';
                line.textContent = '⚠ ' + ((err && err.message) || 'stream error');
                els.stream.appendChild(line);
            });
        };
        if (window.socketIOReady && window.socketIOReady.then) {
            window.socketIOReady.then(attach).catch(attach);
        } else {
            attach();
        }
    }

    function init() {
        els.stream = $('activity-stream');
        if (!els.stream) return;
        els.conn = $('activity-conn');
        els.kinds = $('activity-kinds');
        els.panel = $('activity-session-panel');
        els.panelTitle = $('activity-panel-title');
        els.panelOpen = $('activity-panel-open');
        els.panelBody = $('activity-panel-body');

        $('activity-search').addEventListener('input', function (e) {
            state.text = e.target.value.trim().toLowerCase();
            applyFilters();
        });
        $('activity-follow').addEventListener('change', function (e) {
            state.follow = e.target.checked;
            if (state.follow) els.stream.scrollTop = els.stream.scrollHeight;
        });
        $('activity-pause').addEventListener('click', function (e) {
            state.paused = !state.paused;
            e.target.textContent = state.paused ? 'resume' : 'pause';
            if (!state.paused) flushPending();
        });
        $('activity-clear').addEventListener('click', function () {
            els.stream.textContent = '';
            state.lastDay = '';
        });
        $('activity-panel-close').addEventListener('click', function () {
            els.panel.classList.add('hidden');
            state.panelSession = null;
        });

        backfill().then(connect);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
