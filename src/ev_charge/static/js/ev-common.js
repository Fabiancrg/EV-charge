/* EV Charge — shared helpers: API calls, formatting, modals, toasts, theme, car colours, session form. */
(() => {
    'use strict';
    const EV = window.EV = window.EV || {};

    /* ── Theme ───────────────────────────────────────────────────────── */
    EV.theme = () => document.documentElement.getAttribute('data-theme') || 'light';
    EV.setTheme = (t) => {
        document.documentElement.setAttribute('data-theme', t);
        try { localStorage.setItem('ev-theme', t); } catch (e) { /* private mode */ }
        document.dispatchEvent(new CustomEvent('ev:themechange', { detail: { theme: t } }));
    };
    document.getElementById('theme-toggle')?.addEventListener('click', () => EV.setTheme(EV.theme() === 'dark' ? 'light' : 'dark'));

    /* ── Car colours: categorical palette, fixed order, indexed by car (colour follows the car) ── */
    const PALETTE = {
        light: ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300', '#6250d6', '#e34948'],
        dark:  ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#008300', '#9085e9', '#e66767'],
    };
    EV.seriesColor = (i) => { const p = PALETTE[EV.theme() === 'dark' ? 'dark' : 'light']; return p[i % p.length]; };
    EV.carColorIndex = {};  // car id → index (cars sorted by id), filled by EV.loadCars()
    EV.carColor = (carId) => EV.seriesColor(EV.carColorIndex[carId] ?? 0);
    EV.carDot = (carId) => `<span class="car-dot" style="background:${EV.carColor(carId)}"></span>`;

    /* ── API ─────────────────────────────────────────────────────────── */
    EV.api = async (method, url, body) => {
        const opts = { method, headers: {} };
        if (body !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
        const r = await fetch(url, opts);
        if (r.status === 204) return null;
        const data = await r.json().catch(() => null);
        if (!r.ok) {
            let msg = `${r.status} ${r.statusText}`;
            if (data && typeof data.detail === 'string') msg = data.detail;
            else if (data && Array.isArray(data.detail)) msg = data.detail.map(d => `${(d.loc || []).filter(x => x !== 'body').join('.') || 'form'}: ${d.msg.replace(/^Value error, /, '')}`).join('\n');
            throw new Error(msg);
        }
        return data;
    };
    EV.loadCars = async () => {
        const cars = await EV.api('GET', '/api/cars');
        [...cars].sort((a, b) => a.id - b.id).forEach((c, i) => { EV.carColorIndex[c.id] = i; });
        EV.cars = cars;
        return cars;
    };

    /* ── Formatting ──────────────────────────────────────────────────── */
    EV.esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    const nf = (d) => new Intl.NumberFormat(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
    EV.num = (v, d = 0) => (v === null || v === undefined || v === '') ? '—' : nf(d).format(v);
    EV.kwh = (v) => v == null ? '—' : `${EV.num(v, v >= 100 ? 0 : 1)} kWh`;
    EV.km = (v) => v == null ? '—' : `${EV.num(v)} km`;
    EV.money = (v, d = 2) => {
        if (v == null) return '—';
        try { return new Intl.NumberFormat(undefined, { style: 'currency', currency: EV.currency || 'EUR', minimumFractionDigits: d, maximumFractionDigits: d }).format(v); }
        catch (e) { return `${EV.num(v, d)} ${EV.currency}`; }
    };
    EV.date = (iso) => iso ? new Date(iso + 'T00:00:00').toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }) : '—';
    EV.time = (t) => t ? t.slice(0, 5) : '';
    EV.duration = (min) => min == null ? '—' : `${Math.floor(min / 60)}h${String(min % 60).padStart(2, '0')}`;
    EV.today = () => { const d = new Date(); return new Date(d - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10); };

    /* ── Toasts & modals ─────────────────────────────────────────────── */
    EV.toast = (msg, isErr = false) => {
        const el = document.createElement('div');
        el.className = 'toast' + (isErr ? ' err' : '');
        el.textContent = msg;
        document.getElementById('toast').appendChild(el);
        setTimeout(() => el.remove(), isErr ? 6000 : 3000);
    };
    EV.openModal = (id) => {
        const m = document.getElementById(id);
        m.classList.add('open');
        m.querySelector('.form-error')?.style.setProperty('display', 'none');
        setTimeout(() => m.querySelector('[autofocus], input:not([type=hidden]), select')?.focus(), 30);
    };
    EV.closeModal = (id) => document.getElementById(id).classList.remove('open');
    EV.formError = (modalId, err) => {
        const box = document.querySelector(`#${modalId} .form-error`);
        if (!box) { EV.toast(err.message || String(err), true); return; }
        box.textContent = err.message || String(err);
        box.style.display = 'block';
        box.style.whiteSpace = 'pre-line';
    };
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') document.querySelectorAll('.modal.open').forEach(m => m.classList.remove('open')); });
    document.addEventListener('click', (e) => {
        if (e.target.classList?.contains('modal')) e.target.classList.remove('open');
        const closer = e.target.closest?.('[data-close]');
        if (closer) closer.closest('.modal').classList.remove('open');
    });

    /* Read / fill a form whose inputs carry name="field" */
    EV.formData = (form) => {
        const out = {};
        form.querySelectorAll('[name]').forEach(el => {
            if (el.type === 'checkbox') out[el.name] = el.checked;
            else if (el.type === 'number') out[el.name] = el.value === '' ? null : Number(el.value);
            else out[el.name] = el.value === '' ? null : el.value;
        });
        return out;
    };
    EV.fillForm = (form, data) => {
        form.querySelectorAll('[name]').forEach(el => {
            const v = data[el.name];
            if (el.type === 'checkbox') el.checked = !!v;
            else if (el.type === 'time') el.value = v ? String(v).slice(0, 5) : '';
            else el.value = v ?? '';
        });
    };

    /* ── Session form (modal included by templates/_session_modal.html) ── */
    let suggestions = null;
    let editingId = null;
    let afterSave = null;

    async function refreshSuggestions(carId) {
        const qs = carId ? `?car_id=${carId}` : '';
        suggestions = await EV.api('GET', `/api/sessions/suggestions${qs}`);
        const fill = (id, values) => { document.getElementById(id).innerHTML = values.map(v => `<option value="${EV.esc(v)}">`).join(''); };
        fill('dl-locations', suggestions.locations);
        fill('dl-providers', suggestions.providers);
        const hint = document.getElementById('sf-odo-hint');
        hint.textContent = suggestions.last_odometer != null ? `Last reading: ${EV.km(suggestions.last_odometer)} (${EV.date(suggestions.last_odometer_date)})` : '';
    }

    function updateDuration() {
        const f = document.getElementById('session-form');
        const s = f.start_time.value, e = f.end_time.value, kwh = Number(f.kwh.value);
        const hint = document.getElementById('sf-duration-hint');
        if (!s || !e) { hint.textContent = ''; return; }
        let min = (Number(e.slice(0, 2)) * 60 + Number(e.slice(3, 5))) - (Number(s.slice(0, 2)) * 60 + Number(s.slice(3, 5)));
        const nextDay = min < 0;
        if (nextDay) min += 1440;
        hint.textContent = `${EV.duration(min)}${nextDay ? ' (ends next day)' : ''}${kwh && min ? ` · avg ${EV.num(kwh / (min / 60), 1)} kW` : ''}`;
    }

    EV.openSessionForm = async (session = null, opts = {}) => {
        const f = document.getElementById('session-form');
        if (!f) return;
        if (!EV.cars) await EV.loadCars();
        const active = EV.cars.filter(c => c.active || (session && c.id === session.car_id));
        if (!active.length) { EV.toast('Add a car first (Cars page)', true); return; }
        f.car_id.innerHTML = active.map(c => `<option value="${c.id}">${EV.esc(c.name)}${c.license_plate ? ' · ' + EV.esc(c.license_plate) : ''}</option>`).join('');
        editingId = session ? session.id : null;
        afterSave = opts.onSaved || null;
        document.getElementById('sf-title').textContent = session ? 'Edit charging session' : 'New charging session';
        document.getElementById('sf-delete').style.display = session ? '' : 'none';
        document.getElementById('sf-again').style.display = session ? 'none' : '';
        if (session) {
            EV.fillForm(f, session);
        } else {
            f.reset();
            f.charge_date.value = EV.today();
            await refreshSuggestions(null);
            const carId = opts.carId || suggestions.last_used_car_id;
            if (carId && active.some(c => c.id === carId)) f.car_id.value = carId;
        }
        await refreshSuggestions(Number(f.car_id.value));
        updateDuration();
        EV.openModal('session-modal');
    };

    async function saveSession(keepOpen) {
        const f = document.getElementById('session-form');
        const body = EV.formData(f);
        body.car_id = Number(body.car_id);
        try {
            const saved = editingId
                ? await EV.api('PUT', `/api/sessions/${editingId}`, body)
                : await EV.api('POST', '/api/sessions', body);
            if (body.odometer != null && suggestions && suggestions.last_odometer != null && !editingId && body.odometer < suggestions.last_odometer) {
                EV.toast(`Saved — note: odometer is lower than the last reading (${EV.km(suggestions.last_odometer)})`, true);
            } else {
                EV.toast(editingId ? 'Session updated' : `Session saved: ${EV.kwh(saved.kwh)}`);
            }
            if (keepOpen) {
                const keep = { car_id: f.car_id.value, charge_date: f.charge_date.value };
                f.reset();
                f.car_id.value = keep.car_id; f.charge_date.value = keep.charge_date;
                await refreshSuggestions(Number(keep.car_id));
                updateDuration();
                f.odometer.focus();
            } else {
                EV.closeModal('session-modal');
            }
            afterSave && afterSave(saved);
        } catch (err) { EV.formError('session-modal', err); }
    }

    document.addEventListener('DOMContentLoaded', () => {
        const f = document.getElementById('session-form');
        if (!f) return;
        f.addEventListener('submit', (e) => { e.preventDefault(); saveSession(false); });
        document.getElementById('sf-again').addEventListener('click', () => { if (f.reportValidity()) saveSession(true); });
        document.getElementById('sf-delete').addEventListener('click', async () => {
            if (!editingId || !confirm('Delete this charging session?')) return;
            try {
                await EV.api('DELETE', `/api/sessions/${editingId}`);
                EV.toast('Session deleted');
                EV.closeModal('session-modal');
                afterSave && afterSave(null);
            } catch (err) { EV.formError('session-modal', err); }
        });
        f.car_id.addEventListener('change', () => refreshSuggestions(Number(f.car_id.value)));
        f.start_time.addEventListener('input', updateDuration);
        f.end_time.addEventListener('input', updateDuration);
        f.kwh.addEventListener('input', updateDuration);
        // A known location pre-fills the provider and the reimbursement flag used there last time
        f.location.addEventListener('change', () => {
            const last = suggestions?.by_location?.[f.location.value];
            if (!last || editingId) return;
            if (!f.provider.value && last.provider) f.provider.value = last.provider;
            f.to_reimburse.checked = !!last.to_reimburse;
        });
    });
})();
