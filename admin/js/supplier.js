/**
 * supplier.js - Supplier Management module (admin/admin.php, #supplier-section)
 *
 * Talks to: get_suppliers.php, add_supplier.php, update_supplier.php
 *
 * Exposes window.initializeSupplierModule(), which admin.php calls the
 * first time (and every time) the "Supplier" nav item is clicked.
 */
(function () {
    let eventsBound = false;
    let allSuppliers = [];

    function escapeHtml(str) {
        if (str === null || str === undefined) return '';
        return String(str).replace(/[&<>"']/g, c => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
        }[c]));
    }

    function statusBadge(status) {
        const isActive = (status || '').toLowerCase() === 'active';
        const cls = isActive ? 'active' : 'inactive';
        return `<span class="sup-badge ${cls}">${escapeHtml(status || 'Active')}</span>`;
    }

    function renderSupplierCard(s) {
        const medicines = Array.isArray(s.medicines_supplied) ? s.medicines_supplied : [];
        const shown = medicines.slice(0, 5).map(m =>
            `<span class="sup-chip">${escapeHtml(m)}</span>`
        ).join('');
        const medsHtml = medicines.length
            ? shown + (medicines.length > 5 ? `<span class="sup-chip-more">+${medicines.length - 5} more</span>` : '')
            : `<span class="sup-chip-empty">No medicines linked yet</span>`;

        const reasonHtml = (s.status === 'Inactive' && s.inactive_reason)
            ? `<p class="sup-reason"><i class="fas fa-circle-info"></i> ${escapeHtml(s.inactive_reason)}</p>`
            : '';

        const policy = String(s.consignment_policy || 'none').toLowerCase();
        const policyLabel = policy === 'returnable'
            ? 'Consignment: returnable'
            : (policy === 'non_returnable' ? 'Consignment: non-returnable' : 'No consignment policy');
        const policyChip = `<span class="sup-chip">${escapeHtml(policyLabel)}</span>`;
        const isActive = s.status === 'Active';

        return `
        <div class="supplier-card" data-id="${s.supplier_id}">
            <div class="sup-card-top">
                <div>
                    <h3>${escapeHtml(s.supplier_name)}</h3>
                    ${statusBadge(s.status)}
                </div>
                <div class="sup-card-actions">
                    <button type="button" class="sup-icon-btn edit-supplier-btn" title="Edit supplier"><i class="fas fa-pen"></i></button>
                    <button type="button" class="sup-icon-btn toggle-supplier-btn ${isActive ? 'is-active' : 'is-inactive'}" title="${isActive ? 'Deactivate' : 'Reactivate'}">
                        <i class="fas ${isActive ? 'fa-ban' : 'fa-rotate-left'}"></i>
                    </button>
                </div>
            </div>
            <div class="sup-meta">
                <div><i class="fas fa-phone"></i> ${escapeHtml(s.contact_number) || '-'}</div>
                <div><i class="fas fa-envelope"></i> ${escapeHtml(s.email) || '-'}</div>
                <div><i class="fas fa-location-dot"></i> ${escapeHtml(s.address) || '-'}</div>
            </div>
            <div class="sup-meds">
                <p class="sup-meds-label">Consignment policy</p>
                ${policyChip}
                <p class="sup-meds-label">Medicines supplied</p>
                ${medsHtml}
            </div>
            ${reasonHtml}
        </div>`;
    }

    function applyFiltersAndRender() {
        const list = document.querySelector('.supplier-list');
        if (!list) return;

        const search = (document.getElementById('supplier-search')?.value || '').trim().toLowerCase();
        const statusFilter = document.getElementById('supplier-status-filter')?.value || 'all';

        const filtered = allSuppliers.filter(s => {
            if (search && !(s.supplier_name || '').toLowerCase().includes(search)) return false;
            if (statusFilter === 'active' && s.status !== 'Active') return false;
            if (statusFilter === 'inactive' && s.status !== 'Inactive') return false;
            return true;
        });

        list.innerHTML = filtered.length
            ? filtered.map(renderSupplierCard).join('')
            : '<p class="empty-message">No suppliers found.</p>';
    }

    function fetchSuppliers() {
        const list = document.querySelector('.supplier-list');
        if (list) list.innerHTML = '<p class="empty-message">Loading suppliers...</p>';

        return fetch('/api/admin/suppliers')
            .then(res => {
                if (!res.ok) throw new Error('HTTP ' + res.status);
                return res.json();
            })
            .then(data => {
                allSuppliers = Array.isArray(data) ? data : [];
                applyFiltersAndRender();
            })
            .catch(err => {
                console.error('Failed to load suppliers:', err);
                if (list) list.innerHTML = '<p class="empty-message empty-error">Failed to load suppliers. Please try again.</p>';
            });
    }

    function openSupplierModal(mode, supplier) {
        const isEdit = mode === 'edit';

        const overlay = document.createElement('div');
        overlay.className = 'modal-overlay um-modal-overlay';

        overlay.innerHTML = `
            <div class="modal-content um-modal-card">
                <div class="um-modal-head">
                    <h3>${isEdit ? 'Edit Supplier' : 'Add Supplier'}</h3>
                    <span class="close-modal" title="Close">&times;</span>
                </div>
                <form id="supplierForm">
                    <label for="sf-name">Supplier Name</label>
                    <input type="text" id="sf-name" required value="${isEdit ? escapeHtml(supplier.supplier_name) : ''}">
                    <label for="sf-contact">Contact Number</label>
                    <input type="text" id="sf-contact" value="${isEdit ? escapeHtml(supplier.contact_number) : ''}">
                    <label for="sf-email">Email</label>
                    <input type="email" id="sf-email" value="${isEdit ? escapeHtml(supplier.email) : ''}">
                    <label for="sf-address">Address</label>
                    <input type="text" id="sf-address" value="${isEdit ? escapeHtml(supplier.address) : ''}">
                    <label for="sf-consignment">Consignment policy</label>
                    <select id="sf-consignment">
                        <option value="none" ${!isEdit || supplier.consignment_policy === 'none' || !supplier.consignment_policy ? 'selected' : ''}>None (not consignment)</option>
                        <option value="returnable" ${isEdit && supplier.consignment_policy === 'returnable' ? 'selected' : ''}>Returnable</option>
                        <option value="non_returnable" ${isEdit && supplier.consignment_policy === 'non_returnable' ? 'selected' : ''}>Non-returnable</option>
                    </select>
                    ${isEdit ? `
                    <label for="sf-status">Status</label>
                    <select id="sf-status">
                        <option value="Active" ${supplier.status === 'Active' ? 'selected' : ''}>Active</option>
                        <option value="Inactive" ${supplier.status === 'Inactive' ? 'selected' : ''}>Inactive</option>
                    </select>
                    <div id="sf-reason-wrap" style="display:${supplier.status === 'Inactive' ? 'block' : 'none'};">
                        <label for="sf-reason">Reason for Deactivation</label>
                        <input type="text" id="sf-reason" value="${escapeHtml(supplier.inactive_reason || '')}" placeholder="e.g. No longer supplying">
                    </div>` : ''}
                    <button type="submit">
                        <i class="fas fa-floppy-disk"></i> ${isEdit ? 'Save Changes' : 'Add Supplier'}
                    </button>
                </form>
            </div>`;
        document.body.appendChild(overlay);

        const close = () => overlay.remove();
        overlay.querySelector('.close-modal').onclick = close;
        overlay.addEventListener('click', e => { if (e.target === overlay) close(); });

        const statusSelect = overlay.querySelector('#sf-status');
        if (statusSelect) {
            statusSelect.addEventListener('change', () => {
                const wrap = overlay.querySelector('#sf-reason-wrap');
                if (wrap) wrap.style.display = statusSelect.value === 'Inactive' ? 'block' : 'none';
            });
        }

        overlay.querySelector('#supplierForm').addEventListener('submit', function (e) {
            e.preventDefault();
            const payload = {
                name: overlay.querySelector('#sf-name').value.trim(),
                contact: overlay.querySelector('#sf-contact').value.trim(),
                email: overlay.querySelector('#sf-email').value.trim(),
                address: overlay.querySelector('#sf-address').value.trim(),
                consignment_policy: overlay.querySelector('#sf-consignment')?.value || 'none',
            };
            if (!payload.name) { alert('Supplier name is required.'); return; }

            let url = '/api/admin/suppliers';
            if (isEdit) {
                url = '/api/admin/suppliers/update';
                payload.supplier_id = supplier.supplier_id;
                payload.status = overlay.querySelector('#sf-status').value;
                payload.inactive_reason = overlay.querySelector('#sf-reason')?.value.trim() || '';
            }

            fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            })
                .then(res => res.json())
                .then(result => {
                    if (result.success) {
                        close();
                        fetchSuppliers();
                    } else {
                        alert(result.message || 'Something went wrong. Please try again.');
                    }
                })
                .catch(err => alert('Error: ' + err.message));
        });
    }

    function submitStatusChange(supplier, status, reason) {
        fetch('/api/admin/suppliers/update', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                supplier_id: supplier.supplier_id,
                name: supplier.supplier_name,
                contact: supplier.contact_number,
                email: supplier.email,
                address: supplier.address,
                consignment_policy: supplier.consignment_policy || 'none',
                status,
                inactive_reason: reason
            })
        })
            .then(res => res.json())
            .then(result => {
                if (result.success) {
                    fetchSuppliers();
                } else {
                    alert(result.message || 'Something went wrong. Please try again.');
                }
            })
            .catch(err => alert('Error: ' + err.message));
    }

    function handleListClick(e) {
        const card = e.target.closest('.supplier-card');
        if (!card) return;
        const id = card.getAttribute('data-id');
        const supplier = allSuppliers.find(s => String(s.supplier_id) === String(id));
        if (!supplier) return;

        if (e.target.closest('.edit-supplier-btn')) {
            openSupplierModal('edit', supplier);
        } else if (e.target.closest('.toggle-supplier-btn')) {
            if (supplier.status === 'Active') {
                window.phPrompt('Reason for deactivating "' + supplier.supplier_name + '":', supplier.inactive_reason || '').then(function (reason) {
                    if (reason === null) return;
                    submitStatusChange(supplier, 'Inactive', reason);
                });
            } else {
                window.phConfirm('Reactivate "' + supplier.supplier_name + '"?').then(function (ok) {
                    if (!ok) return;
                    submitStatusChange(supplier, 'Active', '');
                });
            }
        }
    }

    function init() {
        fetchSuppliers();
        if (eventsBound) return;
        eventsBound = true;

        document.getElementById('supplier-search')?.addEventListener('input', applyFiltersAndRender);
        document.getElementById('supplier-status-filter')?.addEventListener('change', applyFiltersAndRender);
        document.querySelector('.btn-add-supplier')?.addEventListener('click', () => openSupplierModal('add'));
        document.querySelector('.supplier-list')?.addEventListener('click', handleListClick);
    }

    window.initializeSupplierModule = init;
})();