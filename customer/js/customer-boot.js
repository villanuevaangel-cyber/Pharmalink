(function () {
    function ready(fn) {
        if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', fn);
        else fn();
    }

    ready(async function () {
        const meRes = await fetch('/api/auth/me', { credentials: 'same-origin' });
        let me = null;
        if (meRes.ok) {
            try { me = await meRes.json(); } catch (e) { me = null; }
        }
        const isCustomer = me && me.success && String(me.role || '').toLowerCase() === 'customer';
        if (me && me.success && !isCustomer) {
            window.location.href = '/';
            return;
        }

        if (isCustomer) {
            window.CUSTOMER_GUEST = false;
            window.CUSTOMER_ID = me.user_id;
            window.GLOBAL_UNIQUE_TOKEN_FROM_PHP_SESSION = me.order_token || 'no_token';

            const fullName = [me.firstName, me.lastName].filter(Boolean).join(' ').trim() || 'Customer';
            const welcome = document.getElementById('headerWelcomeName');
            if (welcome) welcome.textContent = 'Welcome, ' + fullName;
            const homeName = document.getElementById('homeFirstName');
            if (homeName) homeName.textContent = me.firstName || fullName;
            if (me.profile_image) applyAvatar(me.profile_image);
        } else {
            setupGuestShop();
        }

        await loadProducts();
        if (!window.CUSTOMER_GUEST) {
            await loadProfile();
            initProfileForm();
            initPrescriptions();
            initOrderTabs();
        }

        const script = document.createElement('script');
        script.src = '/customer/js/customer.js?v=orders-all1';
        document.body.appendChild(script);
    });

    function setupGuestShop() {
        window.CUSTOMER_GUEST = true;
        window.CUSTOMER_ID = null;
        window.GLOBAL_UNIQUE_TOKEN_FROM_PHP_SESSION = 'no_token';
        document.body.classList.add('customer-guest');
        document.getElementById('home')?.classList.remove('active');
        document.getElementById('products')?.classList.add('active');
        document.querySelectorAll('.sidebar-nav .nav-item').forEach((item) => {
            item.classList.toggle('active', item.getAttribute('data-target') === 'products');
        });
        if (window.phSetPortalHash) window.phSetPortalHash('products');
        const welcome = document.getElementById('headerWelcomeName');
        if (welcome) welcome.textContent = 'Browsing as guest';
        const authLink = document.getElementById('customerAuthLink');
        if (authLink) {
            authLink.href = '/';
            authLink.innerHTML = '<i class="fas fa-sign-in-alt"></i><span style="margin-left:6px">Log in</span>';
        }
        const note = document.getElementById('guestShopNote');
        if (note) note.hidden = false;
        const cartPanel = document.getElementById('cart-panel');
        if (cartPanel) cartPanel.style.display = 'block';
        const headerProfile = document.getElementById('headerProfileTrigger');
        if (headerProfile) {
            headerProfile.title = 'Log in';
            headerProfile.onclick = function () { window.location.href = '/'; };
        }
    }

    async function loadProducts() {
        const grid = document.getElementById('productGrid');
        const catSelect = document.getElementById('categoryFilter');
        if (!grid) return;
        try {
            const res = await fetch('/api/customer/products', { credentials: 'same-origin' });
            const data = await res.json();
            const products = data.products || [];
            if (catSelect) {
                (data.categories || []).forEach((cat) => {
                    const opt = document.createElement('option');
                    opt.value = cat;
                    opt.textContent = cat;
                    catSelect.appendChild(opt);
                });
            }
            if (!products.length) {
                grid.innerHTML = '<p class="shop-empty">No products available right now.</p>';
                return;
            }
            grid.innerHTML = products.map((p) => {
                const displayName = `${p.brand_name} / ${p.generic_name} ${p.dosage} ${p.form}`.trim();
                const stock = parseInt(p.current_stock, 10) || 0;
                const stockClass = stock <= 0 ? 'shop-stock-out' : (stock <= 10 ? 'shop-stock-low' : 'shop-stock-ok');
                return `<div class="product" data-category="${p.category}" data-name-search="${displayName}">
                    <span class="shop-cat">${p.category}</span>
                    <h4>${p.brand_name}</h4>
                    <p class="shop-meta">${p.generic_name} · ${p.dosage} · ${p.form}</p>
                    <div class="shop-card-foot">
                        <p class="shop-price">₱${Number(p.price).toFixed(2)}</p>
                        <p class="shop-stock ${stockClass}">${stock} in stock</p>
                    </div>
                    <button type="button" class="add-btn"
                        data-drug-id="${p.drug_id}"
                        data-lot-id="${p.lot_inventory_id}"
                        data-name="${displayName}"
                        data-price="${p.price}"
                        data-stock="${stock}">
                        <i class="fas fa-cart-plus"></i> Add to Cart
                    </button>
                </div>`;
            }).join('');
        } catch (err) {
            grid.innerHTML = '<p class="shop-empty shop-empty-error">Failed to load products.</p>';
        }
    }

    async function loadProfile() {
        const res = await fetch('/api/customer/profile', { credentials: 'same-origin' });
        const data = await res.json();
        if (!data.success || !data.data) return;
        const c = data.data;
        const form = document.getElementById('customerProfileForm');
        if (form) {
            form.first_name.value = c.first_name || '';
            form.middle_name.value = c.middle_name || '';
            form.last_name.value = c.last_name || '';
            form.email.value = c.email || '';
            form.phone_number.value = window.phProfileValidate
                ? window.phProfileValidate.displayPhone(c.phone_number || '')
                : (c.phone_number || '');
            form.address.value = c.address || '';
        }
        const avatar = c.profile_image || 'https://cdn-icons-png.flaticon.com/512/2922/2922510.png';
        applyAvatar(avatar);
        const nameEl = document.getElementById('profileCardName');
        if (nameEl) nameEl.textContent = `${c.first_name || ''} ${c.last_name || ''}`.trim() || 'Customer';
        const welcome = document.getElementById('headerWelcomeName');
        if (welcome) welcome.textContent = 'Welcome, ' + (`${c.first_name || ''} ${c.last_name || ''}`.trim() || 'Customer');
        const userEl = document.getElementById('profileUsername');
        if (userEl) userEl.textContent = c.username || 'customer';
        const typeEl = document.getElementById('profileType');
        if (typeEl) typeEl.textContent = c.customer_type || 'Regular';
        const ptsEl = document.getElementById('profilePoints');
        if (ptsEl) ptsEl.textContent = c.loyalty_points || '0.00';
    }

    const DEFAULT_AVATAR = 'https://cdn-icons-png.flaticon.com/512/2922/2922510.png';
    function applyAvatar(src) {
        const avatar = (src && src !== DEFAULT_AVATAR) ? src : DEFAULT_AVATAR;
        const preview = document.getElementById('profilePreview');
        if (preview) {
            preview.src = avatar;
            preview.onerror = function () { this.onerror = null; this.src = DEFAULT_AVATAR; };
        }
        const headerAvatar = document.getElementById('headerProfileAvatar');
        if (headerAvatar) {
            headerAvatar.src = avatar;
            headerAvatar.onerror = function () { this.onerror = null; this.src = DEFAULT_AVATAR; };
        }
        return avatar;
    }

    function initProfileForm() {
        const form = document.getElementById('customerProfileForm');
        const inputs = form ? form.querySelectorAll('.p-input') : [];
        const editBtn = document.getElementById('profile_editBtn');
        const saveBtn = document.getElementById('profile_saveBtn');
        const cancelBtn = document.getElementById('profile_cancelBtn');
        const msg = document.getElementById('profileFormMsg');
        const previewImg = document.getElementById('profilePreview');
        const fileInput = document.getElementById('profile_image_input');
        const original = {};
        let originalPreview = previewImg ? previewImg.src : '';
        if (!form || !editBtn || !saveBtn || !cancelBtn) return;
        const pv = window.phProfileValidate;
        if (pv) {
            pv.bindNameCaps(form);
            pv.bindPhoneDigits(form.phone_number);
            pv.bindLiveFields(form);
        }
        inputs.forEach((input) => { original[input.name] = input.value; });

        function setEditing(isEditing) {
            inputs.forEach((input) => { input.disabled = !isEditing; });
            editBtn.hidden = isEditing;
            saveBtn.hidden = !isEditing;
            cancelBtn.hidden = !isEditing;
            if (msg) msg.textContent = '';
            if (pv) pv.paintFieldErrors(form, {});
        }
        editBtn.addEventListener('click', () => setEditing(true));
        cancelBtn.addEventListener('click', () => {
            inputs.forEach((input) => { input.value = original[input.name] || ''; });
            if (fileInput) fileInput.value = '';
            if (previewImg) previewImg.src = originalPreview;
            setEditing(false);
        });
        if (fileInput) {
            fileInput.addEventListener('change', function () {
                const file = this.files && this.files[0];
                if (!file) return;
                const pErr = pv && pv.photoError(file);
                if (pErr) {
                    if (msg) { msg.textContent = pErr; msg.style.color = '#e74c3c'; }
                    this.value = '';
                    return;
                }
                const formData = new FormData();
                formData.append('profile_image', file);
                if (msg) { msg.textContent = 'Uploading photo...'; msg.style.color = '#6b7280'; }
                fetch('/api/customer/profile-picture', { method: 'POST', body: formData, credentials: 'same-origin' })
                    .then((r) => r.json())
                    .then((data) => {
                        if (data.success && data.path) {
                            originalPreview = applyAvatar(data.path + '?t=' + Date.now());
                            if (msg) { msg.textContent = 'Profile picture updated!'; msg.style.color = '#4BAA8B'; }
                        } else if (msg) {
                            msg.textContent = data.message || 'Failed to upload picture.';
                            msg.style.color = '#e74c3c';
                        }
                        fileInput.value = '';
                    })
                    .catch(() => {
                        if (msg) { msg.textContent = 'Network error. Please try again.'; msg.style.color = '#e74c3c'; }
                    });
            });
        }
        form.addEventListener('submit', function (e) {
            e.preventDefault();
            if (pv) {
                form.first_name.value = pv.titleCaseName(form.first_name.value);
                form.middle_name.value = pv.titleCaseName(form.middle_name.value);
                form.last_name.value = pv.titleCaseName(form.last_name.value);
            }
            const payload = {
                first_name: form.first_name.value,
                middle_name: form.middle_name.value,
                last_name: form.last_name.value,
                email: form.email.value,
                phone_number: pv ? (pv.e164Phone(form.phone_number.value) || form.phone_number.value) : form.phone_number.value,
                address: form.address.value,
            };
            const errors = pv ? pv.fieldErrors(payload) : {};
            if (pv && Object.keys(errors).length) {
                pv.paintFieldErrors(form, errors);
                if (msg) { msg.textContent = 'Please fix the highlighted fields.'; msg.style.color = '#e74c3c'; }
                return;
            }
            if (pv) pv.paintFieldErrors(form, {});
            const nationalPhone = form.phone_number.value;
            form.phone_number.value = payload.phone_number;
            if (msg) { msg.textContent = 'Saving...'; msg.style.color = '#6b7280'; }
            fetch('/api/customer/profile', { method: 'POST', body: new FormData(form), credentials: 'same-origin' })
                .then((r) => r.json())
                .then((data) => {
                    form.phone_number.value = nationalPhone;
                    if (msg) {
                        msg.textContent = data.message || (data.success ? 'Profile updated.' : 'Failed to update profile.');
                        msg.style.color = data.success ? '#4BAA8B' : '#e74c3c';
                    }
                    if (!data.success) return;
                    const customer = data.customer || {};
                    inputs.forEach((input) => {
                        if (Object.prototype.hasOwnProperty.call(customer, input.name)) {
                            input.value = input.name === 'phone_number' && pv
                                ? pv.displayPhone(customer[input.name] || '')
                                : (customer[input.name] || '');
                        }
                        original[input.name] = input.value;
                    });
                    setEditing(false);
                    const fullName = `${form.first_name.value} ${form.last_name.value}`.trim() || 'Customer';
                    const cardName = document.getElementById('profileCardName');
                    if (cardName) cardName.textContent = fullName;
                    const headerWelcome = document.getElementById('headerWelcomeName');
                    if (headerWelcome) headerWelcome.textContent = 'Welcome, ' + fullName;
                    const homeName = document.getElementById('homeFirstName');
                    if (homeName) homeName.textContent = form.first_name.value || 'Customer';
                    if (data.profile_image) originalPreview = applyAvatar(data.profile_image + '?t=' + Date.now());
                    if (fileInput) fileInput.value = '';
                })
                .catch(() => {
                    form.phone_number.value = nationalPhone;
                    if (msg) { msg.textContent = 'Network error. Please try again.'; msg.style.color = '#e74c3c'; }
                });
        });

        const passwordForm = document.getElementById('customerPasswordForm');
        if (passwordForm) {
            passwordForm.addEventListener('submit', function (e) {
                e.preventDefault();
                const pmsg = document.getElementById('passwordFormMsg');
                const vErr = window.phProfileValidate && window.phProfileValidate.passwordError(
                    passwordForm.current_password.value,
                    passwordForm.new_password.value,
                    passwordForm.confirm_password.value
                );
                if (vErr) {
                    if (pmsg) { pmsg.textContent = vErr; pmsg.style.color = '#e74c3c'; }
                    return;
                }
                fetch('/api/auth/change-password', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    credentials: 'same-origin',
                    body: JSON.stringify({
                        current_password: passwordForm.current_password.value,
                        new_password: passwordForm.new_password.value,
                        confirm_password: passwordForm.confirm_password.value,
                    }),
                })
                    .then((r) => r.json())
                    .then((data) => {
                        if (pmsg) {
                            pmsg.textContent = data.message || (data.success ? 'Password updated.' : 'Failed.');
                            pmsg.style.color = data.success ? '#4BAA8B' : '#e74c3c';
                        }
                        if (data.success) passwordForm.reset();
                    })
                    .catch(() => {
                        if (pmsg) { pmsg.textContent = 'Network error.'; pmsg.style.color = '#e74c3c'; }
                    });
            });
            if (window.phProfileValidate) {
                window.phProfileValidate.bindHints(passwordForm.new_password, document.getElementById('pwHints'));
            }
        }
    }

    function initPrescriptions() {
        const form = document.getElementById('prescriptionUploadForm');
        const fileInput = document.getElementById('prescriptionFile');
        const msg = document.getElementById('prescriptionUploadMsg');
        const tbody = document.getElementById('prescriptionHistoryBody');
        if (!form || !tbody) return;

        function escapeHtml(str) {
            const div = document.createElement('div');
            div.textContent = str == null ? '' : str;
            return div.innerHTML;
        }
        function peso(n) {
            const v = Number(n);
            if (!Number.isFinite(v)) return '-';
            return '₱' + v.toFixed(2);
        }
        function attr(value) {
            return escapeHtml(value).replace(/"/g, '&quot;');
        }
        function canAddMatch(m) {
            return m && m.status === 'In Stock' && m.lot_id && Number(m.stock) > 0;
        }
        function matchLabel(m) {
            const brand = String(m.brand_name || '').trim();
            const name = String(m.name || '').trim();
            return brand || name;
        }
        function matchRowHtml(m) {
            const ok = canAddMatch(m);
            const label = matchLabel(m);
            const action = ok
                ? `<button type="button" class="rx-add-cart" data-lot-id="${attr(m.lot_id)}" data-name="${attr(label)}" data-price="${attr(m.price)}" data-drug-id="${attr(m.drug_id)}" data-stock="${attr(m.stock)}"><i class="fas fa-cart-plus"></i> Add to cart</button>`
                : `<span class="rx-pill rx-pill-bad">Out of stock</span>`;
            return `<div class="rx-stock-row">
                <strong>${escapeHtml(label)}</strong>
                <span class="rx-price">${ok ? escapeHtml(peso(m.price)) : '-'}</span>
                ${action}
            </div>`;
        }
        function handleRxAddCart(e) {
            const btn = e.target.closest('.rx-add-cart');
            if (!btn) return;
            e.preventDefault();
            if (typeof window.addRxMatchToCart !== 'function') {
                const wait = 'Shop is still loading. Try again in a moment.';
                if (typeof window.phAlert === 'function') window.phAlert(wait);
                else window.alert(wait);
                return;
            }
            const ok = window.addRxMatchToCart({
                lot_id: btn.getAttribute('data-lot-id'),
                name: btn.getAttribute('data-name'),
                price: btn.getAttribute('data-price'),
                drug_id: btn.getAttribute('data-drug-id'),
                stock: btn.getAttribute('data-stock')
            });
            if (ok) {
                const added = (btn.getAttribute('data-name') || 'Item') + ' added to cart.';
                if (typeof window.phAlert === 'function') window.phAlert(added);
            }
        }
        function renderAvailability(p) {
            if (p.ocr_status === 'unavailable') return '<span class="rx-pill rx-pill-muted">OCR not installed on server</span>';
            if (p.ocr_status === 'skipped_pdf') return '<span class="rx-pill rx-pill-muted">OCR skipped (PDF)</span>';
            if (p.ocr_status === 'failed') return '<span class="rx-pill rx-pill-bad">Could not read text</span>';
            if (p.ocr_status === 'pending' || !p.ocr_status) return '<span class="rx-pill rx-pill-muted">Processing…</span>';
            const matches = Array.isArray(p.availability_summary) ? p.availability_summary : [];
            if (!matches.length) return '<span class="rx-pill rx-pill-muted">No matching medicine found</span>';
            return `<div class="rx-stock-list">${matches.map(matchRowHtml).join('')}</div>`;
        }
        function loadHistory() {
            fetch('/api/customer/prescriptions', { credentials: 'same-origin' })
                .then((r) => r.json())
                .then((data) => {
                    if (!data.success || !data.prescriptions.length) {
                        tbody.innerHTML = '<div class="rx-empty"><i class="fas fa-file-medical"></i><h4>No uploads yet</h4><p>Your prescription files will show up here.</p></div>';
                        return;
                    }
                    tbody.innerHTML = data.prescriptions.map((p) => {
                        const text = (p.extracted_text || '').trim();
                        return `
                        <article class="rx-card">
                            <p class="rx-section-label">Extracted text</p>
                            <pre class="rx-excerpt">${text ? escapeHtml(text) : 'No readable text was saved for this file.'}</pre>
                            <p class="rx-section-label">Items</p>
                            <div class="rx-avail">${renderAvailability(p)}</div>
                        </article>`;
                    }).join('');
                })
                .catch(() => {
                    tbody.innerHTML = '<div class="rx-empty rx-empty-error">Failed to load history.</div>';
                });
        }
        tbody.addEventListener('click', handleRxAddCart);
        const fileNameEl = document.getElementById('rxFileName');
        const dropzone = document.getElementById('rxDropzone');
        const allowedExt = { jpg: 1, jpeg: 1, png: 1, webp: 1, pdf: 1 };
        const allowedMime = {
            'image/jpeg': 1,
            'image/png': 1,
            'image/webp': 1,
            'application/pdf': 1
        };
        const maxBytes = 5 * 1024 * 1024;

        function showRxError(text) {
            if (msg) {
                msg.style.color = 'red';
                msg.textContent = text;
            }
            if (typeof window.phAlert === 'function') window.phAlert(text);
            else window.alert(text);
        }
        function clearRxError() {
            if (msg) msg.textContent = '';
        }
        function fileExt(name) {
            const parts = String(name || '').split('.');
            return parts.length > 1 ? parts.pop().toLowerCase() : '';
        }
        function validateRxFile(file) {
            if (!file) return 'Please choose a file to upload.';
            const ext = fileExt(file.name);
            const type = String(file.type || '').toLowerCase();
            const typeOk = allowedExt[ext] || allowedMime[type];
            if (!typeOk) return 'Only JPG, PNG, WEBP, or PDF files are allowed.';
            if (file.size > maxBytes) return 'File must be under 5MB.';
            if (file.size <= 0) return 'That file is empty. Please choose another one.';
            return '';
        }
        function applySelectedFile(file, fromDrop) {
            if (!file) {
                fileInput.value = '';
                if (fileNameEl) fileNameEl.textContent = 'No file selected';
                return false;
            }
            const err = validateRxFile(file);
            if (err) {
                fileInput.value = '';
                if (fileNameEl) fileNameEl.textContent = 'No file selected';
                if (dropzone) dropzone.classList.add('rx-drop-error');
                showRxError(err);
                return false;
            }
            if (fromDrop) {
                const dt = new DataTransfer();
                dt.items.add(file);
                fileInput.files = dt.files;
            }
            if (dropzone) dropzone.classList.remove('rx-drop-error');
            clearRxError();
            if (fileNameEl) fileNameEl.textContent = file.name;
            return true;
        }
        function showSelectedFile() {
            applySelectedFile(fileInput.files[0], false);
        }
        fileInput.addEventListener('change', showSelectedFile);

        const cameraBtn = document.getElementById('rxCameraBtn');
        const cameraCapture = document.getElementById('prescriptionCameraCapture');
        const camOverlay = document.getElementById('rxCameraOverlay');
        const camVideo = document.getElementById('rxCameraVideo');
        const camCanvas = document.getElementById('rxCameraCanvas');
        let cameraStream = null;
        function stopRxCamera() {
            if (cameraStream) {
                cameraStream.getTracks().forEach((t) => t.stop());
                cameraStream = null;
            }
            if (camVideo) camVideo.srcObject = null;
            if (camOverlay) camOverlay.style.display = 'none';
        }
        function useCapturedFile(file) {
            applySelectedFile(file, true);
        }
        function captureFromVideo() {
            if (!camVideo || !camCanvas || !camVideo.videoWidth) {
                showRxError('Camera is not ready yet. Wait a second, then capture.');
                return;
            }
            camCanvas.width = camVideo.videoWidth;
            camCanvas.height = camVideo.videoHeight;
            const ctx = camCanvas.getContext('2d');
            ctx.drawImage(camVideo, 0, 0);
            camCanvas.toBlob((blob) => {
                stopRxCamera();
                if (!blob) {
                    showRxError('Could not capture the photo. Try again.');
                    return;
                }
                useCapturedFile(new File([blob], 'prescription-camera.jpg', { type: 'image/jpeg' }));
            }, 'image/jpeg', 0.88);
        }
        async function openLiveCamera() {
            if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
                cameraCapture?.click();
                return;
            }
            try {
                cameraStream = await navigator.mediaDevices.getUserMedia({
                    video: { facingMode: { ideal: 'environment' } },
                    audio: false
                });
                if (camVideo) {
                    camVideo.srcObject = cameraStream;
                    await camVideo.play().catch(() => {});
                }
                if (camOverlay) camOverlay.style.display = 'flex';
            } catch (err) {
                cameraCapture?.click();
            }
        }
        cameraBtn?.addEventListener('click', (e) => {
            e.preventDefault();
            openLiveCamera();
        });
        cameraCapture?.addEventListener('change', () => {
            const file = cameraCapture.files && cameraCapture.files[0];
            if (file) useCapturedFile(file);
            cameraCapture.value = '';
        });
        document.getElementById('rxCameraSnap')?.addEventListener('click', captureFromVideo);
        document.getElementById('rxCameraClose')?.addEventListener('click', stopRxCamera);
        document.getElementById('rxCameraCancel')?.addEventListener('click', stopRxCamera);
        camOverlay?.addEventListener('click', (e) => { if (e.target === camOverlay) stopRxCamera(); });

        if (dropzone) {
            ['dragenter', 'dragover'].forEach((ev) => {
                dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.add('rx-drop-active'); });
            });
            ['dragleave', 'drop'].forEach((ev) => {
                dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.remove('rx-drop-active'); });
            });
            dropzone.addEventListener('drop', (e) => {
                applySelectedFile(e.dataTransfer?.files?.[0], true);
            });
        }
        form.addEventListener('submit', function (e) {
            e.preventDefault();
            const file = fileInput.files[0];
            const err = validateRxFile(file);
            if (err) {
                showRxError(err);
                return;
            }
            const fd = new FormData();
            fd.append('prescription_file', file);
            msg.style.color = '#555';
            msg.textContent = 'Uploading and reading prescription...';
            fetch('/api/customer/prescriptions', { method: 'POST', body: fd, credentials: 'same-origin' })
                .then((r) => r.json())
                .then((d) => {
                    msg.style.color = d.success ? 'green' : 'red';
                    msg.textContent = d.message || (d.success ? 'Uploaded!' : 'Upload failed.');
                    if (d.success) {
                        form.reset();
                        showSelectedFile();
                        loadHistory();
                    }
                })
                .catch(() => { msg.style.color = 'red'; msg.textContent = 'Upload error.'; });
        });
        loadHistory();
    }

    function initOrderTabs() {
        document.querySelectorAll('.order-type-tab').forEach((btn) => {
            btn.addEventListener('click', () => {
                document.querySelectorAll('.order-type-tab').forEach((b) => {
                    b.classList.toggle('active', b === btn);
                });
                const ordersNav = document.querySelector('.nav-item[data-target="orders"]');
                if (ordersNav) ordersNav.setAttribute('data-order-type', btn.dataset.type || 'all');
                const start = document.getElementById('order_start_date')?.value || '';
                const end = document.getElementById('order_end_date')?.value || '';
                if (window.loadCustomerOrders) window.loadCustomerOrders(btn.dataset.type || 'all', start, end, document.getElementById('order_search')?.value || '');
            });
        });
    }
})();
