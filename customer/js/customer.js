// FILE: customer.js (FINAL CLEAN & FIXED VERSION)

function onCustomerReady(fn) {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', fn);
    else fn();
}

onCustomerReady(function() {

    // =======================================================
    // 1. GLOBAL VARIABLES & CONSTANTS
    // =======================================================
    const cart = {};
    
    // ⭐ FIXED: Tiyakin na defined ang CUSTOMER_ID mula sa PHP
    let selectedCustomer = typeof CUSTOMER_ID !== 'undefined' ? CUSTOMER_ID : null; 
    
    // --- Element Selectors ---
    const navItems = document.querySelectorAll('.nav-item');
    const sections = document.querySelectorAll('section');
    const productGrid = document.getElementById('productGrid');
    const cartPanel = document.getElementById('cart-panel');
    const cartItemsTableBody = document.getElementById('cart-items');
    const ordersContent = document.getElementById('orders'); 
    
    // Summary Spans
    const totalPriceSpan = document.getElementById('total-price');
    const subtotalDisplay = document.getElementById('subtotalDisplay');
    const checkoutBtn = document.getElementById('checkout-btn');
    const clearCartBtn = document.getElementById('clear-cart-btn');
    const emptyCartMessage = document.getElementById('emptyCartMessage');

    // Modals
    const receiptModal = document.getElementById('receiptModal');
    const receiptContent = document.getElementById('receiptContent');
    const closeReceiptModal = document.getElementById('closeReceiptModal');

    // ⭐ ORDER DETAILS MODAL ELEMENTS ⭐
    const orderDetailsModal = document.getElementById('orderDetailsModal');
    const orderDetailsContent = document.getElementById('orderDetailsContent');
    const closeOrderDetailsModal = document.getElementById('closeOrderDetailsModal'); 
    
    // Search & Filter
    const searchInput = document.getElementById('searchInput');
    const categoryFilter = document.getElementById('categoryFilter');

   // notif  
    const notificationBell = document.getElementById('customerNotificationBell') || document.querySelector('.notification');
    const notificationDropdown = document.getElementById('notification-dropdown');    
    
    
    // --- Initial Safety Check ---
    if (!productGrid || !cartItemsTableBody || !totalPriceSpan || !subtotalDisplay || !cartPanel || !ordersContent) {
        console.error("FATAL ERROR: One or more critical elements are missing.");
    }

    // --- Initial Event Listeners ---
    if (searchInput) {
        searchInput.addEventListener('input', applyProductFilters);
    }
    if (categoryFilter) {
        categoryFilter.addEventListener('change', applyProductFilters);
    }
    if (closeReceiptModal) {
        closeReceiptModal.addEventListener('click', () => {
            receiptModal.style.display = 'none';
        });
    }
    // ⭐ Close Listener para sa Order Details Modal
    if (closeOrderDetailsModal) {
        closeOrderDetailsModal.addEventListener('click', () => {
            if (orderDetailsModal) {
                orderDetailsModal.style.display = 'none';
            }
        });
    }


    // =======================================================
    // 2. NAVIGATION LOGIC & ORDER RELOAD
    // =======================================================
    navItems.forEach(item => {
        item.addEventListener('click', () => {
            const target = item.getAttribute('data-target');
            if (window.CUSTOMER_GUEST && target && target !== 'products') {
                window.location.href = '/';
                return;
            }
            navItems.forEach(i => i.classList.remove('active'));
            item.classList.add('active');
            sections.forEach(sec => sec.classList.remove('active'));
            const targetSection = document.getElementById(target);

            if (targetSection) {
                targetSection.classList.add('active');
            }

            // Show/Hide Cart Panel based on the active section
            if (target === 'products') {
                cartPanel.style.display = 'block';
                updateCartPanel();
                refreshProductStock(); // pick up any stock changes since last visit
            } else {
                cartPanel.style.display = 'none';
            }
            
            // NEW: Load orders when the 'orders' tab is clicked
if (target === 'orders') {
    const type = document.querySelector('.order-type-tab.active')?.dataset.type
        || item.getAttribute('data-order-type')
        || 'online';
    const start = document.getElementById('order_start_date')?.value || '';
    const end = document.getElementById('order_end_date')?.value || '';
    window.loadCustomerOrders(type, start, end);
}

            // Refresh Home stats/recent orders every time the tab is opened,
            // so a checkout done elsewhere in the session shows up immediately.
            if (target === 'home') {
                loadHomeStats();
            }

            if (window.phSetPortalHash) window.phSetPortalHash(target);
        });
    if (window.phRestorePortalHash) window.phRestorePortalHash();

    // Real-time refresh for My Orders while that tab is active.
    setInterval(() => {
        const ordersSection = document.getElementById('orders');
        if (ordersSection && ordersSection.classList.contains('active')) {
            const type = document.querySelector('.order-type-tab.active')?.dataset.type
                || document.querySelector('.nav-item[data-target="orders"]')?.getAttribute('data-order-type')
                || 'online';
            const start = document.getElementById('order_start_date')?.value || '';
            const end = document.getElementById('order_end_date')?.value || '';
            window.loadCustomerOrders(type, start, end);
        }
    }, 20000);

    // ===== REAL-TIME HOME STATS =====
    // Keeps "Completed Orders / Total Spent / Loyalty Points" and the
    // Recent Orders table current while the Home tab is open, using the
    // JSON endpoint (get_customer_home_stats.php) instead of requiring a
    // full page reload.
    function homeStatusClass(status) {
        return String(status || '').trim().toLowerCase().replace(/\s+/g, '-');
    }

    function homePayLabel(method) {
        const p = String(method || 'cash').toLowerCase();
        if (p === 'gcash') return 'GCash';
        if (p === 'maya') return 'Maya';
        if (p === 'cash') return 'Cash';
        if (p === 'card') return 'Card';
        if (p === 'bank') return 'Bank Transfer';
        return p.replace(/[_-]+/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
    }

    function renderOrderedItems(items) {
        const tbody = document.getElementById('homeOrderedItems');
        if (!tbody) return;
        if (!items || !items.length) {
            tbody.innerHTML = '<tr><td colspan="4" class="dash-empty-cell">No ordered items yet.</td></tr>';
            return;
        }
        tbody.innerHTML = items.map((it) => {
            const name = it.name || 'Item';
            const generic = it.generic_name && it.generic_name !== name ? it.generic_name : '';
            return `<tr>
                <td>
                    <strong>${name}</strong>
                    ${generic ? `<div class="dash-item-sub">${generic}</div>` : ''}
                </td>
                <td>${it.qty}</td>
                <td>₱${it.spent}</td>
                <td>${it.last_ordered || '-'}</td>
            </tr>`;
        }).join('');
    }

    function renderTxnLogs(logs) {
        const tbody = document.getElementById('homeTxnLogs');
        if (!tbody) return;
        if (!logs || !logs.length) {
            tbody.innerHTML = `<tr><td colspan="7" class="dash-empty-cell">
                You haven't placed any orders yet.
            </td></tr>`;
            return;
        }
        tbody.innerHTML = logs.map((row) => {
            const kind = row.kind === 'walkin' ? 'walkin' : 'online';
            const kindLabel = kind === 'walkin' ? 'Walk-in' : 'Online';
            const cls = homeStatusClass(row.order_status);
            return `<tr>
                <td><strong>#${row.order_id}</strong></td>
                <td>${row.order_date || '-'}</td>
                <td><span class="dash-kind dash-kind-${kind}">${kindLabel}</span></td>
                <td><span class="mo-pay">${homePayLabel(row.payment_method)}</span></td>
                <td><span class="status ${cls}">${row.order_status || '-'}</span></td>
                <td class="mo-amt-cell">₱${row.total_amount}</td>
                <td><button type="button" class="dash-view" onclick="window.showOrderDetails(${row.order_id}, '${kind}')"><i class="fas fa-eye"></i> View</button></td>
            </tr>`;
        }).join('');
    }

    window.loadHomeStats = function loadHomeStats() {
        if (window.CUSTOMER_GUEST) return;
        fetch('/api/customer/home-stats', { credentials: 'same-origin' })
            .then(res => res.json())
            .then(data => {
                if (!data || !data.success) return;
                const ordersEl = document.getElementById('homeStatOrders');
                const spentEl = document.getElementById('homeStatSpent');
                const pointsEl = document.getElementById('homeStatPoints');
                if (ordersEl) ordersEl.textContent = data.total_orders;
                if (spentEl) spentEl.textContent = '₱' + data.total_spent;
                if (pointsEl) pointsEl.textContent = data.loyalty_points;
                renderOrderedItems(data.ordered_items);
                renderTxnLogs(data.transaction_logs || data.recent_orders);
            })
            .catch(err => console.error('Home stats refresh failed:', err));
    };

    setInterval(() => {
        const homeSection = document.getElementById('home');
        if (homeSection && homeSection.classList.contains('active')) {
            loadHomeStats();
        }
    }, 25000);

    // ===== REAL-TIME PRODUCT STOCK =====
    // The product grid is rendered once (server-side, on page load) for a
    // fast first paint, then kept current here by re-checking real stock
    // against ../get_products.php - this is the same active/in-stock query
    // the cashier POS already polls, so both screens agree on what's
    // actually available. Only lot IDs still present in the response are
    // in stock; anything that dropped out (sold out, deactivated, expired)
    // gets disabled without re-rendering the whole grid, so search/filter
    // state and scroll position are preserved.
    window.refreshProductStock = function refreshProductStock() {
        const productsSection = document.getElementById('products');
        if (!productsSection || !productsSection.classList.contains('active')) return;
        if (document.activeElement === searchInput) return; // don't fight typing

        fetch('/api/customer/products', { credentials: 'same-origin' })
            .then(res => res.json())
            .then(payload => {
                const products = Array.isArray(payload) ? payload : (payload.products || []);
                const stockByLot = {};
                products.forEach(p => { stockByLot[String(p.lot_inventory_id)] = parseInt(p.current_stock, 10) || 0; });

                document.querySelectorAll('#productGrid .add-btn[data-lot-id]').forEach(btn => {
                    const lotId = btn.getAttribute('data-lot-id');
                    const stock = stockByLot.hasOwnProperty(lotId) ? stockByLot[lotId] : 0;
                    btn.setAttribute('data-stock', stock);

                    const card = btn.closest('.product');
                    const stockLabel = card ? card.querySelector('p[style*="color:#888"]') : null;
                    if (stockLabel) stockLabel.textContent = stock > 0 ? `${stock} in stock` : 'Out of stock';

                    if (stock <= 0) {
                        btn.disabled = true;
                        btn.style.opacity = '0.5';
                        btn.style.cursor = 'not-allowed';
                    } else {
                        btn.disabled = false;
                        btn.style.opacity = '';
                        btn.style.cursor = 'pointer';
                    }
                });
            })
            .catch(err => console.error('Product stock refresh failed:', err));
    };

    setInterval(refreshProductStock, 20000);

    /**
     * Fetches and loads the customer's orders into the 'orders' section.
     */
/**
     * Fetches and loads the customer's orders into the 'orders' section, optionally with date filters.
     * Ginawang window.function para ma-access ng <script> tag sa PHP output.
     * @param {string} [startDate=''] - Start date filter (YYYY-MM-DD).
     * @param {string} [endDate=''] - End date filter (YYYY-MM-DD).
     */
window.loadCustomerOrders = function(type = 'online', startDate = '', endDate = '') {
    if (window.CUSTOMER_GUEST) return;
    const tbody = document.getElementById('ordersTableBody');
    if (!tbody) return;
    tbody.innerHTML = '<tr><td colspan="6" class="mo-empty-cell"><i class="fas fa-spinner fa-spin"></i> Loading your orders...</td></tr>';

    let fetchUrl = `/api/customer/orders?type=${encodeURIComponent(type)}`;
    if (startDate) fetchUrl += `&start_date=${encodeURIComponent(startDate)}`;
    if (endDate) fetchUrl += `&end_date=${encodeURIComponent(endDate)}`;

    fetch(fetchUrl, { credentials: 'same-origin' })
        .then(response => {
            if (!response.ok) throw new Error('Network response was not ok');
            return response.json();
        })
        .then(data => {
            customerOrdersCache = data.orders || [];
            renderCustomerOrdersTable();
            setupOrderFilterListener();
        })
        .catch(error => {
            console.error('Error loading orders:', error);
            tbody.innerHTML = '<tr><td colspan="6" class="mo-empty-cell">Failed to load orders. Please try again.</td></tr>';
        });
};

    let customerOrdersCache = [];

    function orderSearchQuery() {
        return String(document.getElementById('order_search')?.value || '').trim().toLowerCase();
    }

    function orderPayLabel(method) {
        const p = String(method || 'cash').toLowerCase();
        if (p === 'gcash') return 'GCash';
        if (p === 'maya') return 'Maya';
        if (p === 'cash') return 'Cash';
        if (p === 'card') return 'Card';
        if (p === 'bank') return 'Bank Transfer';
        return p.replace(/[_-]+/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
    }

    function orderMatchesSearch(row, q) {
        if (!q) return true;
        const hay = [
            row.order_id,
            row.order_date,
            row.total_amount,
            row.order_status,
            row.payment_method,
            orderPayLabel(row.payment_method),
            row.kind
        ].join(' ').toLowerCase();
        return hay.includes(q);
    }

    function renderCustomerOrdersTable() {
        const tbody = document.getElementById('ordersTableBody');
        if (!tbody) return;
        const q = orderSearchQuery();
        const rows = customerOrdersCache.filter((r) => orderMatchesSearch(r, q));
        const countEl = document.getElementById('moStatCount');
        const openEl = document.getElementById('moStatOpen');
        const spentEl = document.getElementById('moStatSpent');
        const amounts = rows.map(r => parseFloat(String(r.total_amount || '0').replace(/,/g, '')) || 0);
        const spent = amounts.reduce((s, n) => s + n, 0);
        const openCount = rows.filter(r => {
            const st = String(r.order_status || '').toLowerCase();
            return st.includes('pending') || st.includes('processing') || st.includes('ready');
        }).length;
        if (countEl) countEl.textContent = String(rows.length);
        if (openEl) openEl.textContent = String(openCount);
        if (spentEl) spentEl.textContent = '₱' + spent.toFixed(2);
        if (!rows.length) {
            const title = customerOrdersCache.length ? 'No matching orders' : 'No orders found';
            const hint = customerOrdersCache.length
                ? 'Try a different search term.'
                : 'Try another date range, or switch between Online and Walk-in.';
            tbody.innerHTML = `<tr><td colspan="6" class="mo-empty-cell">
                <i class="fas fa-bag-shopping"></i>
                <h3>${title}</h3>
                <p>${hint}</p>
            </td></tr>`;
            return;
        }
        tbody.innerHTML = rows.map(row => {
            const cls = String(row.order_status || '').toLowerCase().replace(/\s+/g, '-');
            const pay = orderPayLabel(row.payment_method || (row.kind === 'walkin' ? 'cash' : 'cash'));
            const kind = row.kind === 'walkin' ? 'walkin' : 'online';
            return `<tr>
                <td><strong>#${row.order_id}</strong></td>
                <td>${row.order_date || '-'}</td>
                <td><span class="mo-pay">${pay}</span></td>
                <td><span class="status ${cls}">${row.order_status || '-'}</span></td>
                <td class="mo-amt-cell">₱${row.total_amount}</td>
                <td><button type="button" class="mo-view" onclick="showOrderDetails(${row.order_id}, '${kind}')"><i class="fas fa-eye"></i> View</button></td>
            </tr>`;
        }).join('');
    }


    // =======================================================
    // ⭐ BAGONG FUNCTION: I-fetch at Ipakita ang Order Details
    // =======================================================

    // Ginawang global function (window.) para magamit ng 'onclick' sa PHP file.
    window.showOrderDetails = function(orderId, kind = 'online') {
        if (!orderDetailsModal || !orderDetailsContent) return;

        orderDetailsContent.innerHTML = '<p style="text-align:center;">Loading order details... ⏳</p>';
        orderDetailsModal.style.display = 'flex';

        const kindSafe = kind === 'walkin' ? 'walkin' : 'online';
        fetch(`/api/customer/orders/${orderId}?kind=${encodeURIComponent(kindSafe)}`, { credentials: 'same-origin' }) 
            .then(response => {
                if (!response.ok) {
                    throw new Error(`HTTP error! status: ${response.status}`);
                }
                return response.json();
            })
            .then(data => {
                if (data.success) {
                    displayOrderDetails(data); // Tumawag sa helper function
                } else {
                    orderDetailsContent.innerHTML = `<p style="text-align:center; color:red;">Error: ${data.message}</p>`;
                }
            })
            .catch(error => {
                console.error('Error fetching order details:', error);
                orderDetailsContent.innerHTML = `<p style="text-align:center; color:red;">Failed to load order details: ${error.message}</p>`;
            });
    }

    function loadContent(sectionId) {
        // Reuse the real nav-item click handler so this stays in sync with
        // however navigation actually works (section .active class), instead
        // of toggling a '.page' class that doesn't exist in this portal.
        const navLink = document.querySelector(`.nav-item[data-target="${sectionId}"]`);
        if (navLink) {
            navLink.click();
            return;
        }

        const targetSection = document.getElementById(sectionId);
        if (targetSection) {
            sections.forEach(sec => sec.classList.remove('active'));
            targetSection.classList.add('active');
            targetSection.scrollIntoView({ behavior: 'smooth' });
        } else {
            console.error('Target section not found with ID:', sectionId);
        }
    }
    // Exposed globally because it's called from inline onclick="" handlers
    // rendered by PHP (e.g. the Home page's "Shop Now" / "View Orders" buttons).
    window.loadContent = loadContent;

    // ⭐ BAGONG FUNCTION: I-render ang Order Details sa Modal
function displayOrderDetails(data) {
    let itemsHtml = data.items.map(item => {
        // ⭐ BAGONG LOGIC DITO: Gumawa ng mas kumpletong string.
        const brandName = item.brand_name ? item.brand_name.trim() : '';
        const genericInfo = (item.generic_name && item.dosage) 
            ? `${item.generic_name.trim()} (${item.dosage.trim()})` 
            : (item.generic_name ? item.generic_name.trim() : 'N/A');

        // Pagsamahin: (Brand Name) [Generic Name (Dosage)]
        let itemName;
        if (brandName && brandName !== genericInfo) {
            itemName = `${brandName} / ${genericInfo}`;
        } else {
            itemName = genericInfo;
        }

        const itemTotal = item.ordered_qty * item.price_per_unit;
        
        // ... (Ang natitirang HTML ay unchanged)
    
        return `
            <tr>
                <td>${item.ordered_qty}× <strong>${itemName}</strong></td>
                <td style="text-align:right;">₱${itemTotal.toFixed(2)}</td>
            </tr>
        `;
    }).join('');

        const calculatedTotal = data.items.reduce((sum, item) => sum + (item.ordered_qty * item.price_per_unit), 0);
        const statusColor = getStatusColor(data.status);

        orderDetailsContent.innerHTML = `
            <div class="mo-detail-head">
                <h4>Order #${data.order_id}</h4>
                <p>${data.customer_name || ''}</p>
            </div>
            <div class="mo-detail-row"><span>Status</span><strong style="color:${statusColor};">${data.status}</strong></div>
            <table class="mo-detail-items">
                <thead>
                    <tr>
                        <th>Item</th>
                        <th style="text-align:right;">Amount</th>
                    </tr>
                </thead>
                <tbody>${itemsHtml}</tbody>
            </table>
            <div class="mo-detail-total">
                <span>Total</span>
                <span>₱${calculatedTotal.toFixed(2)}</span>
            </div>
            <p class="mo-detail-note">${data.kind === 'walkin' ? 'Paid at the cashier (walk-in sale).' : 'Payment and change will be finalized upon pickup.'}</p>
        `;
    }

    // Helper function para sa kulay ng status
    function getStatusColor(status) {
        status = status.toLowerCase();
        if (status.includes('pending')) return '#ffc107';
        if (status.includes('processing')) return '#17a2b8';
        if (status.includes('ready')) return '#28a745';
        if (status.includes('completed')) return '#0069d9';
        if (status.includes('canceled')) return '#dc3545';
        return '#6c757d';
    }
    
    // =======================================================
    // 3. CART MANIPULATION & STOCK LOGIC (UNCHANGED)
    // =======================================================

    function addToCart(button) {
        if (window.CUSTOMER_GUEST) {
            const msg = 'Log in to add items to your cart.';
            if (typeof window.phAlert === 'function') window.phAlert(msg);
            else alert(msg);
            window.location.href = '/';
            return;
        }
        const lotId = button.getAttribute('data-lot-id');
        const name = button.getAttribute('data-name');
        const price = parseFloat(button.getAttribute('data-price'));
        const drugId = button.getAttribute('data-drug-id');
        const maxStock = parseInt(button.getAttribute('data-stock'));
        let currentInCart = cart[lotId] ? cart[lotId].qty : 0;

        if (currentInCart >= maxStock) {
             alert(`❌ You have reached the maximum available stock (${maxStock}) for ${name}.`);
             return;
        }
        if(cart[lotId]) {
            cart[lotId].qty += 1;
        } else {
            cart[lotId] = { name: name, price: price, qty: 1, drug_id: drugId, max_stock: maxStock, lot_id: lotId };
        }
        updateCartPanel();
    }

    window.addRxMatchToCart = function (item) {
        if (!item || !item.lot_id) {
            const msg = 'This medicine is out of stock.';
            if (typeof window.phAlert === 'function') window.phAlert(msg);
            else alert(msg);
            return false;
        }
        const lotId = String(item.lot_id);
        const before = cart[lotId] ? cart[lotId].qty : 0;
        const btn = document.createElement('button');
        btn.setAttribute('data-lot-id', lotId);
        btn.setAttribute('data-name', String(item.name || ''));
        btn.setAttribute('data-price', String(item.price || 0));
        btn.setAttribute('data-drug-id', String(item.drug_id || ''));
        btn.setAttribute('data-stock', String(item.stock || 0));
        addToCart(btn);
        const after = cart[lotId] ? cart[lotId].qty : 0;
        return after > before;
    };

    window.updateCartQty = function(inputElement, delta = 0) {
        const lotId = inputElement.dataset.lotId;
        const item = cart[lotId];
        if (!item) { updateCartPanel(); return; }

        let newQty = (delta !== 0) ? item.qty + delta : parseInt(inputElement.value);
        if (isNaN(newQty) || newQty < 1) newQty = 1;
        if (newQty > item.max_stock) {
            alert("⚠️ Cannot exceed max stock of " + item.max_stock);
            newQty = item.max_stock;
        }
        if (newQty === 0) { removeItem(lotId); return; }

        item.qty = newQty;
        inputElement.value = newQty;
        updateCartPanel();
    };

    window.removeItem = function(lotId) {
        const addBtn = document.querySelector(`.add-btn[data-lot-id="${lotId}"]`);
        if (addBtn) { addBtn.disabled = false; addBtn.style.opacity = 1; }
        delete cart[lotId];
        updateCartPanel();
    }

    function clearCart() {
        if (Object.keys(cart).length === 0) { alert("Cart is already empty."); return; }
        for (let lotId in cart) {
            const addBtn = document.querySelector(`.add-btn[data-lot-id="${lotId}"]`);
            if (addBtn) { addBtn.disabled = false; addBtn.style.opacity = 1; }
            delete cart[lotId];
        }
        updateCartPanel();
        alert("Cart has been cleared.");
    }

    function updateCartPanel() {
        cartItemsTableBody.innerHTML = '';
        let totalItems = 0;
        let finalTotal = 0;

        if (Object.keys(cart).length === 0) {
            emptyCartMessage.style.display = 'block';
        } else {
            emptyCartMessage.style.display = 'none';
        }

        for (let lotId in cart) {
            const item = cart[lotId];
            const itemTotal = item.price * item.qty;
            finalTotal += itemTotal;
            totalItems += item.qty;

            const tr = document.createElement('tr');
            tr.innerHTML = `
                 <td class="cart-item-name">
                    ${item.name}
                    <span class="cart-item-price">₱${item.price.toFixed(2)} each</span>
                </td>
                <td>
                    <div class="qty-wrapper">
                        <button class="qty-control-btn qty-minus" onclick="updateCartQty(this.closest('tr').querySelector('.qty-input'), -1)">−</button>
                        <input type="number" class="qty-input" min="1" value="${item.qty}" data-lot-id="${lotId}" onchange="updateCartQty(this)">
                        <button class="qty-control-btn qty-plus" onclick="updateCartQty(this.closest('tr').querySelector('.qty-input'), 1)" ${item.qty >= item.max_stock ? 'disabled style="opacity:0.5;"' : ''}>+</button>
                    </div>
                </td>
                <td class="cart-item-total">₱${itemTotal.toFixed(2)}</td>
                <td>
                    <button class="cart-remove-btn" onclick="removeItem('${lotId}')">&times;</button>
                </td>
            `;

            cartItemsTableBody.appendChild(tr);

            const addBtn = document.querySelector(`.add-btn[data-lot-id="${lotId}"]`);
            if (addBtn) {
                addBtn.disabled = (item.qty >= item.max_stock);
                addBtn.style.opacity = (item.qty >= item.max_stock) ? 0.5 : 1;
            }
        }

        const subtotalValue = finalTotal;
        if (subtotalDisplay) subtotalDisplay.innerText = subtotalValue.toFixed(2);
        if (totalPriceSpan) totalPriceSpan.innerText = finalTotal.toFixed(2);

        if (checkoutBtn) {
            checkoutBtn.disabled = totalItems === 0;
            checkoutBtn.innerText = totalItems > 0 ? `Submit Order for Pickup (₱${finalTotal.toFixed(2)})` : 'Submit Order for Pickup';
        }

        return { finalTotal };
    }


    /// =======================================================
    // 4. ORDER SUBMISSION (FIXED for Timeout Errors)
    // =======================================================

    function submitOrder() {
        if (window.CUSTOMER_GUEST) {
            const msg = 'Log in to place a pickup order.';
            if (typeof window.phAlert === 'function') window.phAlert(msg);
            else alert(msg);
            window.location.href = '/';
            return;
        }
        const { finalTotal } = updateCartPanel();
        const totalItems = Object.keys(cart).reduce((sum, key) => sum + cart[key].qty, 0);

        if (totalItems === 0) {
            alert('Your cart is empty!');
            return;
        }

        const orderData = {
            customer_id: selectedCustomer,
            total_amount: finalTotal,
            sc_pwd_applied: false,
            // Ginagamit ang GLOBAL_UNIQUE_TOKEN_FROM_PHP_SESSION
            order_token: typeof GLOBAL_UNIQUE_TOKEN_FROM_PHP_SESSION !== 'undefined' ? GLOBAL_UNIQUE_TOKEN_FROM_PHP_SESSION : 'no_token',
            payment_method: document.getElementById('checkoutPaymentMethod')?.value || 'cash',
            items: Object.values(cart).map(item => ({
                lot_id: item.lot_id,
                drug_id: item.drug_id,
                name: item.name, 
                quantity: item.qty,
                price_per_unit: item.price
            }))
        };

        if (checkoutBtn) {
            checkoutBtn.disabled = true;
            checkoutBtn.innerText = 'Processing...';
        }

        fetch('/api/customer/orders', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify(orderData),
        })
        .then(response => {
            // Re-enable button early to prevent it from staying disabled if .json() fails
            if (checkoutBtn) checkoutBtn.disabled = false;
            
            if (response.status === 409) {
                 return response.json(); 
            }
            
            if (!response.ok) {
                // Try to read the real error out of the response body (JSON
                // message, or a PHP fatal-error HTML page) instead of just
                // throwing a generic status code - this is what used to get
                // swallowed and reported to the customer as a fake "network
                // error", hiding the actual server-side problem.
                return response.text().then(text => {
                    let serverMessage = null;
                    try {
                        const parsed = JSON.parse(text);
                        serverMessage = parsed.message || null;
                    } catch (e) {
                        // Not JSON - likely a raw PHP fatal error/warning page.
                        const stripped = text.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
                        if (stripped) serverMessage = stripped.substring(0, 300);
                    }
                    const err = new Error(serverMessage || `HTTP error! status: ${response.status}`);
                    err.isServerError = true;
                    throw err;
                });
            }
            
            return response.json();
        })
        .then(data => {
            if (data.success) {
                // SUCCESS PATH
                showReceiptModal(data.order_id, finalTotal, orderData.items);
                loadCustomerOrders(); // Force reload ng orders tab
                loadHomeStats(); // Reflect the new order/spend total immediately on Home
                if (data.order_token) {
                    window.GLOBAL_UNIQUE_TOKEN_FROM_PHP_SESSION = data.order_token;
                }
                refreshProductStock();
                
                // Clear the cart
                for (let lotId in cart) delete cart[lotId];
                updateCartPanel();

            } else {
                // FAILURE PATH
                alert(`❌ Order submission failed: ${data.message}`);
                console.error(data);
            }
        })
        .catch(error => {
            // CRITICAL FIX: a network error/timeout here does NOT mean the
            // order was saved. The previous version of this code assumed
            // success, deleted every item from `cart`, and showed a fake
            // receipt - that's why items appeared to vanish right after
            // checkout even though nothing had actually been ordered.
            //
            // The cart is now left untouched on failure. The customer can
            // safely click "Submit Order for Pickup" again: process_customer_order.php
            // is idempotent per order_token, so even if the first request
            // actually did reach the server, retrying will NOT create a
            // duplicate order or double-deduct stock - it just returns the
            // original order.
            console.error('Checkout error:', error);
            if (error && error.isServerError) {
                // The server responded, just with an error - show that real
                // message instead of the misleading "check your connection"
                // text, which used to hide actual server-side bugs.
                alert('❌ Order could not be placed: ' + error.message);
            } else {
                // fetch() itself rejected - this really is a network-level
                // failure (offline, DNS, CORS, etc.), so the connection
                // message is accurate here.
                alert('⚠️ Could not reach the server to place your order. Your cart has been kept - please check your connection and try again.');
            }
        })
        .finally(() => {
            if (checkoutBtn) {
                checkoutBtn.disabled = false;
                checkoutBtn.innerText = 'Submit Order for Pickup';
            }
            updateCartPanel();
        });
    }

    function showReceiptModal(orderId, finalTotal, items) {
        
        if (!receiptContent || !receiptModal) {
            console.error("receiptModal or receiptContent element is missing.");
            return;
        }

        receiptContent.innerHTML = `
            <h5 style="color:#007bff; text-align: center;">🧾 Order Confirmation</h5>
            <p style="text-align: center; margin-bottom: 20px;">
                <strong>Order ID:</strong> #${orderId}<br>
                <strong>Date:</strong> ${new Date().toLocaleDateString()} ${new Date().toLocaleTimeString()}
            </p>
            <hr style="border-top: 1px dashed #ccc;">
            
            <h5 style="margin-top: 15px; font-weight: bold;">Order Details:</h5>
            
            <table style="width: 100%; border-collapse: collapse; font-size: 0.9em; margin-bottom: 15px;">
                <thead>
                    <tr style="border-bottom: 2px solid #ddd;">
                        <th style="padding: 5px 0; text-align: left;">Item</th>
                        <th style="padding: 5px 0; text-align: right;">Total</th>
                    </tr>
                </thead>
                <tbody>
                ${items.map(item => {
                    const itemName = item.name; 
                    const itemTotalPrice = item.quantity * item.price_per_unit;
                    return `
                        <tr>
                            <td style="padding: 2px 0; width: 70%;">${item.quantity}x ${itemName}</td>
                            <td style="padding: 2px 0; width: 30%; text-align: right;">₱${itemTotalPrice.toFixed(2)}</td>
                        </tr>
                    `;
                }).join('')}
                </tbody>
            </table>
            
            <div style="margin-top: 20px; border-top: 1px dashed #ccc; padding-top: 10px;">
                <p style="display: flex; justify-content: space-between; font-size: 1.1em; color: #333;">
                    Subtotal: <span>₱${finalTotal.toFixed(2)}</span>
                </p>
                <h5 style="display: flex; justify-content: space-between; font-size: 1.3em; color:#28a745; margin: 5px 0 0;">
                    TOTAL AMOUNT: <span>₱${finalTotal.toFixed(2)}</span>
                </h5>
            </div>
            
            <hr style="border-top: 1px dashed #ccc; margin-top: 20px;">
            
            <p style="margin-top: 15px; font-weight: bold; color: orange; text-align: center;">Current Status: Pending ⏱️</p>
            <p style="font-size: 0.9em; text-align: center;">We will notify you when your order is **'Ready for Pickup'**.</p>
        `;
        
        receiptModal.style.display = 'flex';
    }


    // =======================================================
    // ⭐ BAGONG FUNCTION: Setup Filter Listener
    // =======================================================
    function setupOrderFilterListener() {
        const applyBtn = document.getElementById('apply_order_filter_btn');
        const startDateInput = document.getElementById('order_start_date');
        const endDateInput = document.getElementById('order_end_date');
        const searchInputEl = document.getElementById('order_search');

        function reloadOrders() {
            const start = startDateInput?.value || '';
            const end = endDateInput?.value || '';
            const type = document.querySelector('.order-type-tab.active')?.dataset.type
                || document.querySelector('.order-type-tab')?.dataset.type
                || 'online';
            window.loadCustomerOrders(type, start, end);
        }

        if (applyBtn && !applyBtn.dataset.bound) {
            applyBtn.dataset.bound = '1';
            applyBtn.addEventListener('click', reloadOrders);
        }
        if (searchInputEl && !searchInputEl.dataset.bound) {
            searchInputEl.dataset.bound = '1';
            searchInputEl.addEventListener('input', renderCustomerOrdersTable);
            searchInputEl.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    renderCustomerOrdersTable();
                }
            });
        }
    }

    // =======================================================
    // 5. EVENT LISTENERS & FILTERS (UNCHANGED)
    // =======================================================
    function applyProductFilters() {
        if (!searchInput || !categoryFilter || !productGrid) return;
        const search = searchInput.value.toLowerCase().trim();
        const selectedCategory = categoryFilter.value;
        const products = productGrid.querySelectorAll('.product');

        products.forEach(product => {
            const productCategory = product.getAttribute('data-category') || '';
            const productNameSearch = product.getAttribute('data-name-search') ? product.getAttribute('data-name-search').toLowerCase() : '';

            const matchesCategory = selectedCategory === '' || productCategory === selectedCategory;
            const matchesSearch = search === '' || productNameSearch.includes(search);

            if (matchesSearch && matchesCategory) {
                product.style.display = 'block';
            } else {
                product.style.display = 'none';
            }
        });
    }

    if (productGrid) {
        productGrid.addEventListener('click', function(e) {
            const addButton = e.target.closest('.add-btn');
            if (addButton) {
                e.preventDefault();
                addToCart(addButton);
            }
        });
    }

    if (checkoutBtn) checkoutBtn.addEventListener('click', submitOrder);
    if (clearCartBtn) clearCartBtn.addEventListener('click', clearCart);

    // Initial Load
    updateCartPanel();
    loadHomeStats();

    // FILE: customer.js (Sa dulo ng DOMContentLoaded block)

// =======================================================
// ⭐ 6. NOTIFICATION POLLING LOGIC ⭐
// =======================================================

// Counter para sa notification badge
let notificationCount = 0;

function updateNotificationBadge(count) {
    if (!notificationBell) return;

    notificationCount = count || 0;
    notificationBell.classList.toggle('has-unread', notificationCount > 0);

    let badge = notificationBell.querySelector('.notification-badge');
    if (notificationCount > 0) {
        if (!badge) {
            badge = document.createElement('span');
            badge.className = 'notification-badge';
            notificationBell.appendChild(badge);
        }
        badge.textContent = notificationCount > 9 ? '9+' : notificationCount;
    } else if (badge) {
        badge.remove();
    }
}

function renderNotificationDropdown(notifications) {
    if (!notificationDropdown || !notificationDropdown.classList.contains('open')) return;

    if (!notifications || notifications.length === 0) {
        notificationDropdown.innerHTML = '<div class="staff-notif-empty">No new alerts. You are all caught up!</div>';
        return;
    }

    notificationDropdown.innerHTML = notifications.map(notif => `
        <div class="staff-notif-item order_update"
             data-order-id="${notif.order_id}"
             role="button"
             tabindex="0"
             style="cursor:pointer;">
            <i class="fas fa-receipt"></i>
            <span>${notif.message}<br><small style="color:#9ca3af;">${notif.date}</small></span>
        </div>
    `).join('');

    notificationDropdown.querySelectorAll('.staff-notif-item[data-order-id]').forEach(item => {
        item.addEventListener('click', (e) => {
            e.stopPropagation();
            const orderId = item.dataset.orderId;
            window.markNotificationRead(orderId);
            window.showOrderDetails(orderId);
            notificationDropdown.classList.remove('open');
        });

        item.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                item.click();
            }
        });
    });
}

function checkNotifications() {
    if (selectedCustomer === null || !notificationBell || !notificationDropdown) return;

    fetch('/api/customer/notifications', { credentials: 'same-origin' })
        .then(response => {
            if (!response.ok) throw new Error('Failed to fetch notifications.');
            return response.json();
        })
        .then(data => {
            updateNotificationBadge(data.count || 0);
            renderNotificationDropdown(data.notifications || []);
        })
        .catch(error => {
            console.error('Notification Polling Error:', error);
        });
}

window.markNotificationRead = function(orderId) {
    fetch(`/api/customer/notifications/read?order_id=${orderId}`, { method: 'POST', credentials: 'same-origin' })
        .then(response => response.json())
        .then(data => {
            if (data && data.success) {
                notificationCount = Math.max(0, notificationCount - 1);
                updateNotificationBadge(notificationCount);
                checkNotifications();
            }
        })
        .catch(error => {
            console.error('Failed to mark as read:', error);
        });
};

if (notificationBell && notificationDropdown) {
    notificationDropdown.addEventListener('click', (e) => e.stopPropagation());

    notificationBell.addEventListener('click', (e) => {
        e.stopPropagation();
        notificationDropdown.classList.toggle('open');
        if (notificationDropdown.classList.contains('open')) {
            notificationDropdown.innerHTML = '<div class="staff-notif-empty">Loading...</div>';
            checkNotifications();
        }
    });

    document.addEventListener('click', (e) => {
        if (!notificationBell.contains(e.target) && !notificationDropdown.contains(e.target)) {
            notificationDropdown.classList.remove('open');
        }
    });

    setInterval(checkNotifications, 30000);
    checkNotifications();
}

// =======================================================
// LIVE CLOCK ("Server Date & Time" widget, same as Admin/Cashier)
// =======================================================
const liveClockEl = document.getElementById('live-clock');
if (liveClockEl) {
    const tickClock = () => {
        const now = new Date();
        liveClockEl.textContent = now.toLocaleString('en-PH', {
            weekday: 'long', year: 'numeric', month: 'short', day: 'numeric',
            hour: 'numeric', minute: '2-digit', second: '2-digit', hour12: true
        });
    };
    tickClock();
    setInterval(tickClock, 1000);
}
});