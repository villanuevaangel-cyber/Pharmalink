/**
 * supplier.js - Supplier list for the admin page.
 * The table stays one line per supplier. Add and edit open a popup.
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

    function renderRow(s) {
        const medicines = Array.isArray(s.medicines_supplied) ? s.medicines_supplied : [];
        const countLabel = medicines.length ? String(medicines.length) : '0';
        const tip = medicines.length ? medicines.join(', ') : 'No medicines linked yet';
        const contact = s.contact_number || s.email || '—';
        const isActive = s.status === 'Active';

        return `
        <tr data-id="${s.supplier_id}">
            <td class="sup-name">${escapeHtml(s.supplier_name)}</td>
            <td class="sup-contact" title="${escapeHtml(contact)}">${escapeHtml(contact)}</td>
            <td>${statusBadge(s.status)}</td>
            <td><span class="sup-count" title="${escapeHtml(tip)}">${countLabel}</span></td>
            <td>
                <div class="sup-actions">
                    <button type="button" class="um-btn um-btn-edit edit-supplier-btn" title="Edit" aria-label="Edit"><i class="fas fa-pen"></i></button>
                    <button type="button" class="um-btn ${isActive ? 'um-btn-danger' : 'um-btn-activate'} toggle-supplier-btn" title="${isActive ? 'Deactivate' : 'Reactivate'}" aria-label="${isActive ? 'Deactivate' : 'Reactivate'}">
                        <i class="fas ${isActive ? 'fa-ban' : 'fa-rotate-left'}"></i>
                    </button>
                </div>
            </td>
        </tr>`;
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
            ? filtered.map(renderRow).join('')
            : '<tr><td colspan="5" class="empty-message">No suppliers found.</td></tr>';
    }

    function fetchSuppliers() {
        const list = document.querySelector('.supplier-list');
        if (list) list.innerHTML = '<tr><td colspan="5" class="empty-message">Loading suppliers...</td></tr>';

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
                if (list) list.innerHTML = '<tr><td colspan="5" class="empty-message empty-error">Failed to load suppliers. Please try again.</td></tr>';
            });
    }

    function medicineListHtml(supplier) {
        const medicines = supplier && Array.isArray(supplier.medicines_supplied) ? supplier.medicines_supplied : [];
        if (!medicines.length) return '<li>No medicines linked yet.</li>';
        return medicines.map(name => `<li>${escapeHtml(name)}</li>`).join('');
    }

    function openSupplierModal(mode, supplier) {
        const isEdit = mode === 'edit' && supplier;
        const overlay = document.createElement('div');
        overlay.className = 'modal-overlay um-modal-overlay';

        overlay.innerHTML = `
            <div class="modal-content um-modal-card sup-modal">
                <div class="um-modal-head">
                    <h3>${isEdit ? 'Edit supplier' : 'Add supplier'}</h3>
                    <span class="close-modal" title="Close">&times;</span>
                </div>
                <form id="supplierForm">
                    <label for="sf-name">Supplier name</label>
                    <input type="text" id="sf-name" required value="${isEdit ? escapeHtml(supplier.supplier_name) : ''}">
                    <label for="sf-contact">Contact number</label>
                    <input type="text" id="sf-contact" value="${isEdit ? escapeHtml(supplier.contact_number) : ''}">
                    <label for="sf-email">Email</label>
                    <input type="email" id="sf-email" value="${isEdit ? escapeHtml(supplier.email) : ''}">
                    <label for="sf-address">Address</label>
                    <input type="text" id="sf-address" value="${isEdit ? escapeHtml(supplier.address) : ''}">
                    <label for="sf-consignment">Consignment</label>
                    <select id="sf-consignment">
                        <option value="none" ${!isEdit || !supplier.consignment_policy || supplier.consignment_policy === 'none' ? 'selected' : ''}>None</option>
                        <option value="returnable" ${isEdit && supplier.consignment_policy === 'returnable' ? 'selected' : ''}>Returnable</option>
                        <option value="non_returnable" ${isEdit && supplier.consignment_policy === 'non_returnable' ? 'selected' : ''}>Non-returnable</option>
                    </select>
                    ${isEdit ? `
                    <label for="sf-status">Status</label>
                    <select id="sf-status">
                        <option value="Active" ${supplier.status === 'Active' ? 'selected' : ''}>Active</option>
                        <option value="Inactive" ${supplier.status === 'Inactive' ? 'selected' : ''}>Inactive</option>
                    </select>
                    <div id="sf-reason-wrap" ${supplier.status === 'Inactive' ? '' : 'hidden'}>
                        <label for="sf-reason">Reason for deactivation</label>
                        <input type="text" id="sf-reason" value="${escapeHtml(supplier.inactive_reason || '')}" placeholder="Example: No longer supplying">
                    </div>
                    <div class="sup-meds">
                        <p class="sup-meds-label">Medicines supplied</p>
                        <ul class="sup-meds-scroll">${medicineListHtml(supplier)}</ul>
                    </div>` : ''}
                    <button type="submit">
                        <i class="fas fa-floppy-disk"></i> ${isEdit ? 'Save changes' : 'Add supplier'}
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
                if (wrap) wrap.hidden = statusSelect.value !== 'Inactive';
            });
        }

        overlay.querySelector('#supplierForm').addEventListener('submit', function (e) {
            e.preventDefault();
            const payload = {
                name: overlay.querySelector('#sf-name').value.trim(),
                contact: overlay.querySelector('#sf-contact').value.trim(),
                email: overlay.querySelector('#sf-email').value.trim(),
                address: overlay.querySelector('#sf-address').value.trim(),
                consignment_policy: overlay.querySelector('#sf-consignment')?.value || 'none'
            };
            if (!payload.name) {
                alert('Supplier name is required.');
                return;
            }

            let url = '/api/admin/suppliers';
            if (isEdit) {
                url = '/api/admin/suppliers/update';
                payload.supplier_id = supplier.supplier_id;
                payload.status = overlay.querySelector('#sf-status').value;
                payload.inactive_reason = payload.status === 'Inactive'
                    ? (overlay.querySelector('#sf-reason')?.value.trim() || '')
                    : '';
            }

            const submitBtn = overlay.querySelector('button[type="submit"]');
            submitBtn.disabled = true;
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
                .catch(err => alert('Error: ' + err.message))
                .finally(() => { submitBtn.disabled = false; });
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
                if (result.success) fetchSuppliers();
                else alert(result.message || 'Something went wrong. Please try again.');
            })
            .catch(err => alert('Error: ' + err.message));
    }

    function handleListClick(e) {
        const row = e.target.closest('tr[data-id]');
        if (!row) return;
        const supplier = allSuppliers.find(s => String(s.supplier_id) === String(row.getAttribute('data-id')));
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
