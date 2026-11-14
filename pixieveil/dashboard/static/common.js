/* PixieVeil dashboard — shared helpers (no framework, no build step). */
'use strict';

const PV = (() => {
    const THEME_KEY = 'pixieveil_theme';

    // ------------------------------------------------------------ storage
    // localStorage can throw (private mode, blocked site data) — every
    // access goes through these so the UI still works without it.
    function store(key, value) {
        try {
            if (value === undefined) return localStorage.getItem(key);
            if (value === null) localStorage.removeItem(key); else localStorage.setItem(key, value);
        } catch (_e) { /* storage unavailable */ }
        return null;
    }

    // ------------------------------------------------------------ icons
    const ICONS = {
        refresh: '<path d="M20 11a8 8 0 1 0-2.3 5.7M20 5v6h-6"/>',
        sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
        moon: '<path d="M20 14.5A8 8 0 0 1 9.5 4 8 8 0 1 0 20 14.5z"/>',
        send: '<path d="M4 12l16-8-6 16-3-7-7-1z"/>',
        upload: '<path d="M12 16V4M6 10l6-6 6 6M4 20h16"/>',
        redo: '<path d="M4 12a8 8 0 0 1 14-5.3L20 9M20 4v5h-5M20 12a8 8 0 0 1-14 5.3L4 15M4 20v-5h5"/>',
        save: '<path d="M5 4h11l3 3v13H5zM8 4v5h7V4M8 20v-6h8v6"/>',
        info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.5"/>',
        alert: '<path d="M12 3l10 18H2zM12 10v5M12 18v.5"/>',
        check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
        x: '<path d="M6 6l12 12M18 6L6 18"/>',
        search: '<circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4"/>',
        back: '<path d="M15 5l-7 7 7 7"/>',
        edit: '<path d="M4 20h4L19 9l-4-4L4 16zM13.5 6.5l4 4"/>',
        inbox: '<path d="M3 13l3-8h12l3 8v6H3zM3 13h5l1 3h6l1-3h5"/>',
        lock: '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
        pause: '<path d="M9 5v14M15 5v14"/>',
        play: '<path d="M7 5l12 7-12 7z"/>',
        chevron: '<path d="M9 6l6 6-6 6"/>',
        copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5a1 1 0 0 0-1-1H5a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h3"/>',
    };
    function icon(name, extra) {
        return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" ` +
            `stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" ${extra || ''}>` +
            `${ICONS[name] || ''}</svg>`;
    }

    // ------------------------------------------------------------ formatting
    function esc(s) {
        if (s === null || s === undefined) return '';
        return String(s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
    const pad4 = n => String(n).padStart(4, '0');
    const fmtInt = v => (typeof v === 'number' ? v.toLocaleString() : (v ?? '—'));
    const fmtNum = (v, digits = 1) => (v == null || Number.isNaN(Number(v)) ? '—' : Number(v).toFixed(digits));

    function fmtDate(iso) {
        if (!iso) return '—';
        const d = new Date(iso);
        if (Number.isNaN(d.getTime())) return '—';
        const today = new Date();
        const sameDay = d.toDateString() === today.toDateString();
        const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        if (sameDay) return `Today ${time}`;
        return `${d.toLocaleDateString([], { day: '2-digit', month: 'short' })} ${time}`;
    }

    function fmtAgo(ms) {
        const s = Math.max(0, Math.round(ms / 1000));
        if (s < 5) return 'just now';
        if (s < 60) return `${s}s ago`;
        const m = Math.round(s / 60);
        if (m < 60) return `${m} min ago`;
        return `${Math.round(m / 60)} h ago`;
    }

    function fmtDuration(totalSeconds) {
        const s = Math.max(0, Math.floor(totalSeconds || 0));
        const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
        if (d) return `${d}d ${h}h`;
        if (h) return `${h}h ${m}m`;
        return `${m}m`;
    }

    // ------------------------------------------------------------ study vocabulary
    // Sidecar status → display label + pill tone.
    const STATUS = {
        receiving:       { label: 'Receiving',     tone: 'busy' },
        complete:        { label: 'Queued',        tone: 'busy' },
        defacing:        { label: 'Defacing',      tone: 'busy' },
        defacing_failed: { label: 'Deface failed', tone: 'bad' },
        defacing_disabled: { label: 'Defacing disabled', tone: 'bad' },
        ready:           { label: 'Ready',         tone: 'ok' },
        archived:        { label: 'Exported',      tone: 'done' },
    };
    function statusPill(status) {
        const s = STATUS[status] || { label: status || 'unknown', tone: '' };
        return `<span class="pill ${s.tone}">${esc(s.label)}</span>`;
    }

    // Required dotted path → short chip label (region + clinical indication
    // collapse into one "Indication" chip).
    const FIELD_LABELS = {
        'image_quality': 'Quality',
        'patient.weight_kg': 'Weight',
        'patient.age_years': 'Age',
        'indication.region': 'Indication',
        'indication.clinical_indication': 'Indication',
        'indication.iv_contrast': 'Contrast',
        'scanner.rhythm_scanner_id': 'Scanner ID',
        'rhythm.protocol_key': 'RHYTHM protocol',
    };
    function missingLabels(paths) {
        return [...new Set((paths || []).map(p => FIELD_LABELS[p] || p))];
    }

    // ------------------------------------------------------------ theme
    function currentTheme() {
        return document.documentElement.dataset.theme === 'light' ? 'light' : 'dark';
    }
    function applyTheme(theme) {
        document.documentElement.dataset.theme = theme;
        document.querySelectorAll('[data-theme-toggle]').forEach(btn => {
            btn.innerHTML = icon(theme === 'light' ? 'moon' : 'sun');
            btn.title = theme === 'light' ? 'Switch to dark theme' : 'Switch to light theme';
        });
    }
    function toggleTheme() {
        const next = currentTheme() === 'light' ? 'dark' : 'light';
        store(THEME_KEY, next);
        applyTheme(next);
    }

    // ------------------------------------------------------------ connection indicator
    function setConn(ok, text) {
        const el = document.getElementById('conn');
        if (!el) return;
        el.classList.toggle('ok', ok);
        el.classList.toggle('down', !ok);
        el.querySelector('.label').textContent = text || (ok ? 'Live' : 'Server unreachable');
    }

    // ------------------------------------------------------------ toast
    function toast(message, tone, ms) {
        let host = document.querySelector('.toasts');
        if (!host) {
            host = document.createElement('div');
            host.className = 'toasts';
            host.setAttribute('role', 'status');
            host.setAttribute('aria-live', 'polite');
            document.body.appendChild(host);
        }
        const el = document.createElement('div');
        el.className = `toast ${tone || ''}`;
        el.textContent = message;
        host.appendChild(el);
        const remove = () => el.remove();
        let timer = setTimeout(remove, ms || 4500);
        return {
            update(msg, newTone) {
                el.textContent = msg;
                if (newTone) el.className = `toast ${newTone}`;
                clearTimeout(timer);
                timer = setTimeout(remove, ms || 4500);
            },
            sticky() { clearTimeout(timer); },
            close: remove,
        };
    }

    // ------------------------------------------------------------ dialog
    // modal({title, body (html), confirm, cancel, tone}) → Promise<FormData|null>
    function modal(opts) {
        return new Promise(resolve => {
            const dlg = document.createElement('dialog');
            dlg.className = 'modal';
            const confirmCls = opts.tone === 'danger' ? 'btn btn-danger' : 'btn btn-primary';
            dlg.innerHTML = `
                <form method="dialog">
                    <div class="modal-body">
                        <h2>${esc(opts.title)}</h2>
                        ${opts.body || ''}
                    </div>
                    <div class="modal-actions">
                        <button type="button" class="btn btn-ghost" data-cancel>${esc(opts.cancel || 'Cancel')}</button>
                        <button type="submit" class="${confirmCls}" value="ok">${esc(opts.confirm || 'OK')}</button>
                    </div>
                </form>`;
            document.body.appendChild(dlg);
            const form = dlg.querySelector('form');
            let result = null;
            form.addEventListener('submit', () => { result = new FormData(form); });
            dlg.querySelector('[data-cancel]').addEventListener('click', () => dlg.close());
            dlg.addEventListener('close', () => { dlg.remove(); resolve(result); });
            dlg.showModal();
            const first = dlg.querySelector('input.input, textarea, button[type=submit]');
            if (first) first.focus();
        });
    }

    // ------------------------------------------------------------ API
    // Authentication is a session cookie set by /login; every request also
    // carries X-Requested-With (the server's CSRF guard for mutations).
    function toLogin() {
        const next = location.pathname + location.search;
        location.href = `/login?next=${encodeURIComponent(next)}`;
    }

    // api(path, {method, body}) → parsed JSON; throws Error(message) with
    // .status/.data on non-2xx. A 401 (session expired) goes to /login.
    async function api(path, opts) {
        opts = opts || {};
        const headers = { 'X-Requested-With': 'PixieVeil' };
        if (opts.body !== undefined) headers['Content-Type'] = 'application/json';
        const resp = await fetch(path, {
            method: opts.method || 'GET',
            headers,
            credentials: 'same-origin',
            body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
        });
        let data = null;
        try { data = await resp.json(); } catch (_e) { data = null; }
        if (resp.status === 401 && !opts.noRedirect) {
            toLogin();
            throw Object.assign(new Error('Session expired'), { status: 401, data });
        }
        if (!resp.ok) {
            const err = new Error((data && (data.message || data.error)) || `HTTP ${resp.status}`);
            err.status = resp.status;
            err.data = data;
            throw err;
        }
        return data;
    }

    // ------------------------------------------------------------ account menu
    let me = null;
    async function changePassword() {
        const fd = await modal({
            title: 'Change password',
            body: `<label class="field"><span class="label">Current password</span>
                     <input class="input" type="password" name="current" autocomplete="current-password" required></label>
                   <label class="field"><span class="label">New password (min. 8 characters)</span>
                     <input class="input" type="password" name="new" autocomplete="new-password" minlength="8" required></label>`,
            confirm: 'Change password',
        });
        if (!fd) return;
        try {
            await api('/api/me/password', { method: 'POST',
                body: { current: fd.get('current'), new: fd.get('new') } });
            toast('Password changed. Other sessions were logged out.', 'ok');
        } catch (e) { toast(e.message, 'bad'); }
    }

    async function manageTokens() {
        let list;
        try { list = (await api('/api/me/tokens')).tokens; } catch (e) { toast(e.message, 'bad'); return; }
        const rows = list.length ? list.map(t => `
            <tr><td class="mono">${esc(t.id)}</td><td>${esc(t.name)}</td>
                <td class="dim">${fmtDate(t.last_used)}</td>
                <td><label class="faint"><input type="checkbox" name="revoke" value="${esc(t.id)}"> revoke</label></td></tr>`).join('')
            : '<tr><td colspan="4" class="dim">No API tokens yet.</td></tr>';
        const fd = await modal({
            title: 'API tokens',
            body: `<p class="dim">For scripts and tooling: send as <span class="mono">Authorization: Bearer &lt;token&gt;</span>.
                   A token acts as you.</p>
                   <table class="data"><thead><tr><th>ID</th><th>Name</th><th>Last used</th><th></th></tr></thead>
                   <tbody>${rows}</tbody></table>
                   <label class="field"><span class="label">New token name (optional)</span>
                     <input class="input" name="name" maxlength="64" placeholder="e.g. export watcher"></label>`,
            confirm: 'Apply',
        });
        if (!fd) return;
        try {
            for (const id of fd.getAll('revoke')) {
                await api(`/api/me/tokens/${encodeURIComponent(id)}`, { method: 'DELETE' });
            }
            const name = String(fd.get('name') || '').trim();
            if (name) {
                const r = await api('/api/me/tokens', { method: 'POST', body: { name } });
                await modal({
                    title: 'New API token',
                    body: `<p>Copy it now — it will not be shown again.</p>
                           <input class="input mono" readonly value="${esc(r.token)}" onfocus="this.select()">`,
                    confirm: 'Done', cancel: 'Close',
                });
            } else if (fd.getAll('revoke').length) {
                toast('Token(s) revoked', 'ok');
            }
        } catch (e) { toast(e.message, 'bad'); }
    }

    async function logout() {
        try { await api('/logout', { method: 'POST' }); } catch (_e) { /* going anyway */ }
        location.href = '/login';
    }

    // The Studies nav badge (studies needing input). / and /studies keep it
    // current from their own data; every other page fills it once here.
    async function refreshNavBadge() {
        const badge = document.getElementById('nav-needs');
        if (!badge || ['/', '/studies', '/login', '/setup'].includes(location.pathname)) return;
        try {
            const { studies } = await api('/api/studies');
            const needs = (studies || []).filter(PV.needsInput).length;
            badge.hidden = !needs;
            badge.textContent = needs;
        } catch (_e) { /* badge is optional */ }
    }

    async function initAccount() {
        const bar = document.querySelector('.topbar');
        if (!bar || ['/login', '/setup'].includes(location.pathname)) return;
        try { me = await api('/api/me'); } catch (_e) { return; }
        if (me.admin) {
            const nav = bar.querySelector('.nav');
            if (nav && !nav.querySelector('a[href="/users"]')) {
                const a = document.createElement('a');
                a.href = '/users';
                a.textContent = 'Users';
                if (location.pathname === '/users') a.setAttribute('aria-current', 'page');
                // Users sits before About.
                nav.insertBefore(a, nav.querySelector('a[href="/about"]'));
            }
            if (nav && !nav.querySelector('a[href="/settings"]')) {
                const a = document.createElement('a');
                a.href = '/settings';
                a.textContent = 'Settings';
                if (location.pathname === '/settings') a.setAttribute('aria-current', 'page');
                // Settings sits between Users and About.
                nav.insertBefore(a, nav.querySelector('a[href="/about"]'));
            }
            if (nav && !nav.querySelector('a[href="/lookup"]')) {
                const a = document.createElement('a');
                a.href = '/lookup';
                a.textContent = 'Lookup';
                if (location.pathname === '/lookup') a.setAttribute('aria-current', 'page');
                nav.insertBefore(a, nav.querySelector('a[href="/about"]'));
            }
        }
        const wrap = document.createElement('div');
        wrap.className = 'account';
        wrap.innerHTML = `
            <button type="button" class="btn btn-ghost account-btn" aria-haspopup="true" aria-expanded="false">
                ${icon('lock')}<span>${esc(me.username)}</span></button>
            <div class="account-menu" hidden>
                <button type="button" data-act="password">Change password</button>
                <button type="button" data-act="tokens">API tokens</button>
                <button type="button" data-act="logout">Log out</button>
            </div>`;
        const btn = wrap.querySelector('.account-btn');
        const menu = wrap.querySelector('.account-menu');
        const setOpen = open => { menu.hidden = !open; btn.setAttribute('aria-expanded', String(open)); };
        btn.addEventListener('click', e => { e.stopPropagation(); setOpen(menu.hidden); });
        document.addEventListener('click', () => setOpen(false));
        document.addEventListener('keydown', e => { if (e.key === 'Escape') setOpen(false); });
        menu.addEventListener('click', e => {
            const act = e.target.closest('[data-act]')?.dataset.act;
            setOpen(false);
            if (act === 'password') changePassword();
            if (act === 'tokens') manageTokens();
            if (act === 'logout') logout();
        });
        const toggle = bar.querySelector('[data-theme-toggle]');
        bar.insertBefore(wrap, toggle || null);
    }

    // ------------------------------------------------------------ polling
    // poll(fn, ms): runs fn now and every ms while the tab is visible.
    function poll(fn, ms) {
        let timer = null;
        const baseMs = ms;
        let paused = false;
        const tick = async () => {
            clearTimeout(timer);
            try { await fn(); } finally {
                if (!paused && !document.hidden) timer = setTimeout(tick, ms);
            }
        };
        document.addEventListener('visibilitychange', () => {
            if (!document.hidden && !paused) tick(); else clearTimeout(timer);
        });
        tick();
        return {
            now: tick,
            pause() { paused = true; clearTimeout(timer); },
            resume() { paused = false; tick(); },
            get paused() { return paused; },
            // slow(true) while the live stream is up (polling is only a
            // safety net then); slow(false) restores the original interval.
            slow(on) {
                const next = on ? Math.max(baseMs, 60000) : baseMs;
                if (next === ms) return;
                ms = next;
                if (!paused && !document.hidden) { clearTimeout(timer); timer = setTimeout(tick, ms); }
            },
        };
    }

    // ------------------------------------------------------------ live (SSE)
    // live(handlers) subscribes to /api/events. handlers: {study, exam,
    // progress, counters} get the event data; resync() runs when the server
    // restarted or missed events can't be replayed; state(connected) runs on
    // connect/disconnect. Events are hints — handlers re-fetch via the API.
    // Returns null when EventSource is unavailable (polling then carries on).
    function live(handlers) {
        if (!window.EventSource) return null;
        let es = null;
        let serverStarted = null;
        let connected = false;
        const setState = ok => {
            if (ok === connected) return;
            connected = ok;
            if (handlers.state) handlers.state(ok);
        };
        const open = () => {
            es = new EventSource('/api/events');
            es.addEventListener('hello', e => {
                const d = JSON.parse(e.data);
                setState(true);
                const restarted = serverStarted !== null && d.server_started !== serverStarted;
                serverStarted = d.server_started;
                if ((restarted || d.resync) && handlers.resync) handlers.resync();
            });
            ['study', 'exam', 'progress', 'counters'].forEach(type => {
                es.addEventListener(type, e => {
                    if (handlers[type]) handlers[type](JSON.parse(e.data));
                });
            });
            // EventSource reconnects by itself (resending Last-Event-ID).
            // If the session expired, the reconnect gets a 401 and the
            // browser gives up (readyState CLOSED) — check and go to /login.
            es.onerror = () => {
                setState(false);
                if (es.readyState === EventSource.CLOSED) {
                    fetch('/api/me', { credentials: 'same-origin' })
                        .then(r => { if (r.status === 401) toLogin(); })
                        .catch(() => {});
                }
            };
        };
        open();
        return {
            get connected() { return connected; },
            close() { if (es) es.close(); setState(false); },
        };
    }

    // debounce(fn, ms): one trailing call per burst of events.
    function debounce(fn, ms) {
        let t = null;
        return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
    }

    // ------------------------------------------------------------ init
    document.addEventListener('DOMContentLoaded', () => {
        applyTheme(currentTheme());
        document.querySelectorAll('[data-theme-toggle]').forEach(btn =>
            btn.addEventListener('click', toggleTheme));
        initAccount();
        refreshNavBadge();
    });

    return {
        esc, pad4, fmtInt, fmtNum, fmtDate, fmtAgo, fmtDuration,
        icon, toast, modal, api, poll, live, debounce, setConn,
        get me() { return me; },
        STATUS, statusPill, FIELD_LABELS, missingLabels,
    };
})();

// A study "needs input" once it has finished processing (or been exported)
// and its exam data is absent, unreadable, or missing a required field.
PV.needsInput = s => {
    if (['receiving', 'complete', 'defacing'].includes(s.status)) return false;
    if (s.exam_corrupt || !s.exam_summary) return true;
    return (s.exam_summary.missing_fields || []).length > 0;
};
