(function () {
    const body = document.getElementById('pharmacistPrescriptionsBody');
    const modal = document.getElementById('pharmacistPrescriptionReviewModal');
    const matchesBox = document.getElementById('pharmacistRxMatches');
    const message = document.getElementById('pharmacistRxMessage');
    let prescriptions = [];
    let currentPrescription = null;
    let medicineSearchTimer = null;

    function escapeHtml(value) {
        const node = document.createElement('span');
        node.textContent = value == null ? '' : String(value);
        return node.innerHTML;
    }

    function attr(value) {
        return escapeHtml(value).replace(/"/g, '&quot;');
    }

    function reviewLabel(status) {
        if (status === 'approved') return 'Approved';
        if (status === 'rejected') return 'Rejected';
        return 'Pending';
    }

    function renderQueue() {
        if (!body) return;
        if (!prescriptions.length) {
            body.innerHTML = '<tr><td colspan="7" style="text-align:center;padding:18px;">No prescriptions to review.</td></tr>';
            return;
        }
        body.innerHTML = prescriptions.map((rx) => {
            const meds = (rx.availability_summary || []).map((item) => item.name || item.generic_name || 'Medicine');
            const medText = meds.length ? meds.map(escapeHtml).join(', ') : 'No OCR matches';
            const status = String(rx.review_status || 'pending').toLowerCase();
            return `<tr>
                <td>Rx #${Number(rx.id)}</td>
                <td>${escapeHtml(rx.customer_name)}</td>
                <td>${escapeHtml(rx.created_at || '-')}</td>
                <td>${escapeHtml(rx.ocr_status || 'pending')}</td>
                <td>${escapeHtml(reviewLabel(status))}${rx.review_note ? `<div title="${attr(rx.review_note)}">${escapeHtml(rx.review_note)}</div>` : ''}</td>
                <td>${medText}</td>
                <td><button type="button" class="btn btn-primary" data-review-rx="${Number(rx.id)}">Review</button></td>
            </tr>`;
        }).join('');
    }

    async function loadQueue() {
        if (!body) return;
        body.innerHTML = '<tr><td colspan="7" style="text-align:center;padding:18px;">Loading prescriptions...</td></tr>';
        try {
            const response = await fetch('/api/cashier/prescriptions', { credentials: 'same-origin' });
            const data = await response.json();
            if (!response.ok || !data.success) throw new Error(data.message || 'Could not load prescriptions.');
            prescriptions = Array.isArray(data.prescriptions) ? data.prescriptions : [];
            const pendingCount = prescriptions.filter((rx) => String(rx.review_status || 'pending').toLowerCase() === 'pending').length;
            const badge = document.getElementById('pendingRxBadge');
            if (badge) badge.textContent = String(pendingCount);
            renderQueue();
        } catch (error) {
            console.error('Pharmacist prescription queue failed:', error);
            body.innerHTML = '<tr><td colspan="7" style="color:#b91c1c;text-align:center;padding:18px;">Failed to load prescriptions. Please try again.</td></tr>';
        }
    }

    function openReview(rx) {
        currentPrescription = rx;
        const title = document.getElementById('pharmacistRxTitle');
        const customer = document.getElementById('pharmacistRxCustomer');
        const file = document.getElementById('pharmacistRxFile');
        const idFile = document.getElementById('pharmacistRxIdFile');
        const idVerified = document.getElementById('pharmacistRxIdVerified');
        const text = document.getElementById('pharmacistRxText');
        const note = document.getElementById('pharmacistRxNote');
        const approve = document.getElementById('approvePharmacistRx');
        const reject = document.getElementById('rejectPharmacistRx');
        const rxMatches = (rx.availability_summary || []).filter((item) => String(item.sale_class || 'rx').toLowerCase() === 'rx');
        const reviewedIds = (rx.reviewed_drug_ids || []).map(Number);
        if (title) title.textContent = `Review prescription #${rx.id}`;
        if (customer) customer.textContent = `${rx.customer_name} · Uploaded ${rx.created_at || ''}`;
        if (file) {
            file.href = rx.file_url || '#';
            file.hidden = !rx.file_url;
        }
        if (idFile) {
            idFile.href = rx.id_file_url || '#';
            idFile.hidden = !rx.id_file_url;
        }
        if (idVerified) {
            idVerified.checked = false;
            idVerified.disabled = !rx.id_file_url;
        }
        if (text) text.textContent = rx.extracted_text || 'No readable OCR text. The pharmacist must review the uploaded file manually.';
        if (note) note.value = rx.review_note || '';
        if (message) message.textContent = '';
        if (matchesBox) {
            matchesBox.innerHTML = rxMatches.length
                ? rxMatches.map((item) => {
                    const id = Number(item.drug_id);
                    return `<label style="display:flex;gap:8px;align-items:center;padding:6px 0;">
                        <input type="checkbox" class="pharmacist-rx-drug" value="${id}" ${reviewedIds.includes(id) ? 'checked' : ''}>
                        <span>${escapeHtml(item.name || item.generic_name || 'Medicine')} · ${escapeHtml(item.status || 'Availability unknown')}</span>
                    </label>`;
                }).join('')
                : '<p>No prescription-only medicines were matched from the OCR text. Check the uploaded file, then add the prescribed medicines using search.</p>';
        }
        const search = document.getElementById('pharmacistRxDrugSearch');
        const searchResults = document.getElementById('pharmacistRxSearchResults');
        if (search) search.value = '';
        if (searchResults) searchResults.innerHTML = '';
        if (approve) approve.disabled = false;
        if (reject) reject.disabled = false;
        if (modal) modal.style.display = 'flex';
    }

    function addMedicineCheckbox(medicine) {
        if (!matchesBox) return;
        const id = Number(medicine.drug_id);
        const existing = matchesBox.querySelector(`.pharmacist-rx-drug[value="${id}"]`);
        if (existing) {
            existing.checked = true;
            return;
        }
        const name = [medicine.brand_name, medicine.generic_name, medicine.dosage, medicine.form]
            .filter(Boolean)
            .join(' / ');
        const label = document.createElement('label');
        label.style.cssText = 'display:flex;gap:8px;align-items:center;padding:6px 0;';
        label.innerHTML = `<input type="checkbox" class="pharmacist-rx-drug" value="${id}" checked><span>${escapeHtml(name)}</span>`;
        matchesBox.appendChild(label);
    }

    async function searchMedicines() {
        const query = document.getElementById('pharmacistRxDrugSearch')?.value.trim() || '';
        const results = document.getElementById('pharmacistRxSearchResults');
        const targetPrescription = currentPrescription;
        if (!results) return;
        if (query.length < 2) {
            results.innerHTML = '<p>Type at least two characters to search.</p>';
            return;
        }
        results.textContent = 'Searching...';
        try {
            const url = new URL('/api/cashier/prescription-medicines', window.location.origin);
            url.searchParams.set('search', query);
            const response = await fetch(url.toString(), { credentials: 'same-origin' });
            const data = await response.json();
            if (!response.ok || !data.success) throw new Error(data.message || 'Could not search medicines.');
            if (currentPrescription !== targetPrescription) return;
            const medicines = Array.isArray(data.medicines) ? data.medicines : [];
            currentPrescription.medicineSearchResults = medicines;
            results.innerHTML = medicines.length
                ? medicines.map((medicine) => {
                    const name = [medicine.brand_name, medicine.generic_name, medicine.dosage, medicine.form]
                        .filter(Boolean)
                        .join(' / ');
                    return `<button type="button" data-add-rx-drug="${Number(medicine.drug_id)}">${escapeHtml(name)}</button>`;
                }).join('')
                : '<p>No active prescription medicines match.</p>';
        } catch (error) {
            console.error('Prescription medicine search failed:', error);
            results.textContent = error.message || 'Search failed. Please try again.';
        }
    }

    async function submitReview(decision) {
        if (!currentPrescription) return;
        const note = document.getElementById('pharmacistRxNote')?.value.trim() || '';
        const drugIds = Array.from(document.querySelectorAll('.pharmacist-rx-drug:checked'))
            .map((input) => Number(input.value))
            .filter((id) => Number.isInteger(id) && id > 0);
        if (decision === 'approved' && !drugIds.length) {
            if (message) message.textContent = 'Select at least one matched prescription medicine.';
            return;
        }
        if (decision === 'rejected' && !note) {
            if (message) message.textContent = 'Add a reason before rejecting this prescription.';
            return;
        }
        const idVerified = document.getElementById('pharmacistRxIdVerified')?.checked === true;
        if (decision === 'approved' && !idVerified) {
            if (message) message.textContent = 'Verify the customer ID and prescription document before approval.';
            return;
        }
        const buttons = [
            document.getElementById('approvePharmacistRx'),
            document.getElementById('rejectPharmacistRx')
        ].filter(Boolean);
        buttons.forEach((button) => { button.disabled = true; });
        if (message) message.textContent = 'Saving review...';
        try {
            const response = await fetch(`/api/cashier/prescriptions/${currentPrescription.id}/review`, {
                method: 'POST',
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ decision, note, drug_ids: drugIds, id_verified: idVerified })
            });
            const data = await response.json();
            if (!response.ok || !data.success) throw new Error(data.message || 'Could not save review.');
            if (modal) modal.style.display = 'none';
            await loadQueue();
        } catch (error) {
            console.error('Prescription review failed:', error);
            if (message) message.textContent = error.message || 'Could not save review.';
            buttons.forEach((button) => { button.disabled = false; });
        }
    }

    body?.addEventListener('click', (event) => {
        const button = event.target.closest('[data-review-rx]');
        if (!button) return;
        const rx = prescriptions.find((item) => String(item.id) === button.getAttribute('data-review-rx'));
        if (rx) openReview(rx);
    });
    document.getElementById('approvePharmacistRx')?.addEventListener('click', () => submitReview('approved'));
    document.getElementById('rejectPharmacistRx')?.addEventListener('click', () => submitReview('rejected'));
    document.getElementById('pharmacistRxDrugSearch')?.addEventListener('input', () => {
        clearTimeout(medicineSearchTimer);
        medicineSearchTimer = setTimeout(searchMedicines, 250);
    });
    document.getElementById('pharmacistRxSearchResults')?.addEventListener('click', (event) => {
        const button = event.target.closest('[data-add-rx-drug]');
        if (!button || !currentPrescription) return;
        const id = Number(button.getAttribute('data-add-rx-drug'));
        const medicine = (currentPrescription.medicineSearchResults || []).find((item) => Number(item.drug_id) === id);
        if (medicine) addMedicineCheckbox(medicine);
    });
    document.getElementById('closePharmacistRxReview')?.addEventListener('click', () => {
        if (modal) modal.style.display = 'none';
        currentPrescription = null;
    });
    document.querySelector('.nav-item[data-page="prescriptions-page"]')?.addEventListener('click', loadQueue);
})();
