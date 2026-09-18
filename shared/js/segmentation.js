(function () {
    const SEGMENT_COLORS = ['#3b5b92', '#3f8f7a', '#c08a3e', '#7a6aa8', '#b25c5c'];
    if (!window.CHART_PALETTE) {
        window.CHART_PALETTE = ['#4BAA8B', '#4BAA8B', '#FFC857', '#FFC857', '#FFC857', '#4BAA8B'];
    }
    if (!window.chartBarGradient) {
        window.chartBarGradient = function (ctx, color) {
            color = color || window.CHART_PALETTE[0];
            const chartArea = ctx.chart && ctx.chart.chartArea;
            if (!chartArea) return color;
            const g = ctx.chart.ctx.createLinearGradient(chartArea.left, 0, chartArea.right, 0);
            g.addColorStop(0, color + 'cc');
            g.addColorStop(1, color);
            return g;
        };
    }

    const shadowPlugin = {
        id: 'softShadow',
        beforeDatasetsDraw(chart) {
            const ctx = chart.ctx;
            ctx.save();
            ctx.shadowColor = 'rgba(15, 23, 42, 0.22)';
            ctx.shadowBlur = 14;
            ctx.shadowOffsetX = 0;
            ctx.shadowOffsetY = 6;
        },
        afterDatasetsDraw(chart) {
            chart.ctx.restore();
        }
    };

    function canManage() {
        return window.SEGMENTATION_MANAGE === true;
    }

    let kmeansPieChart = null;
    let segmentationChartsReady = false;
    let segmentVariants = {};
    let segmentEngine = 'quantile';
    let segmentSilhouette = {};
    let segmentDebug = {};
    let segmentationDataLoaded = false;
    let segmentationDataLoading = false;
    let currentSegmentVariant = null;
    let chartsPayloadLoaded = false;
    let activeK = 3;

    function money(n) {
        return Number(n || 0).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }

    function fillSegmentationKpis(charts) {
        const spend = charts.spend_data || [];
        const types = charts.type_labels || [];
        const typeCounts = charts.type_data || [];
        const total = (charts.spend_labels || []).length;
        const active = spend.filter((amt) => Number(amt) > 0).length;
        const revenue = spend.reduce((sum, amt) => sum + Number(amt || 0), 0);
        const avg = active ? revenue / active : 0;
        let topType = '—';
        if (typeCounts.length) {
            let maxIdx = 0;
            typeCounts.forEach((count, idx) => {
                if (Number(count) > Number(typeCounts[maxIdx])) maxIdx = idx;
            });
            topType = types[maxIdx] || '—';
        }
        const setText = (id, value) => {
            const el = document.getElementById(id);
            if (el) el.textContent = value;
        };
        setText('segTotalCustomers', String(total));
        setText('segActiveCustomers', String(active));
        setText('segAvgSpend', '₱' + money(avg));
        setText('segTopType', topType);
    }

    function applyChartsToWindows(charts) {
        window.CASHIER_FREQ_LABELS = charts.freq_labels || [];
        window.CASHIER_FREQ_DATA = charts.freq_data || [];
        window.CASHIER_SPEND_LABELS = charts.spend_labels || [];
        window.CASHIER_SPEND_DATA = charts.spend_data || [];
        window.CASHIER_TYPE_LABELS = charts.type_labels || [];
        window.CASHIER_TYPE_DATA = charts.type_data || [];
        fillSegmentationKpis(charts);
    }

    function refreshChart(chart, labels, data) {
        if (!chart || !chart.data) return;
        chart.data.labels = labels;
        if (chart.data.datasets && chart.data.datasets[0]) chart.data.datasets[0].data = data;
        chart.update();
    }

    function loadChartPayload() {
        if (chartsPayloadLoaded && (window.CASHIER_TYPE_DATA || []).length) {
            return Promise.resolve();
        }
        return fetch('/api/cashier/charts', { credentials: 'same-origin' })
            .then((res) => res.json())
            .then((charts) => {
                applyChartsToWindows(charts);
                chartsPayloadLoaded = true;
                refreshChart(window.purchaseFrequencyChartInstance, window.CASHIER_FREQ_LABELS, window.CASHIER_FREQ_DATA);
                refreshChart(window.spendingTierChartInstance, window.CASHIER_SPEND_LABELS, window.CASHIER_SPEND_DATA);
                refreshChart(window.customerTypeChartInstance, window.CASHIER_TYPE_LABELS, window.CASHIER_TYPE_DATA);
            })
            .catch((err) => console.error('[PharmaLink] Failed to load segmentation charts', err));
    }

    function loadSegmentationData() {
        if (segmentationDataLoaded) return Promise.resolve();
        if (segmentationDataLoading) {
            return new Promise((resolve) => {
                const t = setInterval(() => {
                    if (!segmentationDataLoading) {
                        clearInterval(t);
                        resolve();
                    }
                }, 50);
            });
        }
        segmentationDataLoading = true;
        const badge = document.getElementById('segmentEngineBadge');
        if (badge) {
            badge.innerHTML = canManage()
                ? '<i class="fas fa-spinner fa-spin"></i> Running K-Means clustering...'
                : '<i class="fas fa-spinner fa-spin"></i> Loading Admin segment snapshot...';
        }

        return fetch('/api/cashier/segmentation', { credentials: 'same-origin' })
            .then((res) => res.json())
            .then((data) => {
            segmentVariants = data.variants || {};
            segmentEngine = data.engine || 'quantile';
            segmentSilhouette = data.silhouette || {};
            segmentDebug = data.debug || {};
            const k = parseInt(data.active_k, 10);
            activeK = [2, 3, 4, 5].includes(k) ? k : 3;
            const segmentSelect = document.getElementById('segmentCountTop');
            if (segmentSelect) segmentSelect.value = String(activeK);
            segmentationDataLoaded = true;
                if (segmentEngine !== 'kmeans' && Object.keys(segmentDebug).length > 0) {
                    console.warn('[PharmaLink] K-Means fell back to quantile grouping. Reasons:', segmentDebug);
                }
            })
            .catch((err) => {
                console.error('[PharmaLink] Failed to load segmentation data:', err);
                if (badge) badge.innerHTML = '<i class="fas fa-triangle-exclamation"></i> Failed to load segmentation data.';
            })
            .finally(() => { segmentationDataLoading = false; });
    }

    function renderSegmentLegend(variant) {
        const el = document.getElementById('segmentLegend');
        if (!el) return;
        el.innerHTML = (variant.labels || []).map((label, i) => `
            <div class="cs-seg-legend-item">
                <span class="cs-seg-dot" style="background:${SEGMENT_COLORS[i % SEGMENT_COLORS.length]}"></span>
                ${label}
            </div>
        `).join('');
    }

    function renderSegmentTable(variant) {
        const body = document.getElementById('segmentTableBody');
        if (!body) return;
        const actionLabel = canManage() ? 'View / Points' : 'View';
        body.innerHTML = (variant.labels || []).map((label, i) => `
            <tr>
                <td>
                    <span class="cs-seg-dot" style="background:${SEGMENT_COLORS[i % SEGMENT_COLORS.length]}"></span>${label}
                </td>
                <td>${variant.counts[i]}</td>
                <td>₱${money(variant.avgSpend[i])}</td>
                <td>
                    <button type="button" class="btn-view-segment" data-segment-index="${i}"><i class="fas fa-eye"></i> ${actionLabel}</button>
                </td>
            </tr>
        `).join('');
        body.querySelectorAll('.btn-view-segment').forEach((btn) => {
            btn.onclick = () => showSegmentMembers(parseInt(btn.dataset.segmentIndex, 10));
        });
    }

    function renderSegmentEngineBadge(n) {
        const el = document.getElementById('segmentEngineBadge');
        if (!el) return;
        if (segmentEngine === 'kmeans') {
            const score = segmentSilhouette[n];
            const scoreText = (score !== undefined && score !== null) ? ` &middot; Silhouette Score: <strong>${score}</strong>` : '';
            el.innerHTML = `<i class="fas fa-brain" style="color:#4BAA8B;"></i> K-Means Clustering (scikit-learn)${scoreText}`;
        } else {
            el.innerHTML = `<i class="fas fa-info-circle"></i> Quantile-based grouping (fallback — K-Means unavailable on this server)`;
        }
    }

    function applySegmentCount(n) {
        const variant = segmentVariants[n];
        if (!variant || !kmeansPieChart) return;
        currentSegmentVariant = variant;
        kmeansPieChart.data.labels = variant.labels;
        kmeansPieChart.data.datasets[0].data = variant.counts;
        kmeansPieChart.update();
        renderSegmentLegend(variant);
        renderSegmentTable(variant);
        renderSegmentEngineBadge(n);
        closeAddPointsModal();
        closeMembersModal();
    }

    function ensureModals() {
        if (!document.getElementById('addPointsModalBackdrop')) {
            const wrap = document.createElement('div');
            wrap.innerHTML = `
<div id="addPointsModalBackdrop" class="cs-seg-modal" style="display:none;">
    <div class="cs-seg-modal-card">
      <div class="cs-seg-modal-head">
        <h3>Distribute Points</h3>
        <button type="button" id="addPointsCancelBtn" aria-label="Close">✕</button>
      </div>
      <p class="cs-seg-modal-sub">Segment: <strong id="addPointsSegmentLabel">—</strong></p>
      <label for="addPointsCustomerSelect">Customer</label>
      <select id="addPointsCustomerSelect"></select>
      <label for="addPointsAmountInput">Points to add</label>
      <input type="number" id="addPointsAmountInput" placeholder="e.g. 50">
      <p id="addPointsModalMessage"></p>
      <div class="cs-seg-modal-actions">
        <button type="button" id="addPointsCancelBtn2"><i class="fas fa-xmark"></i> Close</button>
        <button type="button" id="addPointsConfirmBtn"><i class="fas fa-plus"></i> Add Points</button>
      </div>
    </div>
</div>`;
            document.body.appendChild(wrap.firstElementChild);
        }
        if (!document.getElementById('segMembersModalBackdrop')) {
            const wrap = document.createElement('div');
            wrap.innerHTML = `
<div id="segMembersModalBackdrop" class="cs-seg-modal" style="display:none;">
    <div class="cs-seg-modal-card">
      <div class="cs-seg-modal-head">
        <h3>Segment members</h3>
        <button type="button" id="segMembersCloseBtn" aria-label="Close">✕</button>
      </div>
      <p class="cs-seg-modal-sub">Segment: <strong id="segMembersSegmentLabel">—</strong></p>
      <div class="cs-seg-members-wrap">
        <table class="cs-seg-members-table">
          <thead>
            <tr><th>Customer</th><th>Spend</th><th>Points</th></tr>
          </thead>
          <tbody id="segMembersTableBody"></tbody>
        </table>
      </div>
      <div class="cs-seg-modal-actions">
        <button type="button" id="segMembersCloseBtn2"><i class="fas fa-xmark"></i> Close</button>
      </div>
    </div>
</div>`;
            document.body.appendChild(wrap.firstElementChild);
        }
    }

    function populateAddPointsCustomerOptions(members) {
        const select = document.getElementById('addPointsCustomerSelect');
        if (!select) return;
        if (!members.length) {
            select.innerHTML = `<option value="">No customers in this segment</option>`;
            select.disabled = true;
            return;
        }
        select.disabled = false;
        select.innerHTML = members.map((m) =>
            `<option value="${m.customer_id}" data-points="${m.loyalty_points}">${m.name} — ${Number(m.loyalty_points).toLocaleString('en-US', { maximumFractionDigits: 2 })} pts</option>`
        ).join('');
    }

    function showSegmentMembers(segmentIndex) {
        if (!currentSegmentVariant) return;
        ensureModals();
        const label = currentSegmentVariant.labels[segmentIndex];
        const members = (currentSegmentVariant.members && currentSegmentVariant.members[segmentIndex]) || [];
        if (canManage()) {
            document.getElementById('addPointsSegmentLabel').textContent = label;
            populateAddPointsCustomerOptions(members);
            document.getElementById('addPointsAmountInput').value = '';
            document.getElementById('addPointsModalMessage').textContent = '';
            document.getElementById('addPointsModalBackdrop').style.display = 'flex';
            const select = document.getElementById('addPointsCustomerSelect');
            if (!select.disabled) select.focus();
            return;
        }
        document.getElementById('segMembersSegmentLabel').textContent = label;
        const body = document.getElementById('segMembersTableBody');
        if (!members.length) {
            body.innerHTML = '<tr><td colspan="3" style="text-align:center;color:#6b7280;">No customers in this segment.</td></tr>';
        } else {
            body.innerHTML = members.map((m) => `
                <tr>
                    <td>${m.name}</td>
                    <td>₱${money(m.total_spent)}</td>
                    <td>${Number(m.loyalty_points || 0).toLocaleString('en-US', { maximumFractionDigits: 2 })}</td>
                </tr>
            `).join('');
        }
        document.getElementById('segMembersModalBackdrop').style.display = 'flex';
    }

    function closeAddPointsModal() {
        const el = document.getElementById('addPointsModalBackdrop');
        if (el) el.style.display = 'none';
    }

    function closeMembersModal() {
        const el = document.getElementById('segMembersModalBackdrop');
        if (el) el.style.display = 'none';
    }

    function submitAddPoints(confirmBtn) {
        const select = document.getElementById('addPointsCustomerSelect');
        const customerId = select.value;
        const modalMsg = document.getElementById('addPointsModalMessage');
        const points = parseFloat(document.getElementById('addPointsAmountInput').value);

        if (!customerId) {
            modalMsg.style.color = '#dc2626';
            modalMsg.textContent = 'Select a customer first.';
            return;
        }
        if (isNaN(points) || points === 0) {
            modalMsg.style.color = '#dc2626';
            modalMsg.textContent = 'Enter a valid points amount.';
            return;
        }

        confirmBtn.disabled = true;
        fetch('/api/cashier/loyalty-points', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify({ customer_id: customerId, points: points })
        })
            .then((res) => res.json())
            .then((data) => {
                confirmBtn.disabled = false;
                if (data.success) {
                    const customerName = select.options[select.selectedIndex].textContent.split(' — ')[0];
                    modalMsg.style.color = '#4BAA8B';
                    modalMsg.textContent = `Added ${points} points for ${customerName}. New balance: ${Number(data.new_balance).toLocaleString('en-US', { maximumFractionDigits: 2 })}.`;
                    select.options[select.selectedIndex].dataset.points = data.new_balance;
                    select.options[select.selectedIndex].textContent = `${customerName} — ${Number(data.new_balance).toLocaleString('en-US', { maximumFractionDigits: 2 })} pts`;
                    document.getElementById('addPointsAmountInput').value = '';
                    if (currentSegmentVariant && currentSegmentVariant.members) {
                        for (const bucket of currentSegmentVariant.members) {
                            const member = bucket.find((m) => String(m.customer_id) === String(customerId));
                            if (member) { member.loyalty_points = data.new_balance; break; }
                        }
                    }
                } else {
                    modalMsg.style.color = '#dc2626';
                    modalMsg.textContent = data.message || 'Failed to update points.';
                }
            })
            .catch(() => {
                confirmBtn.disabled = false;
                modalMsg.style.color = '#dc2626';
                modalMsg.textContent = 'Network error while updating points.';
            });
    }

    function initSegmentationCharts() {
        if (segmentationChartsReady) return;
        if (typeof Chart === 'undefined') return;
        segmentationChartsReady = true;

        const freqCanvas = document.getElementById('purchaseFrequencyChart');
        if (freqCanvas) {
            window.purchaseFrequencyChartInstance = new Chart(freqCanvas.getContext('2d'), {
                type: 'bar',
                data: {
                    labels: window.CASHIER_FREQ_LABELS || [],
                    datasets: [{
                        label: 'Number of Orders',
                        data: window.CASHIER_FREQ_DATA || [],
                        backgroundColor: (c) => window.chartBarGradient(c, window.CHART_PALETTE[(c.dataIndex ?? 0) % window.CHART_PALETTE.length]),
                        borderRadius: 6,
                        borderSkipped: false,
                        maxBarThickness: 26
                    }]
                },
                options: {
                    indexAxis: 'y',
                    responsive: true,
                    maintainAspectRatio: false,
                    animation: { duration: 600, easing: 'easeOutQuart' },
                    plugins: {
                        legend: { display: false },
                        tooltip: { backgroundColor: '#1E3A34', padding: 10, cornerRadius: 8 }
                    },
                    scales: {
                        x: { beginAtZero: true, ticks: { precision: 0, color: '#64748b', font: { size: 11.5 } }, grid: { color: '#eef2f7' } },
                        y: { grid: { display: false }, ticks: { color: '#4BAA8B', font: { size: 12 } } }
                    }
                }
            });
        }

        const spendCanvas = document.getElementById('spendingTierChart');
        if (spendCanvas) {
            window.spendingTierChartInstance = new Chart(spendCanvas.getContext('2d'), {
                type: 'bar',
                data: {
                    labels: window.CASHIER_SPEND_LABELS || [],
                    datasets: [{
                        label: 'Total Spending (₱)',
                        data: window.CASHIER_SPEND_DATA || [],
                        backgroundColor: (c) => window.chartBarGradient(c, window.CHART_PALETTE[(c.dataIndex ?? 0) % window.CHART_PALETTE.length]),
                        borderRadius: 6,
                        borderSkipped: false,
                        maxBarThickness: 26
                    }]
                },
                options: {
                    indexAxis: 'y',
                    responsive: true,
                    maintainAspectRatio: false,
                    animation: { duration: 600, easing: 'easeOutQuart' },
                    plugins: {
                        legend: { display: false },
                        tooltip: {
                            backgroundColor: '#1E3A34', padding: 10, cornerRadius: 8,
                            callbacks: { label: (c) => `₱${Number(c.raw).toLocaleString('en-US', { minimumFractionDigits: 2 })}` }
                        }
                    },
                    scales: {
                        x: { beginAtZero: true, ticks: { color: '#64748b', font: { size: 11.5 } }, grid: { color: '#eef2f7' } },
                        y: { grid: { display: false }, ticks: { color: '#4BAA8B', font: { size: 12 } } }
                    }
                }
            });
        }

        const typeCanvas = document.getElementById('customerTypeChart');
        if (typeCanvas) {
            window.customerTypeChartInstance = new Chart(typeCanvas.getContext('2d'), {
                type: 'doughnut',
                plugins: [shadowPlugin],
                data: {
                    labels: window.CASHIER_TYPE_LABELS || [],
                    datasets: [{
                        data: window.CASHIER_TYPE_DATA || [],
                        backgroundColor: SEGMENT_COLORS,
                        borderColor: '#fff',
                        borderWidth: 3,
                        hoverOffset: 6
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    cutout: '68%',
                    plugins: {
                        legend: { position: 'bottom', labels: { color: '#475569', font: { size: 12.5 }, boxWidth: 10, boxHeight: 10, padding: 14, usePointStyle: true, pointStyle: 'circle' } },
                        tooltip: { backgroundColor: '#1E3A34', padding: 10, cornerRadius: 8 }
                    }
                }
            });
        }

        const ctxPieEl = document.getElementById('customerSegmentationPie');
        if (ctxPieEl) {
            kmeansPieChart = new Chart(ctxPieEl.getContext('2d'), {
                type: 'doughnut',
                plugins: [shadowPlugin],
                data: { labels: [], datasets: [{ data: [], backgroundColor: SEGMENT_COLORS, borderColor: '#fff', borderWidth: 3, hoverOffset: 8 }] },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    cutout: '55%',
                    plugins: {
                        legend: { display: false },
                        tooltip: {
                            backgroundColor: '#1E3A34', padding: 10, cornerRadius: 8,
                            callbacks: {
                                label: function (context) {
                                    const total = context.dataset.data.reduce((a, b) => a + b, 0) || 1;
                                    const value = context.raw;
                                    const percent = ((value / total) * 100).toFixed(1);
                                    return `${context.label}: ${value} customers (${percent}%)`;
                                }
                            }
                        }
                    }
                }
            });
            window.kmeansPieChart = kmeansPieChart;
        }

        const segmentSelect = document.getElementById('segmentCountTop');
        if (segmentSelect && !segmentSelect.dataset.segBound) {
            segmentSelect.dataset.segBound = '1';
            segmentSelect.value = String(activeK);
            applySegmentCount(segmentSelect.value);
            segmentSelect.addEventListener('change', () => {
                if (!canManage()) {
                    segmentSelect.value = String(activeK);
                    return;
                }
                applySegmentCount(segmentSelect.value);
                saveActiveK(segmentSelect.value);
            });
        }
    }

    function saveActiveK(n) {
        const k = parseInt(n, 10);
        if (![2, 3, 4, 5].includes(k)) return;
        activeK = k;
        fetch('/api/cashier/segmentation/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify({ k: k })
        }).catch(() => {});
    }

    function resizeSegCharts() {
        if (window.kmeansPieChart) window.kmeansPieChart.resize();
        if (window.customerTypeChartInstance) window.customerTypeChartInstance.resize();
        if (window.purchaseFrequencyChartInstance) window.purchaseFrequencyChartInstance.resize();
        if (window.spendingTierChartInstance) window.spendingTierChartInstance.resize();
        applySegmentCount(String(activeK));
    }

    function paintViewOnlyChrome() {
        const sub = document.querySelector('#customer-segmentation-page .subtitle');
        if (sub) {
            sub.textContent = canManage()
                ? 'You run the grouping here (how many segments) and can distribute loyalty points. Cashiers only see the result you set.'
                : 'Read-only. These groups are set by Admin — you can view members, not change clustering or points.';
        }
        const h2 = document.querySelector('#customer-segmentation-page .dashboard-header h2');
        if (h2 && !canManage() && !h2.querySelector('.cs-seg-viewonly')) {
            const badge = document.createElement('span');
            badge.className = 'cs-seg-viewonly';
            badge.textContent = 'View only';
            h2.appendChild(badge);
        }
        const th = document.querySelector('#customer-segmentation-page .cs-seg-table thead th:last-child');
        if (th) th.textContent = canManage() ? 'Action' : 'Members';
        const wrap = document.querySelector('#customer-segmentation-page .ph-select-wrap');
        if (wrap) {
            wrap.style.display = canManage() ? '' : 'none';
            if (canManage()) wrap.title = 'Cashiers will see this number of groups.';
        }
        let lock = document.getElementById('segActiveKLabel');
        if (!canManage()) {
            if (!lock) {
                lock = document.createElement('div');
                lock.id = 'segActiveKLabel';
                lock.className = 'cs-seg-locked';
                const toolbar = document.querySelector('#customer-segmentation-page .cs-seg-toolbar');
                if (toolbar) toolbar.appendChild(lock);
            }
            lock.textContent = activeK + ' segments (set by Admin)';
        } else if (lock) {
            lock.remove();
        }
    }

    function bootSegmentation() {
        if (!document.getElementById('customer-segmentation-page')) return;
        ensureModals();
        paintViewOnlyChrome();
        Promise.all([loadChartPayload(), loadSegmentationData()]).then(() => {
            paintViewOnlyChrome();
            setTimeout(() => {
                initSegmentationCharts();
                resizeSegCharts();
            }, 20);
        });
    }

    document.addEventListener('click', function (e) {
        if (e.target.closest('#addPointsCancelBtn') || e.target.closest('#addPointsCancelBtn2')) {
            closeAddPointsModal();
            return;
        }
        if (e.target.id === 'addPointsModalBackdrop') {
            closeAddPointsModal();
            return;
        }
        if (e.target.closest('#segMembersCloseBtn') || e.target.closest('#segMembersCloseBtn2')) {
            closeMembersModal();
            return;
        }
        if (e.target.id === 'segMembersModalBackdrop') {
            closeMembersModal();
            return;
        }
        const confirmBtn = e.target.closest('#addPointsConfirmBtn');
        if (confirmBtn) submitAddPoints(confirmBtn);
    });

    window.initSegmentationCharts = initSegmentationCharts;
    window.loadSegmentationData = loadSegmentationData;
    window.applySegmentCount = applySegmentCount;
    window.bootSegmentation = bootSegmentation;
})();
