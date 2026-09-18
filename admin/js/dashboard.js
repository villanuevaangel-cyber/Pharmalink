/**
 * dashboard.js — powers admin/dashboard.html.
 *
 * Talks to: fetch_dashboard.php (summary cards), get_sales_analytics.php
 * (Sales Overview + Category Distribution charts).
 *
 * Note: the "real" admin dashboard now lives in admin.php (the Dashboard
 * tab of the SPA), which already renders these numbers server-side in PHP.
 * This file exists so the older standalone dashboard.html page (still
 * linked from the sidebar / upload-prescription flow) shows live data
 * instead of the hardcoded placeholder figures it shipped with.
 */
(function () {
    function money(n) {
        return '₱' + Number(n || 0).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }

    function setCard(index, value, changeText, isUp) {
        const cards = document.querySelectorAll('.cards .card');
        const card = cards[index];
        if (!card) return;
        const valueEl = card.querySelector('.value');
        if (valueEl) valueEl.textContent = value;
        const changeEl = card.querySelector('.change');
        if (changeEl && changeText !== undefined) {
            changeEl.textContent = changeText;
            changeEl.classList.toggle('up', !!isUp);
            changeEl.classList.toggle('down', !isUp);
        }
    }

    function loadSummaryCards() {
        fetch('/api/admin/dashboard?period=month')
            .then(res => res.json())
            .then(data => {
                setCard(0, money(data.total_sales), '(' + (data.period_label || 'This Month') + ')', true);
                setCard(1, (data.staff_count ?? 0) + (data.customer_count ?? 0), undefined, true);
                setCard(2, data.customer_count ?? 0, undefined, true);
                const totalStock = (data.ok_stock ?? 0) + (data.low_stock ?? 0) + (data.out_stock ?? 0);
                const healthyPct = totalStock ? Math.round(((data.ok_stock ?? 0) / totalStock) * 100) : 0;
                setCard(3, healthyPct + '%', (data.low_stock ?? 0) + ' low / ' + (data.out_stock ?? 0) + ' out', healthyPct >= 80);
            })
            .catch(err => console.error('Dashboard summary load error:', err));
    }

    function loadCharts() {
        fetch('/api/admin/sales-analytics?year=' + new Date().getFullYear())
            .then(res => res.json())
            .then(data => {
                const salesCanvas = document.getElementById('salesChart');
                if (salesCanvas && window.Chart) {
                    new Chart(salesCanvas, {
                        type: 'line',
                        data: {
                            labels: data.monthly_labels || [],
                            datasets: [{
                                label: 'Sales (₱)',
                                data: data.monthly_data || [],
                                borderColor: '#4BAA8B',
                                backgroundColor: 'rgba(37,99,235,0.1)',
                                tension: 0.3,
                                fill: true,
                            }]
                        },
                        options: { responsive: true, plugins: { legend: { display: false } } }
                    });
                }

                const catCanvas = document.getElementById('categoryChart');
                if (catCanvas && window.Chart) {
                    new Chart(catCanvas, {
                        type: 'doughnut',
                        data: {
                            labels: data.category_labels || [],
                            datasets: [{
                                data: data.category_data || [],
                                backgroundColor: ['#4BAA8B', '#4BAA8B', '#FFC857', '#ef4444', '#FFC857', '#4BAA8B', '#FFC857', '#4BAA8B'],
                            }]
                        },
                        options: { responsive: true }
                    });
                }
            })
            .catch(err => console.error('Dashboard chart load error:', err));
    }

    function init() {
        loadSummaryCards();
        loadCharts();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();