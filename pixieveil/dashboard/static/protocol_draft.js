/* Draft a protocol from a study: local catalog entry + RHYTHM CTProtocol.
 * PV.protocolDraft(studyNumber, { onDone }) — used by /protocols (Unmatched
 * inbox) and /studies (study detail). The server pre-fills everything it can
 * (GET /api/studies/{n}/protocol-draft); Create sends the form back
 * (POST), which creates the protocol on RHYTHM and adds it locally. */
(function () {
    'use strict';
    const { esc, api, toast, icon } = PV;
    const OTHER = 'Other: Please Specify';
    const FIELDS = [
        ['examination_group', 'Examination group'], ['age_group', 'Age / weight group'],
        ['scan_type', 'Scan type'], ['contrast', 'Contrast'], ['number_of_phases', 'Number of phases'],
        ['auto_kvp_selection', 'Automatic kVp selection'], ['kvp', 'kVp (ref)'],
        ['auto_ma_modulation', 'Automatic mA modulation'], ['pitch', 'Pitch'],
        ['rotation_time', 'Rotation time (s)'], ['slice_thickness', 'Slice thickness, 1st recon (mm)'],
        ['scan_fov', 'Scan FOV'], ['kernel_class', 'Kernel class'],
        ['reconstruction_algorithm', 'Reconstruction algorithm'], ['protocol_intent', 'Protocol intent'],
    ];
    const REQUIRED = new Set(['examination_group', 'age_group', 'scan_type', 'contrast',
        'auto_kvp_selection', 'kvp', 'auto_ma_modulation', 'pitch', 'rotation_time',
        'slice_thickness', 'kernel_class', 'reconstruction_algorithm']);
    const GROUP_KEY = { PEDIATRIC_HEAD: 'examination_group_pediatric_head',
        PEDIATRIC_BODY: 'examination_group_pediatric_body', YOUNG_ADULT: 'examination_group_young_adult' };
    const AGE_KEY = { PEDIATRIC_HEAD: 'age_group_pediatric_head',
        PEDIATRIC_BODY: 'age_group_pediatric_body', YOUNG_ADULT: 'age_group_young_adult' };
    const TYPES = [['PEDIATRIC_HEAD', 'Pediatric head'], ['PEDIATRIC_BODY', 'Pediatric body'],
        ['YOUNG_ADULT', 'Young adult']];
    const BADGE = { study: ['ok', 'from study'], guess: ['warn', 'guess — check'], fill: ['bad', 'please fill'] };

    let styled = false;
    function addStyle() {
        if (styled) return;
        styled = true;
        const st = document.createElement('style');
        st.textContent = `
            dialog.modal.draft { width: min(1100px, 97vw); max-width: none; }
            .draft .cols { display: grid; grid-template-columns: 1fr 1.6fr; gap: 18px; max-height: 70vh; overflow: auto; padding-right: 4px; }
            @media (max-width: 860px) { .draft .cols { grid-template-columns: 1fr; } }
            .draft h3 { font-size: 14px; margin: 0 0 10px; }
            .draft .grid { display: grid; gap: 10px 14px; grid-template-columns: repeat(auto-fill, minmax(210px, 1fr)); }
            .draft .field .label { display: flex; justify-content: space-between; gap: 6px; align-items: center; }
            .draft .badge-s { font-size: 10px; padding: 1px 6px; border-radius: 999px; white-space: nowrap; }
            .draft .badge-s.ok { color: var(--ok); background: var(--ok-soft); }
            .draft .badge-s.warn { color: var(--warn); background: var(--warn-soft); }
            .draft .badge-s.bad { color: var(--bad); background: var(--bad-soft); }
            .draft .hint { font-size: 11px; color: var(--text-faint); }
            .draft .err { color: var(--bad); font-size: 11.5px; }
            .draft .field.has-error .select, .draft .field.has-error .input { border-color: var(--bad); }
            .draft .other { margin-top: 4px; }
            .draft .existing { border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 8px 10px; margin-top: 12px; font-size: 12.5px; }
            .draft .existing li { margin: 4px 0; }
            .draft .callout { margin-bottom: 10px; }
            .draft textarea.input { min-height: 54px; height: auto; }`;
        document.head.appendChild(st);
    }

    function choiceApi(choices, manufacturer) {
        const cats = {};
        for (const c of choices.categories || []) cats[c.key] = c;
        const vendor = (choices.manufacturer_field_options || {})[manufacturer] || {};
        return {
            options(key, ptype) {
                if ((key === 'auto_kvp_selection' || key === 'auto_ma_modulation') && vendor[key]) {
                    return vendor[key].map(v => ({ value: v, display: v }));
                }
                const real = key === 'examination_group' ? GROUP_KEY[ptype]
                    : key === 'age_group' ? AGE_KEY[ptype] : key;
                const cat = cats[real];
                if (!cat) return [];
                return (cat.options || []).filter(o => {
                    const t = o.applicable_protocol_types || [];
                    return !ptype || !t.length || t.includes(ptype);
                }).map(o => ({ value: String(o.value), display: String(o.display) }));
            },
            maLabels(mod) { return (choices.ma_modulation_inputs || {})[mod] || []; },
        };
    }

    async function protocolDraft(n, opts) {
        opts = opts || {};
        addStyle();
        let res;
        try { res = await api(`/api/studies/${n}/protocol-draft`); }
        catch (e) { toast(e.message, 'bad', 8000); return; }
        const d = res.draft;
        const ch = choiceApi(res.choices, d.rhythm.manufacturer);
        const form = {
            local: JSON.parse(JSON.stringify(d.local)),
            ptype: d.rhythm.fields.protocol_type.value || '',
            scanner: res.profile_match || '',
            fields: {}, mas: Object.assign({}, d.rhythm.fields.mas_inputs.value || {}),
            protocol_name: d.rhythm.protocol_name || '',
            region: d.rhythm.anatomical_region || '', indication: d.rhythm.clinical_indication || '',
            notes: d.rhythm.notes || '',
        };
        for (const [k] of FIELDS) {
            const f = d.rhythm.fields[k] || {};
            form.fields[k] = { value: f.value || '', other: f.other || '', source: f.source || 'fill', note: f.note || '' };
        }

        const dlg = document.createElement('dialog');
        dlg.className = 'modal draft';
        document.body.appendChild(dlg);
        dlg.addEventListener('close', () => dlg.remove());

        function badge(src) {
            const [tone, text] = BADGE[src] || BADGE.fill;
            return `<span class="badge-s ${tone}">${text}</span>`;
        }
        function selectHtml(name, list, value, extra) {
            const has = list.some(o => o.value === value);
            return `<select class="select" data-k="${esc(name)}" ${extra || ''}>
                <option value="">—</option>
                ${!has && value ? `<option value="${esc(value)}" selected>${esc(value)} (not a RHYTHM option)</option>` : ''}
                ${list.map(o => `<option value="${esc(o.value)}" ${o.value === value ? 'selected' : ''}>${esc(o.display)}</option>`).join('')}
            </select>`;
        }
        function rhythmFieldsHtml() {
            return FIELDS.map(([k, label]) => {
                const f = form.fields[k];
                const list = ch.options(k, form.ptype);
                const src = f.value ? f.source : 'fill';
                return `<div class="field" data-field="rhythm.${k}">
                    <span class="label"><span>${esc(label)}${REQUIRED.has(k) ? ' *' : ''}</span>${badge(src)}</span>
                    ${selectHtml(k, list, f.value)}
                    <input class="input other" data-other="${k}" placeholder="specify" value="${esc(f.other)}" ${f.value === OTHER ? '' : 'hidden'}>
                    ${f.note ? `<span class="hint">${esc(f.note)}</span>` : ''}
                    <span class="err" hidden></span></div>`;
            }).join('');
        }
        function masHtml() {
            const labels = ch.maLabels(form.fields.auto_ma_modulation.value);
            if (!labels.length) return '';
            const m = d.rhythm.fields.mas_inputs;
            return `<div class="grid" style="margin-top:10px">${labels.map(l => `
                <div class="field" data-field="rhythm.mas_inputs.${esc(l)}">
                    <span class="label"><span>${esc(l)} *</span>${badge(form.mas[l] ? (m.source || 'guess') : 'fill')}</span>
                    <input class="input" data-mas="${esc(l)}" value="${esc(form.mas[l] || '')}">
                    <span class="err" hidden></span></div>`).join('')}</div>
                ${m.note && labels.some(l => form.mas[l]) ? `<p class="hint">${esc(m.note)}</p>` : ''}`;
        }
        function existingHtml() {
            if (!res.existing.length) return '';
            return `<div class="existing"><b>Similar protocols already on RHYTHM</b> — link one instead of creating a duplicate:
                <ul>${res.existing.map(p => `<li><span class="mono">${esc(p.protocol_name || '(no name)')}</span>
                    · ${esc(p.protocol_type || '')} · ${esc(p.age_group || '')}
                    <button type="button" class="btn btn-sm" data-use="${esc(p.id)}">Use this one</button></li>`).join('')}</ul></div>`;
        }
        function render() {
            const L = form.local;
            const combos = res.region_indications;
            const comboIdx = combos.findIndex(c => c.region === L.region && c.indication === L.clinical_indication);
            const banners = [];
            if (!res.rhythm_configured) banners.push(`<div class="callout bad">${icon('alert')}<div>RHYTHM is not configured —
                set the base URL and API token on <a href="/settings">Settings</a> first.</div></div>`);
            else if (res.rhythm_error) banners.push(`<div class="callout warn">${icon('alert')}<div>${esc(res.rhythm_error)}</div></div>`);
            if (res.choices_source !== 'live') banners.push(`<div class="callout info">${icon('info')}<div>Using the bundled
                (offline) list of RHYTHM options; the live list will be used once RHYTHM is reachable.</div></div>`);
            dlg.innerHTML = `<form method="dialog" novalidate>
                <div class="modal-body">
                    <h2>Draft protocol from study ${String(n).padStart(4, '0')}</h2>
                    ${banners.join('')}
                    <div class="callout bad" data-errors hidden></div>
                    <div class="cols">
                        <section>
                            <h3>Local protocol (this server)</h3>
                            <div class="field" data-field="local.scanner_id"><span class="label">Scanner</span>
                                <select class="select" data-l="scanner_id">
                                    ${L.new_scanner ? `<option value="" ${!L.scanner_id ? 'selected' : ''}>New: ${esc(L.new_scanner.name)}</option>` : '<option value="">—</option>'}
                                    ${res.local_scanners.map(s => `<option value="${esc(s.id)}" ${s.id === L.scanner_id ? 'selected' : ''}>${esc(s.name)}</option>`).join('')}
                                </select><span class="err" hidden></span></div>
                            <div class="field" data-field="local.names" style="margin-top:10px"><span class="label">Matches names (comma-separated)</span>
                                <input class="input mono" data-l="names" value="${esc((L.names || []).join(', '))}">
                                <span class="hint">Exact ProtocolName(s) the scanner sends.</span><span class="err" hidden></span></div>
                            <div class="field" style="margin-top:10px"><span class="label">Label</span>
                                <input class="input" data-l="label" value="${esc(L.label || '')}"></div>
                            <div class="field" data-field="local.region" style="margin-top:10px"><span class="label">Region / indication</span>
                                <select class="select" data-l="combo"><option value="">—</option>
                                    ${combos.map((c, i) => `<option value="${i}" ${i === comboIdx ? 'selected' : ''}>${esc(c.region)} — ${esc(c.indication)}</option>`).join('')}
                                </select><span class="err" hidden></span></div>
                            <div class="field" style="margin-top:10px"><span class="label">IV contrast</span>
                                <select class="select" data-l="iv_contrast"><option value="">—</option>
                                    ${res.iv_contrast.map(c => `<option ${c === L.iv_contrast ? 'selected' : ''}>${esc(c)}</option>`).join('')}
                                </select></div>
                            <p class="hint" style="margin-top:12px">Patient: ${d.patient.age_years ?? '?'} y, ${d.patient.weight_kg ?? '?'} kg.
                                Values marked <b>from study</b> were read from the images and dose report.</p>
                        </section>
                        <section>
                            <h3>RHYTHM protocol</h3>
                            <div class="grid">
                                <div class="field" data-field="rhythm.scanner"><span class="label"><span>RHYTHM scanner *</span></span>
                                    <select class="select" data-r="scanner"><option value="">—</option>
                                        ${res.profiles.map(p => `<option value="${esc(p.id)}" ${p.id === form.scanner ? 'selected' : ''}>${esc(p.manufacturer || '')} ${esc(p.model || '')}</option>`).join('')}
                                    </select>
                                    ${!res.profiles.length && res.rhythm_configured ? '<span class="hint">No scanner profiles for your site on RHYTHM — create one there first.</span>' : ''}
                                    <span class="err" hidden></span></div>
                                <div class="field" data-field="rhythm.protocol_type"><span class="label"><span>Protocol type *</span>${badge(form.ptype ? d.rhythm.fields.protocol_type.source : 'fill')}</span>
                                    <select class="select" data-r="ptype"><option value="">—</option>
                                        ${TYPES.map(([v, t]) => `<option value="${v}" ${v === form.ptype ? 'selected' : ''}>${t}</option>`).join('')}
                                    </select>
                                    ${d.rhythm.fields.protocol_type.note ? `<span class="hint">${esc(d.rhythm.fields.protocol_type.note)}</span>` : ''}
                                    <span class="err" hidden></span></div>
                                <div class="field" data-field="rhythm.protocol_name"><span class="label">Protocol name *</span>
                                    <input class="input mono" data-r="protocol_name" value="${esc(form.protocol_name)}"><span class="err" hidden></span></div>
                                ${rhythmFieldsHtml()}
                            </div>
                            ${masHtml()}
                            <div class="field" style="margin-top:10px"><span class="label">Notes</span>
                                <textarea class="input" data-r="notes">${esc(form.notes)}</textarea></div>
                            ${existingHtml()}
                        </section>
                    </div>
                </div>
                <div class="modal-actions">
                    <button type="button" class="btn btn-ghost" data-cancel>Cancel</button>
                    <button type="button" class="btn btn-primary" data-create ${res.rhythm_configured ? '' : 'disabled'}>Create on RHYTHM &amp; add locally</button>
                </div></form>`;
        }

        function collect() {
            const q = sel => dlg.querySelector(sel);
            const L = form.local;
            L.scanner_id = q('[data-l="scanner_id"]').value || null;
            L.names = q('[data-l="names"]').value.split(',').map(s => s.trim()).filter(Boolean);
            L.label = q('[data-l="label"]').value.trim();
            const ci = q('[data-l="combo"]').value;
            const combo = ci === '' ? null : res.region_indications[Number(ci)];
            L.region = combo ? combo.region : null;
            L.clinical_indication = combo ? combo.indication : null;
            L.iv_contrast = q('[data-l="iv_contrast"]').value || null;
            form.scanner = q('[data-r="scanner"]').value;
            form.ptype = q('[data-r="ptype"]').value;
            form.protocol_name = q('[data-r="protocol_name"]').value.trim();
            form.notes = q('[data-r="notes"]').value;
            dlg.querySelectorAll('[data-k]').forEach(el => {
                const f = form.fields[el.dataset.k];
                if (f.value !== el.value) f.source = 'study';   // operator's choice now
                f.value = el.value;
            });
            dlg.querySelectorAll('[data-other]').forEach(el => { form.fields[el.dataset.other].other = el.value.trim(); });
            dlg.querySelectorAll('[data-mas]').forEach(el => { form.mas[el.dataset.mas] = el.value.trim(); });
        }

        function showErrors(errors) {
            dlg.querySelectorAll('.field.has-error').forEach(e => e.classList.remove('has-error'));
            dlg.querySelectorAll('.field .err').forEach(e => { e.hidden = true; });
            const loose = [];
            for (const [k, msg] of Object.entries(errors || {})) {
                const host = dlg.querySelector(`[data-field="${CSS.escape(k)}"]`);
                if (host) { host.classList.add('has-error'); const e = host.querySelector('.err'); e.textContent = msg; e.hidden = false; }
                else loose.push(`${k}: ${msg}`);
            }
            const box = dlg.querySelector('[data-errors]');
            box.innerHTML = `${icon('alert')}<div><strong>Not created.</strong> Fix the highlighted fields.${loose.length ? '<br>' + loose.map(esc).join('<br>') : ''}</div>`;
            box.hidden = false;
            box.scrollIntoView({ block: 'nearest' });
        }

        async function submit(useExisting) {
            collect();
            const body = { local: form.local };
            if (useExisting) body.use_existing = useExisting;
            else {
                const fields = {};
                for (const [k] of FIELDS) fields[k] = { value: form.fields[k].value, other: form.fields[k].other };
                body.rhythm = { scanner: form.scanner, protocol_type: form.ptype, fields, mas_inputs: form.mas,
                    protocol_name: form.protocol_name, anatomical_region: form.local.region || form.region,
                    clinical_indication: form.local.clinical_indication || form.indication, notes: form.notes,
                    manufacturer: d.rhythm.manufacturer };
            }
            const btn = dlg.querySelector('[data-create]');
            btn.disabled = true;
            try {
                const r = await api(`/api/studies/${n}/protocol-draft`, { method: 'POST', body });
                if (!r.ok) {
                    toast(r.error, 'bad', 15000);
                    return;
                }
                dlg.close();
                toast(`${r.linked_existing ? 'Linked' : 'Created on RHYTHM'} (${r.rhythm_id}) and added locally as "${r.local_key}"`
                    + (r.restudied ? `; ${r.restudied} study(ies) re-matched` : '') + '.', 'ok', 10000);
                (r.warnings || []).forEach(w => toast(w, 'bad', 15000));
                if (opts.onDone) opts.onDone(r);
            } catch (e) {
                if (e.data && e.data.errors && Object.keys(e.data.errors).length) showErrors(e.data.errors);
                else toast(e.message, 'bad', 10000);
            } finally { if (dlg.open) btn.disabled = !res.rhythm_configured; }
        }

        dlg.addEventListener('change', e => {
            const t = e.target;
            if (t.dataset.r === 'ptype' || t.dataset.k === 'auto_ma_modulation') {
                collect();
                if (t.dataset.r === 'ptype') {
                    // Groups are per protocol type: keep a value only if the new type offers it.
                    for (const k of ['examination_group', 'age_group']) {
                        const f = form.fields[k];
                        if (!ch.options(k, form.ptype).some(o => o.value === f.value)) {
                            f.value = ''; f.source = 'fill'; f.note = '';
                        }
                    }
                    const only = k => { const o = ch.options(k, form.ptype); return o.length === 1 ? o[0].value : ''; };
                    for (const k of ['examination_group', 'age_group']) {
                        if (!form.fields[k].value && only(k)) form.fields[k].value = only(k);
                    }
                }
                render();
                return;
            }
            if (t.dataset.k) {
                const o = dlg.querySelector(`[data-other="${t.dataset.k}"]`);
                if (o) o.hidden = t.value !== OTHER;
            }
        });
        dlg.addEventListener('click', e => {
            if (e.target.closest('[data-cancel]')) { dlg.close(); return; }
            if (e.target.closest('[data-create]')) { submit(null); return; }
            const use = e.target.closest('[data-use]');
            if (use) submit(use.dataset.use);
        });
        render();
        dlg.showModal();
    }

    PV.protocolDraft = protocolDraft;   // PV is common.js's global const
})();
