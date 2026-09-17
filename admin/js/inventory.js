/**
 * inventory.js — Drug Master + Stock Lot management (admin Inventory tab).
 * New stock lots are created by receiving a delivery, not by a direct add form.
 */
(function () {
    let masterDrugs = [];
    let lots = [];
    let activeCardFilter = 'all';
    let suppliersById = {};
    const INV_PAGE_SIZE = 10;
    let lotPage = 1;
    let drugPage = 1;

    function updateInvPager(prefix, page, pages, total) {
        const info = document.getElementById(prefix + '-page-info');
        const prev = document.getElementById(prefix + '-prev-btn');
        const next = document.getElementById(prefix + '-next-btn');
        if (info) {
            info.textContent = total
                ? `Page ${page} of ${pages} (${total} result${total === 1 ? '' : 's'})`
                : 'Page 1 of 1 (0 results)';
        }
        [prev, next].forEach((btn, i) => {
            if (!btn) return;
            btn.disabled = i === 0 ? page <= 1 : (page >= pages || total === 0);
            btn.style.opacity = btn.disabled ? '0.5' : '1';
            btn.style.cursor = btn.disabled ? 'not-allowed' : 'pointer';
        });
    }

    function slicePage(rows, page) {
        const total = rows.length;
        const pages = Math.max(1, Math.ceil(total / INV_PAGE_SIZE) || 1);
        if (page > pages) page = pages;
        if (page < 1) page = 1;
        const start = (page - 1) * INV_PAGE_SIZE;
        return { page, pages, total, slice: rows.slice(start, start + INV_PAGE_SIZE) };
    }

    function escapeHtml(str) {
        if (str === null || str === undefined) return '';
        return String(str).replace(/[&<>"']/g, c => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
        }[c]));
    }

    function money(n) {
        return '₱' + Number(n || 0).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }

    function daysUntil(dateStr) {
        const today = new Date();
        today.setHours(0, 0, 0, 0);
        const parts = String(dateStr || '').slice(0, 10).split('-');
        if (parts.length < 3) return 9999;
        const d = new Date(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2]));
        return Math.round((d - today) / 86400000);
    }

    // ---------------------------------------------------------------
    // Modals (open/close only — markup already exists in the host page)
    // ---------------------------------------------------------------
    function showModal(id) { const m = document.getElementById(id); if (m) m.style.display = 'flex'; }
    function hideModal(id) { const m = document.getElementById(id); if (m) m.style.display = 'none'; }
    function manilaTodayYmd() {
        return new Date().toLocaleDateString('en-CA', { timeZone: 'Asia/Manila' });
    }

    function lockDateNoPast(el) {
        if (!el) return;
        el.min = manilaTodayYmd();
        if (el.value && el.value < el.min) el.value = el.min;
    }

    function switchInvPanel(panel) {
        document.querySelectorAll('.inv-tab-btn').forEach(btn => {
            btn.classList.toggle('active', btn.getAttribute('data-inv-panel') === panel);
        });
        document.querySelectorAll('.inv-panel').forEach(el => {
            el.classList.toggle('active', el.id === 'inv-panel-' + panel);
        });
    }

    function allCategories() {
        const set = new Set(Object.keys(categoryMarkups || {}));
        masterDrugs.forEach(d => { if (d.category) set.add(d.category); });
        return Array.from(set).sort((a, b) => a.localeCompare(b, undefined, { sensitivity: 'base' }));
    }

    const DRUG_FORMS = [
        'Tablet', 'Capsule', 'Syrup', 'Suspension', 'Drops', 'Cream', 'Ointment',
        'Gel', 'Lotion', 'Inhaler', 'Injection', 'Ampule', 'Vial', 'Sachet',
        'Suppository', 'Patch', 'Solution', 'Powder', 'Spray',
    ];
    const DOSAGE_UNITS = ['mg', 'mcg', 'g', 'mL', '%', 'IU', 'mg/5mL', 'mg/mL', 'mcg/mL', 'puff', 'N/A'];

    function fillSelectOptions(sel, values, selected, extraLabel) {
        if (!sel) return;
        const opts = values.map(v => `<option value="${escapeHtml(v)}"${v === selected ? ' selected' : ''}>${escapeHtml(v)}</option>`).join('');
        sel.innerHTML = opts + (extraLabel ? `<option value="__other__">${escapeHtml(extraLabel)}</option>` : '');
        if (selected && ![...sel.options].some(o => o.value === selected)) {
            sel.value = '__other__';
        } else if (selected) {
            sel.value = selected;
        }
    }

    function toggleCustomField(selectId, customId, otherValue) {
        const sel = document.getElementById(selectId);
        const custom = document.getElementById(customId);
        if (!sel || !custom) return;
        const show = sel.value === otherValue;
        custom.style.display = show ? 'block' : 'none';
        custom.required = show;
        if (!show) custom.value = '';
    }

    function composeDosage(prefix) {
        const unit = document.getElementById(prefix + '_dosage_unit')?.value || '';
        const val = document.getElementById(prefix + '_dosage_value')?.value;
        if (unit === 'N/A') return 'N/A';
        if (val === '' || val === null || val === undefined) return '';
        return String(val).trim() + unit;
    }

    function splitDosage(raw) {
        const s = String(raw || '').trim();
        if (!s) return { value: '', unit: 'mg' };
        if (/^n\/?a$/i.test(s)) return { value: '', unit: 'N/A' };
        const m = s.match(/^(\d+(?:\.\d+)?)\s*(.+)$/);
        if (m) return { value: m[1], unit: m[2].trim() };
        return { value: '', unit: s };
    }

    function pickedForm(prefix) {
        const sel = document.getElementById(prefix + '_form');
        if (!sel) return '';
        if (sel.value === '__other__') return (document.getElementById(prefix + '_form_custom')?.value || '').trim();
        return sel.value;
    }

    function pickedCategory(prefix) {
        const sel = document.getElementById(prefix + '_category');
        if (!sel) return '';
        if (sel.value === '__new__') return (document.getElementById(prefix + '_category_custom')?.value || '').trim();
        return sel.value;
    }

    function refreshGenericDatalist() {
        const list = document.getElementById('genericNameSuggestions');
        if (!list) return;
        const names = [...new Set(masterDrugs.map(d => d.generic_name).filter(Boolean))].sort();
        list.innerHTML = names.map(n => `<option value="${escapeHtml(n)}"></option>`).join('');
        refreshCopyFromSelect();
    }

    function refreshCopyFromSelect() {
        const sel = document.getElementById('new_copy_from');
        if (!sel) return;
        const prev = sel.value;
        const active = masterDrugs.filter(d => Number(d.is_active) === 1).slice().sort((a, b) =>
            String(a.generic_name || '').localeCompare(String(b.generic_name || ''), undefined, { sensitivity: 'base' })
        );
        sel.innerHTML = '<option value="">Start blank — or pick a catalog drug to copy</option>' +
            active.map(d => {
                const brand = d.brand_name ? ` (${d.brand_name})` : '';
                return `<option value="${d.drug_id}">${escapeHtml(d.generic_name)}${escapeHtml(brand)} — ${escapeHtml(d.dosage)}, ${escapeHtml(d.form)}</option>`;
            }).join('');
        if (prev && [...sel.options].some(o => o.value === prev)) sel.value = prev;
    }

    function refreshBrandDatalist(genericName) {
        const list = document.getElementById('brandNameSuggestions');
        if (!list) return;
        const q = String(genericName || '').trim().toLowerCase();
        let brands = masterDrugs.map(d => d.brand_name).filter(Boolean);
        if (q) {
            const matched = masterDrugs.filter(d => String(d.generic_name || '').toLowerCase() === q).map(d => d.brand_name).filter(Boolean);
            if (matched.length) brands = matched;
        }
        list.innerHTML = [...new Set(brands)].sort().map(n => `<option value="${escapeHtml(n)}"></option>`).join('');
    }

    function mostCommon(values) {
        const counts = {};
        values.forEach(v => {
            const key = String(v || '').trim();
            if (!key) return;
            counts[key] = (counts[key] || 0) + 1;
        });
        return Object.entries(counts).sort((a, b) => b[1] - a[1])[0]?.[0] || '';
    }

    function setSelectValue(sel, value, customId, otherValue) {
        if (!sel || !value) return;
        if (![...sel.options].some(o => o.value === value)) {
            if (otherValue === '__new__' || otherValue === '__other__') {
                const opt = document.createElement('option');
                opt.value = value;
                opt.textContent = value;
                const marker = sel.querySelector(`option[value="${otherValue}"]`);
                if (marker) sel.insertBefore(opt, marker);
                else sel.appendChild(opt);
                sel.value = value;
            } else {
                sel.value = otherValue || value;
            }
        } else {
            sel.value = value;
        }
        if (customId) toggleCustomField(sel.id, customId, otherValue);
        if (sel.value === otherValue) {
            const custom = document.getElementById(customId);
            if (custom && !custom.value) custom.value = value;
        }
    }

    function fillNewDrugFromRecord(drug, overwrite) {
        if (!drug) return;
        const setVal = (id, val) => {
            const el = document.getElementById(id);
            if (!el) return;
            if (val === undefined || val === null) return;
            if (!overwrite && String(el.value || '').trim()) return;
            el.value = val;
        };
        setVal('new_generic_name', drug.generic_name);
        setVal('new_brand_name', drug.brand_name || '');
        const dose = splitDosage(drug.dosage);
        if (overwrite || !document.getElementById('new_dosage_value')?.value) {
            const valEl = document.getElementById('new_dosage_value');
            const unitEl = document.getElementById('new_dosage_unit');
            if (valEl) valEl.value = dose.unit === 'N/A' ? '' : dose.value;
            if (unitEl) unitEl.value = DOSAGE_UNITS.includes(dose.unit) ? dose.unit : (dose.unit ? dose.unit : 'mg');
            if (unitEl && dose.unit === 'N/A') unitEl.value = 'N/A';
            if (valEl) valEl.required = unitEl?.value !== 'N/A';
        }
        setSelectValue(document.getElementById('new_form'), drug.form, 'new_form_custom', '__other__');
        setSelectValue(document.getElementById('new_category'), drug.category, 'new_category_custom', '__new__');
        if (overwrite || document.getElementById('new_minimum_stock')?.value === '20' || !document.getElementById('new_minimum_stock')?.value) {
            setVal('new_minimum_stock', String(drug.minimum_stock ?? 20));
        }
        const proc = String(drug.procurement_type || 'purchase').toLowerCase() === 'consignment' ? 'consignment' : 'purchase';
        const procEl = document.getElementById('new_procurement_type');
        if (procEl && (overwrite || !procEl.value)) procEl.value = proc;
        const hint = document.getElementById('new_autofill_hint');
        if (hint) hint.textContent = 'Filled from catalog. Edit dosage if this is a new strength.';
        refreshBrandDatalist(drug.generic_name);
    }

    function suggestFromGeneric(name, prefix) {
        if (prefix !== 'new') return;
        const q = String(name || '').trim().toLowerCase();
        refreshBrandDatalist(name);
        const hint = document.getElementById('new_autofill_hint');
        if (q.length < 3) {
            if (hint) hint.textContent = '';
            return;
        }
        const exact = masterDrugs.filter(d => String(d.generic_name || '').toLowerCase() === q);
        const pool = exact.length ? exact : masterDrugs.filter(d => String(d.generic_name || '').toLowerCase().startsWith(q));
        if (!pool.length) {
            if (hint) hint.textContent = '';
            return;
        }
        const cat = mostCommon(pool.map(d => d.category));
        const form = mostCommon(pool.map(d => d.form));
        const proc = mostCommon(pool.map(d => d.procurement_type || 'purchase')) || 'purchase';
        const mins = pool.map(d => Number(d.minimum_stock)).filter(n => Number.isFinite(n));
        const minStock = mins.length ? String(Math.round(mins.reduce((a, b) => a + b, 0) / mins.length)) : '';
        const brands = [...new Set(pool.map(d => d.brand_name).filter(Boolean))];
        const template = {
            generic_name: pool[0].generic_name,
            brand_name: brands.length === 1 ? brands[0] : '',
            dosage: pool.length === 1 ? pool[0].dosage : '',
            form,
            category: cat,
            minimum_stock: minStock,
            procurement_type: proc,
        };
        fillNewDrugFromRecord(template, false);
        if (hint && (cat || form)) {
            hint.textContent = `Suggested from ${pool.length} catalog match${pool.length === 1 ? '' : 'es'}: ${[form, cat].filter(Boolean).join(', ')}.`;
        }
    }

    function clientDrugError(payload) {
        const nameRe = /^[A-Za-z0-9Ññ][A-Za-z0-9Ññ\s.'/()+\-]{1,79}$/;
        if (!payload.generic_name) return 'Generic name is required.';
        if (!nameRe.test(payload.generic_name)) return 'Generic name: 2–80 characters, letters/numbers only (plus . \' / ( ) - +).';
        if (payload.brand_name && !nameRe.test(payload.brand_name)) return 'Brand name: 2–80 characters, letters/numbers only (plus . \' / ( ) - +).';
        if (!payload.dosage) return 'Dosage is required.';
        if (!payload.form) return 'Form is required.';
        if (!payload.category) return 'Category is required.';
        const min = Number(payload.minimum_stock);
        if (!Number.isInteger(min) || min < 0 || min > 100000) return 'Minimum stock must be a whole number from 0 to 100,000.';
        if (payload.barcode && !/^[A-Za-z0-9\-._]{4,64}$/.test(payload.barcode)) return 'Barcode must be 4–64 letters, numbers, dash, dot, or underscore.';
        if (!payload.procurement_type || !['purchase', 'consignment'].includes(payload.procurement_type)) {
            return 'Choose Purchased or Consignment. It is not set automatically.';
        }
        const dup = masterDrugs.some(d =>
            String(d.generic_name || '').toLowerCase() === payload.generic_name.toLowerCase()
            && String(d.dosage || '').toLowerCase() === payload.dosage.toLowerCase()
            && String(d.form || '').toLowerCase() === payload.form.toLowerCase()
            && String(d.drug_id) !== String(payload.drug_id || '')
        );
        if (dup) return 'This exact drug (generic name, dosage, form) already exists.';
        return null;
    }

    function fillCategorySelect(sel, selected) {
        if (!sel) return;
        const cats = allCategories();
        sel.innerHTML = '<option value="">Select Category</option>' +
            cats.map(c => `<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`).join('') +
            '<option value="__new__">+ Add new category</option>';
        if (selected && selected !== '__new__') {
            if (![...sel.options].some(o => o.value === selected)) {
                const opt = document.createElement('option');
                opt.value = selected;
                opt.textContent = selected;
                sel.insertBefore(opt, sel.querySelector('option[value="__new__"]'));
            }
            sel.value = selected;
        }
    }

    function refreshCategorySelects(selected) {
        fillCategorySelect(document.getElementById('new_category'));
        fillCategorySelect(document.getElementById('edit_category'), selected);
    }

    function wireModalOpenClose() {
        document.getElementById('addDrugMasterBtn')?.addEventListener('click', () => {
            refreshCategorySelects();
            refreshGenericDatalist();
            refreshBrandDatalist('');
            fillSelectOptions(document.getElementById('new_form'), DRUG_FORMS, 'Tablet', 'Other…');
            fillSelectOptions(document.getElementById('new_dosage_unit'), DOSAGE_UNITS, 'mg');
            const minEl = document.getElementById('new_minimum_stock');
            if (minEl) minEl.value = '20';
            const procEl = document.getElementById('new_procurement_type');
            if (procEl) procEl.value = 'purchase';
            const copyEl = document.getElementById('new_copy_from');
            if (copyEl) copyEl.value = '';
            const hint = document.getElementById('new_autofill_hint');
            if (hint) hint.textContent = 'Type a known generic, or pick a drug above, to auto-fill form, category, and min stock.';
            toggleCustomField('new_form', 'new_form_custom', '__other__');
            toggleCustomField('new_category', 'new_category_custom', '__new__');
            showModal('addDrugModal');
        });
        document.getElementById('openAdjustHistoryBtn')?.addEventListener('click', () => {
            fetchStockAdjustments();
            showModal('adjustHistoryModal');
        });
        document.getElementById('openReorderModalBtn')?.addEventListener('click', () => {
            if (typeof window.loadReorderSuggestions === 'function') window.loadReorderSuggestions();
            showModal('reorderModal');
        });

        document.getElementById('closeDrugModal')?.addEventListener('click', () => hideModal('addDrugModal'));
        document.getElementById('closeEditDrugMasterModal')?.addEventListener('click', () => hideModal('editDrugMasterModal'));
        document.getElementById('closeEditModal')?.addEventListener('click', () => hideModal('editLotModal'));
        document.getElementById('closeAdjustStockModal')?.addEventListener('click', () => hideModal('adjustStockModal'));
        document.getElementById('closeAdjustHistoryModal')?.addEventListener('click', () => hideModal('adjustHistoryModal'));
        document.getElementById('closeReorderModal')?.addEventListener('click', () => hideModal('reorderModal'));
        document.getElementById('runConsignmentCheckBtn')?.addEventListener('click', () => {
            fetch('/api/admin/lots/consignment-cross-check', { method: 'POST' })
                .then(res => res.json())
                .then(result => {
                    if (!result.success) {
                        alert(result.message || 'Cross-check failed.');
                        return;
                    }
                    fetchLots();
                    const n = Number(result.updated || 0);
                    if (!n) {
                        alert('No near-expiry lots were linked to a supplier with a consignment policy.');
                        return;
                    }
                    alert('Cross-check done. Returnable: ' + (result.returnable || 0) + '. Non-returnable: ' + (result.non_returnable || 0) + '.');
                })
                .catch(err => alert('Error: ' + err.message));
        });

        document.querySelectorAll('.inv-tab-btn[data-inv-panel]').forEach(btn => {
            btn.addEventListener('click', () => switchInvPanel(btn.getAttribute('data-inv-panel')));
        });

        window.addEventListener('click', e => {
            ['addDrugModal', 'editDrugMasterModal', 'editLotModal', 'adjustStockModal', 'adjustHistoryModal', 'reorderModal'].forEach(id => {
                const modal = document.getElementById(id);
                if (modal && e.target === modal) modal.style.display = 'none';
            });
        });
    }

    // ---------------------------------------------------------------
    // Supplier dropdowns (Edit lot form)
    // ---------------------------------------------------------------
    function populateSupplierSelects() {
        fetch('/api/admin/suppliers')
            .then(res => res.json())
            .then(data => {
                const allFetched = Array.isArray(data) ? data : [];
                suppliersById = {};
                allFetched.forEach(s => { suppliersById[s.supplier_id] = s.supplier_name; });
                renderInventoryTable(); // refresh supplier names now that we have them

                const suppliers = allFetched.filter(s => s.status === 'Active');
                const options = '<option value="">Select Supplier</option>' +
                    suppliers.map(s => `<option value="${s.supplier_id}">${escapeHtml(s.supplier_name)}</option>`).join('');

                const editSel = document.getElementById('edit_supplier') ||
                    document.querySelector('#editLotForm select[name="supplier"]');
                if (editSel) editSel.innerHTML = options;
            })
            .catch(err => console.error('Failed to load suppliers for dropdown:', err));
    }

    // ---------------------------------------------------------------
    // Drug Master (definitions)
    // ---------------------------------------------------------------
    function drugMasterStatusFilter() {
        return document.getElementById('drugMasterFilter')?.value || 'active';
    }

    function fetchMasterDrugs() {
        const params = new URLSearchParams({ status: drugMasterStatusFilter() });
        return fetch('/api/admin/drugs?' + params.toString())
            .then(res => res.json())
            .then(data => {
                masterDrugs = Array.isArray(data) ? data : [];
                refreshCategorySelects();
                refreshGenericDatalist();
                renderMasterTable();
            })
            .catch(err => {
                console.error('Failed to load drug master list:', err);
                const body = document.getElementById('drugMasterBody');
                if (body) body.innerHTML = '<tr><td colspan="8" class="inv-empty is-error">Failed to load drugs.</td></tr>';
            });
    }

    function renderMasterTable() {
        const body = document.getElementById('drugMasterBody');
        if (!body) return;

        const headers = Array.from(document.querySelectorAll('#drugMasterTable thead th')).map(th => th.textContent.trim());
        const hasIdCol = headers[0] === 'ID';
        const hasStatusCol = headers.includes('Status');

        const search = (document.getElementById('drugMasterSearch')?.value || '').trim().toLowerCase();
        const category = document.getElementById('categoryFilter')?.value;

        let rows = masterDrugs.filter(d => {
            if (search) {
                const hay = [d.generic_name, d.brand_name, d.dosage, d.form, d.category].join(' ').toLowerCase();
                if (!hay.includes(search)) return false;
            }
            if (category && category !== 'all' && d.category !== category) return false;
            return true;
        });

        const paged = slicePage(rows, drugPage);
        drugPage = paged.page;
        updateInvPager('drugs', paged.page, paged.pages, paged.total);

        if (!rows.length) {
            const colspan = headers.length || 8;
            body.innerHTML = `<tr><td colspan="${colspan}" class="inv-empty">No drug definitions found.</td></tr>`;
            return;
        }

        body.innerHTML = paged.slice.map(d => {
            const isActive = Number(d.is_active) === 1;
            const idCell = hasIdCol ? `<td>${d.drug_id}</td>` : '';
            const statusCell = hasStatusCol
                ? `<td><span class="inv-badge ${isActive ? 'active' : 'archived'}">${isActive ? 'Active' : 'Archived'}</span></td>`
                : '';
            return `
            <tr data-drug-id="${d.drug_id}">
                ${idCell}
                <td>${escapeHtml(d.generic_name)}</td>
                <td>${escapeHtml(d.brand_name) || '—'}</td>
                <td>${escapeHtml(d.dosage)}</td>
                <td>${escapeHtml(d.form)}</td>
                <td>${escapeHtml(d.category)}${String(d.procurement_type || '') === 'consignment' ? ' <span class="inv-chip">Consignment</span>' : ''}</td>
                <td>${d.minimum_stock}</td>
                <td class="inv-mono">${escapeHtml(d.barcode) || '—'}</td>
                ${statusCell}
                <td class="action-btn-group">
                    <button type="button" class="inv-icon-btn print-barcode-btn" title="Print Barcode Label"><i class="fas fa-barcode"></i></button>
                    <button type="button" class="inv-icon-btn edit-master-btn" title="Edit"><i class="fas fa-pen"></i></button>
                    <button type="button" class="inv-icon-btn toggle-master-btn ${isActive ? 'is-active' : 'is-inactive'}" title="${isActive ? 'Archive' : 'Reactivate'}"><i class="fas ${isActive ? 'fa-box-archive' : 'fa-rotate-left'}"></i></button>
                </td>
            </tr>`;
        }).join('');
    }

    function handleMasterTableClick(e) {
        const row = e.target.closest('tr[data-drug-id]');
        if (!row) return;
        const id = row.getAttribute('data-drug-id');
        const drug = masterDrugs.find(d => String(d.drug_id) === String(id));
        if (!drug) return;

        if (e.target.closest('.edit-master-btn')) {
            openEditDrugMasterModal(drug);
        } else if (e.target.closest('.print-barcode-btn')) {
            printBarcodeLabel(drug);
        } else if (e.target.closest('.toggle-master-btn')) {
            const isActive = Number(drug.is_active) === 1;
            const label = `${drug.generic_name}${drug.brand_name ? ' (' + drug.brand_name + ')' : ''}`;
            if (isActive) {
                window.phConfirm(`Archive "${label}"? It will be hidden from active lists.`).then(function (ok) {
                    if (!ok) return;
                    fetch('/api/admin/drugs/deactivate?id=' + encodeURIComponent(id))
                        .then(res => res.json())
                        .then(result => { if (result.success) fetchMasterDrugs(); else alert(result.message || 'Failed to archive drug.'); })
                        .catch(err => alert('Error: ' + err.message));
                });
            } else {
                window.phConfirm(`Reactivate "${label}"?`).then(function (ok) {
                    if (!ok) return;
                    fetch('/api/admin/drugs/reactivate?id=' + encodeURIComponent(id))
                        .then(res => res.json())
                        .then(result => { if (result.success) fetchMasterDrugs(); else alert(result.message || 'Failed to reactivate drug.'); })
                        .catch(err => alert('Error: ' + err.message));
                });
            }
        }
    }

    // ---------------------------------------------------------------
    // Barcode label printing — opens a small popup with a scannable
    // Code128 barcode (via the JsBarcode CDN library) plus the drug name,
    // sized for a standard shelf/sticker label, and triggers the browser
    // print dialog automatically.
    // ---------------------------------------------------------------
    function loadJsBarcode(done) {
        if (typeof JsBarcode === 'function') { done(); return; }
        const existing = document.querySelector('script[data-jsbarcode]');
        if (existing) {
            existing.addEventListener('load', done, { once: true });
            return;
        }
        const s = document.createElement('script');
        s.src = 'https://cdn.jsdelivr.net/npm/jsbarcode@3.11.6/dist/JsBarcode.all.min.js';
        s.setAttribute('data-jsbarcode', '1');
        s.onload = done;
        s.onerror = () => alert('Could not load the barcode library. Check your internet connection.');
        document.head.appendChild(s);
    }

    function barcodePngDataUrl(code) {
        const canvas = document.createElement('canvas');
        JsBarcode(canvas, String(code), {
            format: 'CODE128',
            width: 2,
            height: 72,
            displayValue: true,
            fontSize: 16,
            margin: 8,
            background: '#ffffff',
            lineColor: '#000000'
        });
        return canvas.toDataURL('image/png');
    }

    function printBarcodeLabel(drug) {
        if (!drug.barcode) {
            alert('This drug has no barcode assigned yet. Edit it first to set one.');
            return;
        }
        loadJsBarcode(() => {
            let png;
            try {
                png = barcodePngDataUrl(drug.barcode);
            } catch (err) {
                alert('Could not draw barcode "' + drug.barcode + '". Try a simpler code (letters and numbers only).');
                return;
            }
            const label = `${escapeHtml(drug.generic_name)}${drug.brand_name ? ' (' + escapeHtml(drug.brand_name) + ')' : ''} — ${escapeHtml(drug.dosage)}`;
            const iframe = document.createElement('iframe');
            iframe.setAttribute('aria-hidden', 'true');
            iframe.style.cssText = 'position:fixed;right:0;bottom:0;width:0;height:0;border:0;';
            document.body.appendChild(iframe);
            const doc = iframe.contentWindow.document;
            doc.open();
            doc.write(`<!DOCTYPE html><html><head><title>Barcode — ${label}</title>
                <style>
                    body { font-family: Arial, sans-serif; text-align: center; padding: 24px; color: #111; }
                    h3 { margin: 0 0 12px; font-size: 16px; }
                    img { max-width: 100%; height: auto; }
                    .code { font-family: ui-monospace, Consolas, monospace; font-size: 14px; letter-spacing: .08em; margin-top: 8px; }
                </style></head><body>
                <h3>${label}</h3>
                <img src="${png}" alt="Barcode ${escapeHtml(drug.barcode)}">
                <div class="code">${escapeHtml(drug.barcode)}</div>
                </body></html>`);
            doc.close();
            setTimeout(() => {
                iframe.contentWindow.focus();
                iframe.contentWindow.print();
                setTimeout(() => iframe.remove(), 2500);
            }, 400);
        });
    }

    let drugBarcodeReader = null;
    let drugBarcodeTargetId = 'new_barcode';
    let drugBarcodeScanCooldown = false;
    let drugBarcodeOnScan = null;

    function preventBarcodeEnterSubmit(input) {
        input?.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                input.value = String(input.value || '').trim();
            }
        });
    }

    function applyScannedBarcode(code) {
        const text = String(code || '').trim();
        if (!text) return false;
        if (typeof drugBarcodeOnScan === 'function') {
            const ok = drugBarcodeOnScan(text);
            if (ok === false) return false;
            return true;
        }
        const el = document.getElementById(drugBarcodeTargetId);
        if (!el) return false;
        el.value = text;
        el.focus();
        return true;
    }

    function closeDrugBarcodeScanner(opts) {
        const pending = document.getElementById('drugBarcodeManualInput')?.value.trim();
        const cb = drugBarcodeOnScan;
        if (drugBarcodeReader) {
            try { drugBarcodeReader.reset(); } catch (e) { /* ignore */ }
            drugBarcodeReader = null;
        }
        const modal = document.getElementById('drugBarcodeScannerModal');
        if (modal) modal.style.display = 'none';
        drugBarcodeOnScan = null;
        if (opts && opts.applyPending && pending && typeof cb === 'function') {
            cb(pending);
        }
    }

    function openDrugBarcodeScanner(targetId, onScan) {
        drugBarcodeTargetId = targetId || 'new_barcode';
        drugBarcodeOnScan = typeof onScan === 'function' ? onScan : null;
        const modal = document.getElementById('drugBarcodeScannerModal');
        const status = document.getElementById('drugBarcodeScannerStatus');
        const manual = document.getElementById('drugBarcodeManualInput');
        if (manual) manual.value = '';
        if (modal) modal.style.display = 'flex';
        if (status) status.textContent = 'Point the camera at the barcode, or type it below.';
        if (typeof ZXing === 'undefined') {
            if (status) status.textContent = 'Camera library did not load. Type the barcode below, or use a USB scanner on the form field.';
            manual?.focus();
            return;
        }
        try {
            drugBarcodeReader = new ZXing.BrowserMultiFormatReader();
            drugBarcodeReader.decodeFromVideoDevice(null, 'drugBarcodeScannerVideo', (result) => {
                if (result && !drugBarcodeScanCooldown) {
                    drugBarcodeScanCooldown = true;
                    const done = applyScannedBarcode(result.getText());
                    if (done !== false) closeDrugBarcodeScanner();
                    setTimeout(() => { drugBarcodeScanCooldown = false; }, 1500);
                }
            }).catch(() => {
                if (status) status.textContent = 'Camera unavailable. Type the barcode below, or use a USB scanner on the form field.';
                manual?.focus();
            });
        } catch (err) {
            if (status) status.textContent = 'Camera unavailable. Type the barcode below, or use a USB scanner on the form field.';
            manual?.focus();
        }
    }

    function wireBarcodeInputs() {
        preventBarcodeEnterSubmit(document.getElementById('new_barcode'));
        preventBarcodeEnterSubmit(document.getElementById('edit_barcode'));
        document.getElementById('scanNewBarcodeBtn')?.addEventListener('click', () => openDrugBarcodeScanner('new_barcode'));
        document.getElementById('scanEditBarcodeBtn')?.addEventListener('click', () => openDrugBarcodeScanner('edit_barcode'));
        document.getElementById('closeDrugBarcodeScanner')?.addEventListener('click', () => closeDrugBarcodeScanner({ applyPending: true }));
        document.getElementById('drugBarcodeScannerModal')?.addEventListener('click', (e) => {
            if (e.target.id === 'drugBarcodeScannerModal') closeDrugBarcodeScanner({ applyPending: true });
        });
        function useManualBarcode() {
            const v = document.getElementById('drugBarcodeManualInput')?.value.trim();
            if (!v) return;
            const done = applyScannedBarcode(v);
            if (done !== false) closeDrugBarcodeScanner();
        }
        document.getElementById('useDrugBarcodeBtn')?.addEventListener('click', useManualBarcode);
        document.getElementById('drugBarcodeManualInput')?.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                useManualBarcode();
            }
        });
    }

    function openEditDrugMasterModal(drug) {
        const nameSpan = document.getElementById('currentMasterDrugName');
        if (nameSpan) nameSpan.textContent = drug.generic_name;
        document.getElementById('edit_master_drug_id').value = drug.drug_id;
        document.getElementById('edit_generic_name').value = drug.generic_name || '';
        document.getElementById('edit_brand_name').value = drug.brand_name || '';
        const split = splitDosage(drug.dosage);
        fillSelectOptions(document.getElementById('edit_dosage_unit'), DOSAGE_UNITS, split.unit, null);
        const unitSel = document.getElementById('edit_dosage_unit');
        if (unitSel && split.unit && ![...unitSel.options].some(o => o.value === split.unit)) {
            const opt = document.createElement('option');
            opt.value = split.unit;
            opt.textContent = split.unit;
            unitSel.appendChild(opt);
            unitSel.value = split.unit;
        }
        document.getElementById('edit_dosage_value').value = split.unit === 'N/A' ? '' : split.value;
        document.getElementById('edit_dosage_value').required = split.unit !== 'N/A';
        fillSelectOptions(document.getElementById('edit_form'), DRUG_FORMS, drug.form, 'Other…');
        if (drug.form && !DRUG_FORMS.includes(drug.form)) {
            document.getElementById('edit_form').value = '__other__';
            const fc = document.getElementById('edit_form_custom');
            if (fc) fc.value = drug.form;
        }
        toggleCustomField('edit_form', 'edit_form_custom', '__other__');
        fillCategorySelect(document.getElementById('edit_category'), drug.category || '');
        toggleCustomField('edit_category', 'edit_category_custom', '__new__');
        document.getElementById('edit_minimum_stock').value = drug.minimum_stock;
        const procEl = document.getElementById('edit_procurement_type');
        if (procEl) procEl.value = drug.procurement_type === 'consignment' ? 'consignment' : 'purchase';
        const editBarcodeEl = document.getElementById('edit_barcode');
        if (editBarcodeEl) editBarcodeEl.value = drug.barcode || '';
        showModal('editDrugMasterModal');
    }

    function handleAddDrugSubmit(e) {
        e.preventDefault();
        const payload = {
            generic_name: document.getElementById('new_generic_name').value.trim(),
            brand_name: document.getElementById('new_brand_name').value.trim(),
            dosage: composeDosage('new'),
            form: pickedForm('new'),
            category: pickedCategory('new'),
            minimum_stock: parseInt(document.getElementById('new_minimum_stock').value, 10),
            procurement_type: document.getElementById('new_procurement_type')?.value || '',
            barcode: (document.getElementById('new_barcode')?.value || '').trim(),
        };
        const err = clientDrugError(payload);
        if (err) { alert(err); return; }
        fetch('/api/admin/drugs', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
        })
            .then(res => res.json())
            .then(result => {
                if (result.success) {
                    hideModal('addDrugModal');
                    e.target.reset();
                    fetchMasterDrugs();
                    if (result.barcode_warning) alert(result.barcode_warning);
                    else if (result.barcode && !payload.barcode) {
                        alert('Drug created. Auto barcode: ' + result.barcode);
                    }
                } else {
                    alert(result.message || 'Failed to create drug definition.');
                }
            })
            .catch(err => alert('Error: ' + err.message));
    }

    function handleEditDrugSubmit(e) {
        e.preventDefault();
        const payload = {
            drug_id: document.getElementById('edit_master_drug_id').value,
            generic_name: document.getElementById('edit_generic_name').value.trim(),
            brand_name: document.getElementById('edit_brand_name').value.trim(),
            dosage: composeDosage('edit'),
            form: pickedForm('edit'),
            category: pickedCategory('edit'),
            minimum_stock: parseInt(document.getElementById('edit_minimum_stock').value, 10),
            procurement_type: document.getElementById('edit_procurement_type')?.value || '',
            barcode: (document.getElementById('edit_barcode')?.value || '').trim(),
        };
        const err = clientDrugError(payload);
        if (err) { alert(err); return; }
        fetch('/api/admin/drugs/update', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
        })
            .then(res => res.json())
            .then(result => {
                if (result.success) {
                    hideModal('editDrugMasterModal');
                    fetchMasterDrugs();
                } else {
                    alert(result.message || 'Failed to update drug definition.');
                }
            })
            .catch(err => alert('Error: ' + err.message));
    }

    // ---------------------------------------------------------------
    // Stock Lots (inventory)
    // ---------------------------------------------------------------
    function lotStatusFilter() {
        return document.getElementById('lotStatusFilter')?.value || 'active';
    }

    function fetchLots() {
        const status = lotStatusFilter();
        const excludeExpired = status === 'active' ? '1' : '0';
        const params = new URLSearchParams({ status, exclude_expired: excludeExpired });
        return fetch('/api/admin/lots?' + params.toString())
            .then(res => res.json())
            .then(data => {
                lots = Array.isArray(data) ? data : [];
                renderSummaryCards();
                renderInventoryTable();
            })
            .catch(err => {
                console.error('Failed to load inventory lots:', err);
                const body = document.getElementById('inventoryBody');
                if (body) body.innerHTML = '<tr><td colspan="14" class="inv-empty is-error">Failed to load inventory.</td></tr>';
            });
    }

    function renderSummaryCards() {
        const activeLots = lots.filter(l => Number(l.is_active) === 1 && daysUntil(l.expiration_date) >= 0);
        const total = activeLots.length;
        const low = activeLots.filter(l => l.current_stock > 0 && l.current_stock <= l.minimum_stock).length;
        const expiring = activeLots.filter(l => l.current_stock > 0 && daysUntil(l.expiration_date) <= 90).length;
        const expiring30 = activeLots.filter(l => l.current_stock > 0 && daysUntil(l.expiration_date) <= 30).length;

        const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
        set('total-items', total);
        set('low-stock', low);
        set('expiring-soon', expiring);
        set('expiring-soon-30', expiring30);
    }

    function renderInventoryTable() {
        const body = document.getElementById('inventoryBody');
        if (!body) return;

        const search = (document.getElementById('inventorySearch')?.value || '').trim().toLowerCase();
        const category = document.getElementById('categoryFilter')?.value;

        let rows = lots.filter(l => {
            if (search) {
                const hay = [l.generic_name, l.brand_name, l.dosage, l.form, l.category, l.lot_number].join(' ').toLowerCase();
                if (!hay.includes(search)) return false;
            }
            if (category && category !== 'all' && l.category !== category) return false;

            if (activeCardFilter === 'low') return l.current_stock > 0 && l.current_stock <= l.minimum_stock;
            if (activeCardFilter === 'out') return l.current_stock === 0;
            if (activeCardFilter === 'expiring') return l.current_stock > 0 && daysUntil(l.expiration_date) <= 90 && daysUntil(l.expiration_date) >= 0;
            if (activeCardFilter === 'expiring30') return l.current_stock > 0 && daysUntil(l.expiration_date) <= 30 && daysUntil(l.expiration_date) >= 0;
            return true; // 'all'
        });

        const paged = slicePage(rows, lotPage);
        lotPage = paged.page;
        updateInvPager('lots', paged.page, paged.pages, paged.total);

        if (!rows.length) {
            body.innerHTML = '<tr><td colspan="14" class="inv-empty">No stock lots found.</td></tr>';
            return;
        }

        body.innerHTML = paged.slice.map(l => {
            const isActive = Number(l.is_active) === 1;
            const daysLeft = daysUntil(l.expiration_date);
            const expired = daysLeft < 0;
            const expiringCritical = !expired && daysLeft <= 30;
            const expiringSoon = !expired && !expiringCritical && daysLeft <= 90;
            const expClass = expired ? 'is-expired' : (expiringCritical ? 'is-critical' : (expiringSoon ? 'is-soon' : ''));
            const stockClass = l.current_stock === 0 ? 'is-out' : (l.current_stock <= l.minimum_stock ? 'is-low' : 'is-ok');
            const expTag = expired ? ' (expired)' : (expiringCritical ? ` (${daysLeft}d left ⚠️)` : (expiringSoon ? ` (${daysLeft}d left)` : ''));
            const policy = String(l.supplier_consignment_policy || '').toLowerCase();
            const hasPolicy = policy === 'returnable' || policy === 'non_returnable';
            const status = String(l.return_status || '').toLowerCase();
            const nearExpiry = !expired && daysLeft <= 90 && l.current_stock > 0;
            let returnCell = '—';
            if (status === 'returnable') {
                returnCell = '<span class="inv-chip return-yes">Returnable</span>';
            } else if (status === 'non_returnable') {
                returnCell = '<span class="inv-chip return-no">Non-returnable</span>';
            } else if (nearExpiry && hasPolicy) {
                returnCell = '<span class="inv-chip return-flag">Flagged — run check</span>';
            }
            return `
            <tr data-lot-id="${l.lot_inventory_id}">
                <td>${escapeHtml(l.generic_name)}</td>
                <td>${escapeHtml(l.brand_name) || '—'}</td>
                <td>${escapeHtml(l.dosage)}</td>
                <td>${escapeHtml(l.form)}</td>
                <td>${escapeHtml(l.category)}</td>
                <td>${escapeHtml(l.lot_number)}</td>
                <td class="inv-exp ${expClass}">${l.expiration_date}${expTag}</td>
                <td class="inv-stock ${stockClass}">${l.current_stock}</td>
                <td>${l.minimum_stock}</td>
                <td>${money(l.price)}</td>
                <td>${l.supplier && suppliersById[l.supplier] ? escapeHtml(suppliersById[l.supplier]) : '—'}</td>
                <td>${returnCell}</td>
                <td><span class="inv-badge ${isActive ? 'active' : 'archived'}">${isActive ? 'Active' : 'Archived'}</span></td>
                <td class="action-btn-group">
                    <button type="button" class="inv-icon-btn adjust-stock-btn" title="Adjust Stock (with reason)"><i class="fas fa-sliders"></i></button>
                    <button type="button" class="inv-icon-btn edit-lot-btn" title="Edit"><i class="fas fa-pen"></i></button>
                    <button type="button" class="inv-icon-btn toggle-lot-btn ${isActive ? 'is-active' : 'is-inactive'}" title="${isActive ? 'Archive' : 'Reactivate'}"><i class="fas ${isActive ? 'fa-box-archive' : 'fa-rotate-left'}"></i></button>
                </td>
            </tr>`;
        }).join('');
    }

    function handleInventoryTableClick(e) {
        const row = e.target.closest('tr[data-lot-id]');
        if (!row) return;
        const id = row.getAttribute('data-lot-id');
        const lot = lots.find(l => String(l.lot_inventory_id) === String(id));
        if (!lot) return;

        if (e.target.closest('.edit-lot-btn')) {
            openEditLotModal(lot);
        } else if (e.target.closest('.adjust-stock-btn')) {
            openAdjustStockModal(lot);
        } else if (e.target.closest('.toggle-lot-btn')) {
            const isActive = Number(lot.is_active) === 1;
            if (isActive) {
                window.phConfirm(`Archive lot "${lot.lot_number}"?`).then(function (ok) {
                    if (!ok) return;
                    fetch('/api/admin/lots/deactivate?id=' + encodeURIComponent(id))
                        .then(res => res.json())
                        .then(result => { if (result.success) fetchLots(); else alert(result.message || 'Failed to archive lot.'); })
                        .catch(err => alert('Error: ' + err.message));
                });
            } else {
                window.phConfirm(`Reactivate lot "${lot.lot_number}"?`).then(function (ok) {
                    if (!ok) return;
                    fetch('/api/admin/lots/reactivate?id=' + encodeURIComponent(id))
                        .then(res => res.json())
                        .then(result => { if (result.success) fetchLots(); else alert(result.message || 'Failed to reactivate lot.'); })
                        .catch(err => alert('Error: ' + err.message));
                });
            }
        }
    }

    // ---------------------------------------------------------------
    // Stock Adjustment (audit-trailed manual stock corrections)
    // ---------------------------------------------------------------
    let adjustStockLot = null;

    function openAdjustStockModal(lot) {
        adjustStockLot = lot;
        const label = document.getElementById('adjust_stock_drug_label');
        if (label) label.textContent = `${lot.generic_name}${lot.brand_name ? ' (' + lot.brand_name + ')' : ''} — ${lot.dosage}, ${lot.form} — Lot ${lot.lot_number}`;
        const currentEl = document.getElementById('adjust_stock_current');
        if (currentEl) currentEl.textContent = lot.current_stock;
        const form = document.getElementById('adjustStockForm');
        if (form) form.reset();
        const preview = document.getElementById('adjust_stock_preview');
        if (preview) preview.textContent = '';
        showModal('adjustStockModal');
    }

    function updateAdjustStockPreview() {
        if (!adjustStockLot) return;
        const type = document.getElementById('adjust_type')?.value;
        const qty = parseInt(document.getElementById('adjust_quantity')?.value, 10);
        const preview = document.getElementById('adjust_stock_preview');
        if (!preview || isNaN(qty) || qty < 0) { if (preview) preview.textContent = ''; return; }
        const current = adjustStockLot.current_stock;
        let result;
        if (type === 'add') result = current + qty;
        else if (type === 'remove') result = current - qty;
        else result = qty; // 'set'
        preview.textContent = result < 0
            ? `⚠️ Would go below zero (${current} → ${result})`
            : `New stock will be: ${current} → ${result}`;
        preview.style.color = result < 0 ? '#dc2626' : '#16a34a';
    }

    function handleAdjustStockSubmit(e) {
        e.preventDefault();
        if (!adjustStockLot) return;
        const reasonSel = document.getElementById('adjust_reason')?.value;
        const notes = document.getElementById('adjust_notes')?.value.trim() || '';
        if (reasonSel === 'Other' && !notes) {
            alert('Please add a note describing the reason when selecting "Other".');
            return;
        }
        const payload = {
            lot_inventory_id: adjustStockLot.lot_inventory_id,
            adjustment_type: document.getElementById('adjust_type')?.value,
            quantity: parseInt(document.getElementById('adjust_quantity')?.value, 10),
            reason: reasonSel,
            notes: notes,
        };
        fetch('/api/admin/lots/adjust', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
        })
            .then(res => res.json())
            .then(result => {
                if (result.success) {
                    hideModal('adjustStockModal');
                    adjustStockLot = null;
                    fetchLots();
                    fetchStockAdjustments();
                } else {
                    alert(result.message || 'Failed to adjust stock.');
                }
            })
            .catch(err => alert('Error: ' + err.message));
    }

    function fetchStockAdjustments() {
        const body = document.getElementById('stockAdjustmentsBody');
        if (!body) return; // widget not present on this page — skip quietly
        fetch('/api/admin/stock-adjustments?limit=50')
            .then(res => res.json())
            .then(data => {
                const rows = Array.isArray(data) ? data : [];
                if (!rows.length) {
                    body.innerHTML = '<tr><td colspan="7" style="text-align:center;color:#6b7280;padding:15px;">No stock adjustments recorded yet.</td></tr>';
                    return;
                }
                body.innerHTML = rows.map(r => {
                    const sign = r.quantity_change > 0 ? '+' : '';
                    const changeColor = r.quantity_change > 0 ? '#16a34a' : '#dc2626';
                    const when = new Date(r.created_at).toLocaleString('en-PH', { dateStyle: 'medium', timeStyle: 'short' });
                    const drugLabel = `${escapeHtml(r.generic_name)}${r.brand_name ? ' (' + escapeHtml(r.brand_name) + ')' : ''}`;
                    return `
                    <tr>
                        <td>${when}</td>
                        <td>${drugLabel}</td>
                        <td>${escapeHtml(r.lot_number) || '—'}</td>
                        <td style="color:${changeColor};font-weight:600;">${sign}${r.quantity_change} (${r.previous_stock} → ${r.new_stock})</td>
                        <td>${escapeHtml(r.reason)}</td>
                        <td>${escapeHtml(r.notes) || '—'}</td>
                        <td>${escapeHtml(r.admin_name) || '—'}</td>
                    </tr>`;
                }).join('');
            })
            .catch(err => console.error('Failed to load stock adjustments:', err));
    }

    let currentEditLotCategory = null;

    // ---------------------------------------------------------------
    // Price Computation: category markup settings + cost→price suggestion
    // ---------------------------------------------------------------
    let categoryMarkups = {};

    function fetchCategoryMarkups() {
        return fetch('/api/admin/category-markups')
            .then(res => res.json())
            .then(rows => {
                categoryMarkups = {};
                (rows || []).forEach(r => { categoryMarkups[r.category] = r.markup_percent; });
                renderMarkupSettingsTable(rows || []);
                refreshCategorySelects();
                return rows;
            })
            .catch(err => console.error('Failed to load category markups:', err));
    }

    function renderMarkupSettingsTable(rows) {
        const body = document.getElementById('markupSettingsBody');
        if (!body) return;
        if (!rows.length) {
            body.innerHTML = '<tr><td colspan="3" style="text-align:center;color:#6b7280;padding:15px;">No categories yet — add a drug first.</td></tr>';
            return;
        }
        body.innerHTML = rows.map(r => `
            <tr data-category="${escapeHtml(r.category)}">
                <td>${escapeHtml(r.category)}</td>
                <td><input type="number" step="0.1" min="0" class="markup-input" value="${r.markup_percent}" style="width:90px;padding:5px 8px;border:1px solid #ccc;border-radius:4px;"> %</td>
                <td><button type="button" class="save-markup-btn" style="background:#16a34a;color:#fff;border:none;border-radius:4px;padding:5px 12px;cursor:pointer;">Save</button></td>
            </tr>`).join('');
    }

    document.getElementById('markupSettingsBody')?.addEventListener('click', e => {
        const btn = e.target.closest('.save-markup-btn');
        if (!btn) return;
        const row = btn.closest('tr');
        const category = row.getAttribute('data-category');
        const input = row.querySelector('.markup-input');
        const markup_percent = parseFloat(input.value);
        if (isNaN(markup_percent) || markup_percent < 0) { alert('Please enter a valid, non-negative markup %.'); return; }
        fetch('/api/admin/category-markups', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ category, markup_percent })
        })
            .then(res => res.json())
            .then(result => {
                if (result.success) {
                    categoryMarkups[category] = markup_percent;
                    if (typeof fetchLots === 'function') fetchLots();
                    const n = Number(result.updated_lots || 0);
                    const skip = Number(result.skipped_no_cost || 0);
                    let msg = 'Saved ✓';
                    if (n || skip) {
                        msg = 'Saved · ' + n + ' lot' + (n === 1 ? '' : 's') + ' repriced';
                        if (skip) msg += ', ' + skip + ' no cost';
                    }
                    btn.textContent = msg;
                    setTimeout(() => { btn.textContent = 'Save'; }, 2500);
                } else {
                    alert(result.message || 'Failed to save markup.');
                }
            })
            .catch(err => alert('Error: ' + err.message));
    });

    function computeSuggestedPrice(category, cost) {
        const cat = String(category || '').trim().toLowerCase();
        let markup = 30;
        if (cat && categoryMarkups[category] !== undefined) {
            markup = categoryMarkups[category];
        } else if (cat) {
            const key = Object.keys(categoryMarkups).find(k => String(k).trim().toLowerCase() === cat);
            if (key !== undefined) markup = categoryMarkups[key];
        }
        return { suggested: (cost * (1 + markup / 100)).toFixed(2), markup };
    }

    function updateEditLotPriceSuggestion() {
        const hint = document.getElementById('edit_price_suggestion_hint');
        const priceEl = document.getElementById('edit_price');
        const cost = parseFloat(document.getElementById('edit_cost_price')?.value);
        if (isNaN(cost) || cost <= 0) {
            if (hint) hint.innerHTML = '';
            if (priceEl) priceEl.value = '';
            return;
        }
        const { suggested, markup } = computeSuggestedPrice(currentEditLotCategory, cost);
        if (priceEl) priceEl.value = suggested;
        if (hint) {
            hint.innerHTML = `₱${suggested} from ${markup}% markup${currentEditLotCategory ? ' for ' + escapeHtml(currentEditLotCategory) : ''}.`;
        }
    }
    document.getElementById('edit_cost_price')?.addEventListener('input', updateEditLotPriceSuggestion);

    function openEditLotModal(lot) {
        const nameSpan = document.getElementById('editDrugName');
        if (nameSpan) nameSpan.textContent = lot.generic_name;
        const label = document.getElementById('edit_drug_label');
        if (label) label.textContent = `${lot.generic_name}${lot.brand_name ? ' (' + lot.brand_name + ')' : ''} — ${lot.dosage}, ${lot.form}`;

        document.getElementById('edit_lot_inventory_id').value = lot.lot_inventory_id;
        document.getElementById('edit_lot_number').value = lot.lot_number || '';
        document.getElementById('edit_expiration_date').value = lot.expiration_date || '';
        lockDateNoPast(document.getElementById('edit_expiration_date'));
        document.getElementById('edit_current_stock').value = lot.current_stock;
        const editCostEl = document.getElementById('edit_cost_price');
        if (editCostEl) editCostEl.value = (lot.cost_price !== null && lot.cost_price !== undefined) ? lot.cost_price : '';
        currentEditLotCategory = lot.category || null;
        updateEditLotPriceSuggestion();

        const editSel = document.getElementById('edit_supplier') || document.querySelector('#editLotForm select[name="supplier"]');
        if (editSel) editSel.value = lot.supplier || '';

        showModal('editLotModal');
    }

    function handleEditLotSubmit(e) {
        e.preventDefault();
        const exp = document.getElementById('edit_expiration_date')?.value || '';
        if (exp && exp < manilaTodayYmd()) {
            alert('Expiration date cannot be in the past.');
            return;
        }
        const editSel = document.getElementById('edit_supplier') || document.querySelector('#editLotForm select[name="supplier"]');
        const payload = {
            lot_inventory_id: document.getElementById('edit_lot_inventory_id').value,
            lot_number: document.getElementById('edit_lot_number').value.trim(),
            expiration_date: document.getElementById('edit_expiration_date').value,
            current_stock: parseInt(document.getElementById('edit_current_stock').value, 10) || 0,
            price: parseFloat(document.getElementById('edit_price').value) || 0,
            cost_price: document.getElementById('edit_cost_price')?.value || '',
            supplier: editSel ? editSel.value : '',
        };
        fetch('/api/admin/lots/update', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
        })
            .then(res => res.json())
            .then(result => {
                if (result.success) {
                    hideModal('editLotModal');
                    fetchLots();
                } else {
                    alert(result.message || 'Failed to update stock lot.');
                }
            })
            .catch(err => alert('Error: ' + err.message));
    }

    // ---------------------------------------------------------------
    // Init
    // ---------------------------------------------------------------
    function init() {
        wireModalOpenClose();
        populateSupplierSelects();

        document.getElementById('drugMasterBody')?.addEventListener('click', handleMasterTableClick);
        document.getElementById('inventoryBody')?.addEventListener('click', handleInventoryTableClick);

        document.getElementById('addDrugForm')?.addEventListener('submit', handleAddDrugSubmit);
        document.getElementById('editDrugMasterForm')?.addEventListener('submit', handleEditDrugSubmit);
        document.getElementById('new_form')?.addEventListener('change', () => toggleCustomField('new_form', 'new_form_custom', '__other__'));
        document.getElementById('edit_form')?.addEventListener('change', () => toggleCustomField('edit_form', 'edit_form_custom', '__other__'));
        document.getElementById('new_category')?.addEventListener('change', () => toggleCustomField('new_category', 'new_category_custom', '__new__'));
        document.getElementById('edit_category')?.addEventListener('change', () => toggleCustomField('edit_category', 'edit_category_custom', '__new__'));
        document.getElementById('new_generic_name')?.addEventListener('change', (e) => suggestFromGeneric(e.target.value, 'new'));
        document.getElementById('new_generic_name')?.addEventListener('blur', (e) => suggestFromGeneric(e.target.value, 'new'));
        let genericSuggestTimer = null;
        document.getElementById('new_generic_name')?.addEventListener('input', (e) => {
            clearTimeout(genericSuggestTimer);
            genericSuggestTimer = setTimeout(() => suggestFromGeneric(e.target.value, 'new'), 200);
        });
        document.getElementById('new_copy_from')?.addEventListener('change', (e) => {
            const drug = masterDrugs.find(d => String(d.drug_id) === String(e.target.value));
            if (!drug) return;
            fillNewDrugFromRecord(drug, true);
            const barcode = document.getElementById('new_barcode');
            if (barcode) barcode.value = '';
        });
        const syncDoseReq = (prefix) => {
            const unit = document.getElementById(prefix + '_dosage_unit')?.value;
            const val = document.getElementById(prefix + '_dosage_value');
            if (val) val.required = unit !== 'N/A';
        };
        document.getElementById('new_dosage_unit')?.addEventListener('change', () => syncDoseReq('new'));
        document.getElementById('edit_dosage_unit')?.addEventListener('change', () => syncDoseReq('edit'));
        wireBarcodeInputs();
        document.getElementById('editLotForm')?.addEventListener('submit', handleEditLotSubmit);
        document.getElementById('adjustStockForm')?.addEventListener('submit', handleAdjustStockSubmit);
        document.getElementById('adjust_type')?.addEventListener('change', updateAdjustStockPreview);
        document.getElementById('adjust_quantity')?.addEventListener('input', updateAdjustStockPreview);

        document.getElementById('drugMasterSearch')?.addEventListener('input', () => { drugPage = 1; renderMasterTable(); });
        document.getElementById('drugMasterFilter')?.addEventListener('change', () => { drugPage = 1; fetchMasterDrugs(); });
        document.getElementById('inventorySearch')?.addEventListener('input', () => { lotPage = 1; renderInventoryTable(); });
        document.getElementById('lotStatusFilter')?.addEventListener('change', () => { lotPage = 1; fetchLots(); });
        document.getElementById('categoryFilter')?.addEventListener('change', () => {
            drugPage = 1;
            lotPage = 1;
            renderMasterTable();
            renderInventoryTable();
        });
        document.getElementById('lots-prev-btn')?.addEventListener('click', () => {
            if (lotPage > 1) { lotPage -= 1; renderInventoryTable(); }
        });
        document.getElementById('lots-next-btn')?.addEventListener('click', () => {
            lotPage += 1;
            renderInventoryTable();
        });
        document.getElementById('drugs-prev-btn')?.addEventListener('click', () => {
            if (drugPage > 1) { drugPage -= 1; renderMasterTable(); }
        });
        document.getElementById('drugs-next-btn')?.addEventListener('click', () => {
            drugPage += 1;
            renderMasterTable();
        });

        document.querySelectorAll('.inv-summary-card[data-filter]').forEach(card => {
            card.addEventListener('click', () => {
                switchInvPanel('lots');
                activeCardFilter = card.getAttribute('data-filter') || 'all';
                lotPage = 1;
                document.querySelectorAll('.inv-summary-card[data-filter]').forEach(c => {
                    c.classList.toggle('is-selected', c === card);
                });
                renderInventoryTable();
            });
        });

        fetchMasterDrugs();
        fetchLots();
        fetchStockAdjustments();
        fetchCategoryMarkups();
    }

    window.computeSuggestedPrice = computeSuggestedPrice;
    window.fetchCategoryMarkups = fetchCategoryMarkups;
    window.openDrugBarcodeScanner = openDrugBarcodeScanner;

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    window.initializeInventoryModule = init;

    // Lets other parts of the app (e.g. Dashboard cards) deep-link into a
    // specific Inventory filter — e.g. clicking "Expiring Within 30 Days" on
    // the Dashboard jumps to Inventory already filtered to expiring30.
    window.applyInventoryCardFilter = function (filter) {
        switchInvPanel('lots');
        const card = document.querySelector('.inv-summary-card[data-filter="' + filter + '"]');
        if (card) {
            card.click();
        } else {
            activeCardFilter = filter || 'all';
            renderInventoryTable();
        }
    };
})();