/**
 * Session import wizard: Excel workbook (.xlsx/.xlsm) or CSV → charging sessions.
 * Same flow as Flow Manager's flow import wizard:
 *
 *   1. Sheet   — pick the tab and the header row (auto-detected; click a row to change it)
 *   2. Columns — review the column → field mapping (auto-mapped from the header names) and the options
 *   3. Review  — the server parses every row (preview, nothing stored): ok / duplicate / error, then import
 *
 * Usage: EV.importWizard.open(file, { onDone })
 */
(() => {
    'use strict';
    const EV = window.EV;
    const FIELDS = [
        ['charge_date', 'Charge date *'], ['car', 'Car (name or plate)'], ['odometer', 'Odometer (km)'], ['location', 'Location'], ['provider', 'Provider'],
        ['kwh', 'Energy (kWh) *'], ['start_time', 'Start time'], ['end_time', 'End time'], ['duration', 'Duration (if no end time)'],
        ['to_reimburse', 'To be reimbursed (yes/no)'], ['cost', 'Cost'], ['notes', 'Notes'],
    ];
    const LABEL = Object.fromEntries(FIELDS);
    // Header names recognised for each field (compared after normalisation: lower case, no accents/punctuation). EN / FR / NL.
    const SYNONYMS = {
        charge_date: ['date', 'charge date', 'charging date', 'session date', 'day', 'datum', 'jour', 'date de charge', 'date recharge', 'laaddatum', 'start date', 'date debut'],
        car: ['car', 'vehicle', 'car name', 'voiture', 'vehicule', 'auto', 'wagen', 'voertuig', 'plate', 'license plate', 'licence plate', 'number plate', 'plaque', 'immatriculation', 'nummerplaat', 'kenteken'],
        odometer: ['odometer', 'odo', 'mileage', 'km', 'kms', 'kilometers', 'kilometres', 'km stand', 'kilometerstand', 'compteur', 'kilometrage', 'odometer km', 'km counter'],
        location: ['location', 'place', 'site', 'address', 'where', 'lieu', 'adresse', 'endroit', 'locatie', 'plaats', 'adres', 'station', 'charger', 'charging station', 'charge point', 'borne', 'laadpaal', 'laadpunt'],
        provider: ['provider', 'operator', 'network', 'cpo', 'emsp', 'supplier', 'fournisseur', 'operateur', 'reseau', 'leverancier', 'aanbieder', 'charge card', 'card', 'charging card', 'badge'],
        kwh: ['kwh', 'energy', 'energy kwh', 'kwh charged', 'charged', 'charged kwh', 'energy charged', 'quantity', 'volume', 'energie', 'energie kwh', 'consommation', 'verbruik', 'hoeveelheid', 'geladen'],
        start_time: ['start', 'start time', 'begin', 'begin time', 'from', 'started', 'heure debut', 'debut', 'heure de debut', 'starttijd', 'start tijd', 'beginuur', 'begintijd'],
        end_time: ['end', 'end time', 'stop', 'stop time', 'until', 'ended', 'finish', 'heure fin', 'fin', 'heure de fin', 'eindtijd', 'eind tijd', 'einduur', 'stoptijd'],
        duration: ['duration', 'charging time', 'charge time', 'charging duration', 'duree', 'temps de charge', 'duur', 'laadtijd', 'laadduur'],
        to_reimburse: ['reimburse', 'to reimburse', 'reimbursable', 'reimbursement', 'refund', 'refundable', 'business', 'professional', 'expense', 'company paid', 'rembourser', 'a rembourser', 'remboursable', 'remboursement', 'professionnel', 'terugbetaling', 'terug te betalen', 'terugbetalen', 'zakelijk'],
        cost: ['cost', 'price', 'amount', 'total', 'total cost', 'paid', 'eur', 'euro', 'cout', 'prix', 'montant', 'kost', 'kosten', 'prijs', 'bedrag', 'cost eur', 'price eur'],
        notes: ['notes', 'note', 'comment', 'comments', 'remarks', 'remark', 'description', 'commentaire', 'commentaires', 'remarque', 'remarques', 'opmerking', 'opmerkingen'],
    };
    const PREVIEW_ROWS = 30;

    const esc = EV.esc;
    const norm = s => String(s || '').toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, '').replace(/[^a-z0-9#°€]+/g, ' ').trim();
    const colLetter = i => { let s = ''; for (i += 1; i > 0; i = Math.floor((i - 1) / 26)) s = String.fromCharCode(65 + ((i - 1) % 26)) + s; return s; };
    const oneLine = v => String(v || '').replace(/\s*\n\s*/g, ' ⏎ ');
    const clip = (v, n) => (v && v.length > n ? v.slice(0, n - 1) + '…' : v);

    // ── Header matching (as in Flow Manager) ────────────────────────────
    function matchScore(header, field) {
        const h = norm(header);
        if (!h) return 0;
        let best = 0;
        for (const syn of SYNONYMS[field]) {
            if (h === syn) return 100;
            // Synonyms may also appear inside a longer header ("Energy (kWh)", "Coût TTC")
            if ((syn.length >= 3 || /^kwh|km$/.test(syn)) && ` ${h} `.includes(` ${syn} `)) best = Math.max(best, 40 + syn.length);
        }
        return best;
    }

    function autoMap(headers) {
        const pairs = [];
        headers.forEach((h, col) => Object.keys(SYNONYMS).forEach(field => {
            const score = matchScore(h, field);
            if (score) pairs.push({ col, field, score });
        }));
        pairs.sort((a, b) => b.score - a.score || a.col - b.col);
        const mapping = headers.map(() => 'ignore');
        const used = new Set();
        for (const p of pairs) {
            if (mapping[p.col] !== 'ignore' || used.has(p.field)) continue;
            mapping[p.col] = p.field;
            used.add(p.field);
        }
        return mapping;
    }

    function headerCandidate(rows) {
        let best = -1, bestScore = 0;
        rows.slice(0, PREVIEW_ROWS).forEach((row, i) => {
            const filled = row.filter(Boolean);
            if (filled.length < 2) return;
            const known = row.filter(c => c && Object.keys(SYNONYMS).some(f => matchScore(c, f) >= 50)).length;
            const texty = filled.filter(c => !/^[\d.,:\s/-]+$/.test(c)).length;
            const score = known * 10 + texty * 0.1;
            if (score > bestScore) { best = i; bestScore = score; }
        });
        return { row: Math.max(0, best), score: bestScore };
    }

    // ── State & modal ───────────────────────────────────────────────────
    let state = null;
    const modal = () => document.getElementById('import-modal');
    const $ = sel => modal().querySelector(sel);
    const alertBox = (msg) => { const b = $('.form-error'); b.textContent = msg || ''; b.style.display = msg ? 'block' : 'none'; };

    async function open(file, { onDone } = {}) {
        EV.openModal('import-modal');
        alertBox(null);
        $('[data-steps]').innerHTML = '';
        $('[data-body]').innerHTML = '<p class="muted">Reading the file…</p>';
        $('[data-actions]').innerHTML = '';
        const form = new FormData();
        form.append('file', file);
        let data;
        try {
            const r = await fetch('/api/import/sheet', { method: 'POST', body: form });
            data = await r.json();
            if (!r.ok) throw new Error(data.detail || r.statusText);
        } catch (e) {
            $('[data-body]').innerHTML = '';
            alertBox('Cannot read the file: ' + e.message);
            setActions('<button class="btn btn-ghost" data-act="close">Close</button>');
            return;
        }
        const sheets = data.sheets.filter(s => s.rows.length);
        if (!sheets.length) { $('[data-body]').innerHTML = ''; alertBox('The file contains no data.'); setActions('<button class="btn btn-ghost" data-act="close">Close</button>'); return; }
        const cars = EV.cars || await EV.loadCars();
        // Start on the visible sheet that looks most like a session list
        const scored = sheets.map((s, i) => ({ i, score: s.hidden ? -1 : headerCandidate(s.rows).score + Math.min(s.rows.length, 50) / 1000 }));
        state = {
            filename: data.filename || file.name, sheets, step: 1, onDone, cars,
            carId: (cars.find(c => c.active) || cars[0] || {}).id || null,
            createMissing: false, dateOrder: 'auto', defaultReimb: false, skipDuplicates: true,
        };
        selectSheet(scored.sort((a, b) => b.score - a.score)[0].i);
        render();
    }

    function selectSheet(i) {
        state.sheetIndex = i;
        state.headerRow = headerCandidate(state.sheets[i].rows).row;
        remap();
    }

    function headers() {
        const sheet = state.sheets[state.sheetIndex];
        const width = Math.max(...sheet.rows.map(r => r.length));
        const row = sheet.rows[state.headerRow] || [];
        return Array.from({ length: width }, (_, c) => row[c] || '');
    }
    const remap = () => { state.mapping = autoMap(headers()); };

    function render() {
        alertBox(null);
        const titles = ['Sheet & header row', 'Columns', 'Review & import'];
        $('[data-steps]').innerHTML = titles.map((t, i) =>
            `<span class="imp-step${state.step === i + 1 ? ' on' : ''}${state.step > i + 1 ? ' done' : ''}">${i + 1}. ${t}</span>`).join('<span class="muted">›</span>');
        if (state.step === 1) renderSheet();
        else if (state.step === 2) renderColumns();
        else renderReview();
    }

    function setActions(html) {
        $('[data-actions]').innerHTML = html;
        $('[data-actions]').querySelectorAll('[data-act]').forEach(b => b.addEventListener('click', () => {
            const act = b.dataset.act;
            if (act === 'close') EV.closeModal('import-modal');
            else if (act === 'back') { state.step--; render(); }
            else if (act === 'next') {
                if (state.step === 2) {
                    const missing = missingFields();
                    if (missing) { alertBox(missing); return; }
                }
                state.step++; render();
            } else if (act === 'import') doImport(b);
        }));
    }

    function missingFields() {
        const m = state.mapping;
        if (!m.includes('charge_date') && !m.includes('start_time')) return 'Map the column holding the charge date.';
        if (!m.includes('kwh')) return 'Map the column holding the energy (kWh).';
        if (!m.includes('car') && !state.carId) return 'Choose the car, or map the column holding the car.';
        return '';
    }

    // ── Step 1: sheet & header row ──────────────────────────────────────
    function renderSheet() {
        const sheet = state.sheets[state.sheetIndex];
        const width = Math.min(Math.max(...sheet.rows.map(r => r.length)), 30);
        const shown = sheet.rows.slice(0, Math.max(PREVIEW_ROWS, state.headerRow + 6));
        $('[data-body]').innerHTML = `
            <div class="form-grid">
                <div class="field span-2"><label>Sheet</label>
                    <select id="imp-sheet">${state.sheets.map((s, i) => `<option value="${i}" ${i === state.sheetIndex ? 'selected' : ''}>${esc(s.name)} — ${s.rows.length} rows${s.hidden ? ' (hidden)' : ''}</option>`).join('')}</select></div>
                <div class="field"><label>Header row</label><input id="imp-header" type="number" min="1" max="${sheet.rows.length}" value="${state.headerRow + 1}"></div>
            </div>
            <p class="hint">The header row is detected automatically. Click a row in the preview to change it. Rows below it are imported.${sheet.truncated ? ' <b>Only the first rows of this sheet were read.</b>' : ''}</p>
            <div class="imp-grid"><table><thead><tr><th></th>${Array.from({ length: width }, (_, c) => `<th>${colLetter(c)}</th>`).join('')}</tr></thead><tbody>
                ${shown.map((r, i) => `<tr data-row="${i}" class="${i === state.headerRow ? 'imp-header' : i < state.headerRow ? 'imp-above' : ''}"><th>${i + 1}</th>${Array.from({ length: width }, (_, c) => `<td title="${esc(r[c] || '')}">${esc(clip(oneLine(r[c]), 40))}</td>`).join('')}</tr>`).join('')}
            </tbody></table></div>
            ${sheet.rows.length > shown.length ? `<p class="hint">${sheet.rows.length - shown.length} more rows not shown.</p>` : ''}`;
        $('#imp-sheet').addEventListener('change', e => { selectSheet(+e.target.value); renderSheet(); });
        $('#imp-header').addEventListener('change', e => {
            state.headerRow = Math.min(sheet.rows.length - 1, Math.max(0, (parseInt(e.target.value, 10) || 1) - 1));
            remap(); renderSheet();
        });
        $('[data-body]').querySelectorAll('tr[data-row]').forEach(tr => tr.addEventListener('click', () => { state.headerRow = +tr.dataset.row; remap(); renderSheet(); }));
        setActions('<button class="btn btn-ghost" data-act="close">Cancel</button><button class="btn" data-act="next">Next ›</button>');
    }

    // ── Step 2: columns & options ───────────────────────────────────────
    function fieldSelect(col, value) {
        return `<select data-col="${col}"><option value="ignore" ${value === 'ignore' ? 'selected' : ''}>— not imported —</option>
            ${FIELDS.map(([k, l]) => `<option value="${k}" ${k === value ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select>`;
    }

    function renderColumns() {
        const sheet = state.sheets[state.sheetIndex];
        const hs = headers();
        const data = sheet.rows.slice(state.headerRow + 1);
        const samples = c => [...new Set(data.map(r => r[c]).filter(Boolean))].slice(0, 3);
        const cols = hs.map((h, c) => ({ c, h, s: samples(c) })).filter(x => x.h || x.s.length);
        const hasCarCol = state.mapping.includes('car');
        const hasReimbCol = state.mapping.includes('to_reimburse');
        $('[data-body]').innerHTML = `
            <p class="hint">Columns were mapped from their header names (English, French or Dutch). Check each one; unmapped columns are not imported.
                <b>One row = one charging session.</b> Numbers may use a decimal comma (<code>12,5</code>); dates may be <code>31/12/2026</code>, <code>2026-12-31</code> or Excel dates;
                a date column holding a date and time also gives the start time.</p>
            <div class="imp-grid"><table><thead><tr><th>Col.</th><th>Header</th><th>Examples</th><th style="min-width:220px">Imported as</th></tr></thead><tbody>
                ${cols.map(({ c, h, s }) => `<tr class="${state.mapping[c] !== 'ignore' ? 'imp-mapped' : ''}"><th>${colLetter(c)}</th><td><b>${esc(h || '(no header)')}</b></td>
                    <td class="muted">${s.map(v => esc(clip(oneLine(v), 30))).join(' · ') || '—'}</td><td>${fieldSelect(c, state.mapping[c])}</td></tr>`).join('')}
            </tbody></table></div>
            <div class="form-grid" style="margin-top:14px">
                ${hasCarCol
                    ? `<div class="field span-2"><label class="check"><input type="checkbox" id="imp-create" ${state.createMissing ? 'checked' : ''}> Create the cars that don't exist yet</label>
                        <div class="hint">The car column is matched on the car name or license plate.</div></div>`
                    : `<div class="field span-2"><label>Car for every row *</label><select id="imp-car">${state.cars.map(c => `<option value="${c.id}" ${c.id === state.carId ? 'selected' : ''}>${esc(c.name)}${c.license_plate ? ' · ' + esc(c.license_plate) : ''}</option>`).join('') || '<option value="">(add a car first)</option>'}</select></div>`}
                <div class="field"><label>Dates are written</label>
                    <select id="imp-order">${[['auto', 'Detect automatically'], ['dmy', 'day/month/year'], ['mdy', 'month/day/year'], ['ymd', 'year-month-day']].map(([k, l]) => `<option value="${k}" ${k === state.dateOrder ? 'selected' : ''}>${l}</option>`).join('')}</select></div>
                ${hasReimbCol ? '' : `<div class="field"><label class="check" style="margin-top:22px"><input type="checkbox" id="imp-reimb" ${state.defaultReimb ? 'checked' : ''}> Mark all as to be reimbursed</label></div>`}
            </div>`;
        $('[data-body]').querySelectorAll('select[data-col]').forEach(sel => sel.addEventListener('change', () => {
            const col = +sel.dataset.col, field = sel.value;
            if (field !== 'ignore') state.mapping.forEach((f, i) => { if (f === field && i !== col) state.mapping[i] = 'ignore'; });  // a field maps to one column
            state.mapping[col] = field;
            renderColumns();
        }));
        $('#imp-create')?.addEventListener('change', e => { state.createMissing = e.target.checked; });
        $('#imp-car')?.addEventListener('change', e => { state.carId = Number(e.target.value) || null; });
        $('#imp-order').addEventListener('change', e => { state.dateOrder = e.target.value; });
        $('#imp-reimb')?.addEventListener('change', e => { state.defaultReimb = e.target.checked; });
        const missing = missingFields();
        setActions(`${missing ? `<span class="imp-missing">${esc(missing)}</span>` : ''}<button class="btn btn-ghost" data-act="back">‹ Back</button><button class="btn" data-act="next" ${missing ? 'disabled' : ''}>Next ›</button>`);
    }

    // ── Step 3: review (server-side parsing) & import ───────────────────
    function payload() {
        const mapping = {};
        state.mapping.forEach((f, col) => { if (f !== 'ignore') mapping[f] = col; });
        return {
            rows: state.sheets[state.sheetIndex].rows.slice(state.headerRow + 1),
            first_row_number: state.headerRow + 2,
            mapping,
            car_id: mapping.car === undefined ? state.carId : null,
            create_missing_cars: state.createMissing,
            date_order: state.dateOrder,
            default_to_reimburse: state.defaultReimb,
            skip_duplicates: state.skipDuplicates,
        };
    }

    const STATUS = {
        ok: '<span class="badge badge-ok">✓ OK</span>',
        duplicate: '<span class="badge badge-warn">≡ Duplicate</span>',
        error: '<span class="badge badge-err">✖ Error</span>',
    };

    async function renderReview() {
        $('[data-body]').innerHTML = '<p class="muted">Checking the rows…</p>';
        setActions('<button class="btn btn-ghost" data-act="back">‹ Back</button>');
        let res;
        const token = state.reviewToken = {};
        try { res = await EV.api('POST', '/api/import/sessions/preview', payload()); }
        catch (e) { if (token === state.reviewToken) { $('[data-body]').innerHTML = ''; alertBox(e.message); } return; }
        if (token !== state.reviewToken || state.step !== 3) return;  // the user went back meanwhile
        state.preview = res;
        const s = res.summary;
        const filter = state.reviewFilter || 'all';
        const rows = res.rows.filter(r => filter === 'all' || r.status === filter);
        const mapped = new Set(state.mapping.filter(f => f !== 'ignore'));
        const cols = [['car_name', 'Car'], ['charge_date', 'Date'], ['odometer', 'Odometer'], ['location', 'Location'], ['provider', 'Provider'], ['kwh', 'kWh'],
            ['start_time', 'Start'], ['end_time', 'End'], ['to_reimburse', 'Reimb.'], ['cost', 'Cost'], ['notes', 'Notes']]
            .filter(([k]) => k === 'car_name' || k === 'charge_date' || k === 'kwh' || mapped.has(k) || (k === 'end_time' && mapped.has('duration')) || (k === 'to_reimburse' && state.defaultReimb));
        const fmt = (r, k) => {
            const v = r[k];
            if (k === 'charge_date') return v ? EV.date(v) : '';
            if (k === 'kwh' || k === 'cost') return v == null ? '' : EV.num(v, 2);
            if (k === 'odometer') return v == null ? '' : EV.num(v);
            if (k === 'to_reimburse') return v ? '💶 Yes' : '';
            return esc(clip(v || '', 40));
        };
        $('[data-body]').innerHTML = `
            <p><b>${EV.num(s.total)}</b> row(s): <b>${EV.num(s.ok)}</b> OK · <b>${EV.num(s.duplicates)}</b> duplicate(s) · <b>${EV.num(s.errors)}</b> with errors.
               <b>${EV.num(s.to_import)}</b> session(s), ${EV.kwh(s.kwh)} will be imported.</p>
            ${s.new_cars.length ? `<p class="hint">New car(s) to create: <b>${s.new_cars.map(esc).join(', ')}</b> (complete their specs and lease on the Cars page).</p>` : ''}
            <div class="form-grid" style="margin:10px 0">
                <div class="field"><label>Show</label><select id="imp-filter">${[['all', 'All rows'], ['ok', 'OK'], ['duplicate', 'Duplicates'], ['error', 'Errors']].map(([k, l]) => `<option value="${k}" ${k === filter ? 'selected' : ''}>${l}</option>`).join('')}</select></div>
                ${s.duplicates ? `<div class="field span-2"><label class="check" style="margin-top:22px"><input type="checkbox" id="imp-skipdup" ${state.skipDuplicates ? 'checked' : ''}> Skip duplicates (same car, date, kWh and odometer)</label></div>` : ''}
            </div>
            <div class="imp-grid"><table><thead><tr><th>Row</th><th>Status</th>${cols.map(([, l]) => `<th>${l}</th>`).join('')}<th>Messages</th></tr></thead><tbody>
                ${rows.slice(0, 1000).map(r => `<tr class="${r.status === 'error' ? 'imp-error' : r.status === 'duplicate' && state.skipDuplicates ? 'imp-above' : ''}"><th>${r.row}</th><td>${STATUS[r.status]}</td>
                    ${cols.map(([k]) => `<td class="${['kwh', 'cost', 'odometer'].includes(k) ? 'num' : ''}">${fmt(r, k)}</td>`).join('')}
                    <td class="small">${[...r.errors.map(e => `<span class="imp-err-text">${esc(e)}</span>`), ...r.warnings.map(w => `<span class="muted">${esc(w)}</span>`)].join('<br>')}</td></tr>`).join('')}
                ${rows.length ? '' : `<tr><td colspan="${cols.length + 3}" class="muted">No row.</td></tr>`}
            </tbody></table></div>
            ${s.errors ? '<p class="hint">Rows with errors are not imported: fix them in the file, or go back and check the column mapping and the date order.</p>' : ''}`;
        $('#imp-filter').addEventListener('change', e => { state.reviewFilter = e.target.value; renderReview(); });
        $('#imp-skipdup')?.addEventListener('change', e => { state.skipDuplicates = e.target.checked; renderReview(); });
        setActions(`<button class="btn btn-ghost" data-act="back">‹ Back</button><button class="btn" data-act="import" ${s.to_import ? '' : 'disabled'}>⬆ Import ${EV.num(s.to_import)} session(s)</button>`);
    }

    async function doImport(button) {
        button.disabled = true;
        try {
            const r = await EV.api('POST', '/api/import/sessions', payload());
            EV.closeModal('import-modal');
            EV.toast(`${r.imported} session(s) imported${r.cars_created.length ? `, ${r.cars_created.length} car(s) created` : ''}`);
            EV.cars = null;
            state.onDone && state.onDone(r);
        } catch (e) {
            button.disabled = false;
            alertBox('Import failed: ' + e.message);
        }
    }

    EV.importWizard = { open, _internals: { autoMap, headerCandidate, matchScore } };
})();
