(function () {
    function ready(fn) {
        if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', fn);
        else fn();
    }

    ready(async function () {
        try {
            const meRes = await fetch('/api/auth/me', { credentials: 'same-origin' });
            if (!meRes.ok) {
                window.location.href = '/';
                return;
            }
            const me = await meRes.json();
            const role = String(me.role || '').toLowerCase();
            if (!me.success || (role !== 'cashier/pharmacist' && role !== 'admin')) {
                window.location.href = '/';
                return;
            }
            let staff = me.staff || {};
            try {
                const profileRes = await fetch('/api/cashier/profile', { credentials: 'same-origin' });
                const profile = await profileRes.json();
                if (profile.success && profile.staff) staff = profile.staff;
            } catch (err) {}
            applyProfile(staff, me.firstName, me.lastName, me.profile_image || staff.profile_image);
            startLiveClock();
        } catch (err) {
            window.location.href = '/';
            return;
        }

        try {
            const res = await fetch('/api/cashier/charts', { credentials: 'same-origin' });
            const charts = await res.json();
            window.CASHIER_FREQ_LABELS = charts.freq_labels || [];
            window.CASHIER_FREQ_DATA = charts.freq_data || [];
            window.CASHIER_SPEND_LABELS = charts.spend_labels || [];
            window.CASHIER_SPEND_DATA = charts.spend_data || [];
            window.CASHIER_TYPE_LABELS = charts.type_labels || [];
            window.CASHIER_TYPE_DATA = charts.type_data || [];
            fillSegmentationKpis(charts);
            refreshChart(window.purchaseFrequencyChartInstance, window.CASHIER_FREQ_LABELS, window.CASHIER_FREQ_DATA);
            refreshChart(window.spendingTierChartInstance, window.CASHIER_SPEND_LABELS, window.CASHIER_SPEND_DATA);
            refreshChart(window.customerTypeChartInstance, window.CASHIER_TYPE_LABELS, window.CASHIER_TYPE_DATA);
        } catch (err) {
            console.error('Failed to load cashier charts', err);
        }
    });

    function personName(staff, firstName, lastName, fallback) {
        const full = [staff.first_name || firstName, staff.last_name || lastName]
            .filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
        return full || fallback;
    }

    function applyProfile(staff, firstName, lastName, profileImage) {
        const name = personName(staff, firstName, lastName, 'Cashier');
        const welcome = document.getElementById('headerWelcomeName');
        if (welcome) welcome.textContent = 'Welcome, ' + name;
        const fields = ['first_name', 'middle_name', 'last_name', 'email', 'phone_number', 'address'];
        fields.forEach((id) => {
            const el = document.getElementById(id);
            if (!el || staff[id] == null) return;
            if (id === 'phone_number' && window.phProfileValidate) window.phProfileValidate.fillPhone(el, staff[id]);
            else el.value = staff[id];
        });
        const cardName = document.getElementById('profileCardName');
        if (cardName) cardName.textContent = `${staff.first_name || ''} ${staff.last_name || ''}`.trim() || name;
        const userEl = document.getElementById('profileUsername');
        if (userEl && staff.username) userEl.textContent = '@' + staff.username;
        const avatar = profileImage || staff.profile_image;
        if (avatar) {
            const preview = document.getElementById('profile_avatar');
            if (preview) preview.src = avatar;
            const headerAvatar = document.getElementById('headerProfileAvatar');
            if (headerAvatar) headerAvatar.src = avatar;
        }
        window.profileOriginal = window.profileOriginal || {};
        document.querySelectorAll('#profile-page .p-input').forEach((input) => {
            window.profileOriginal[input.id] = input.value;
        });
    }

    function startLiveClock() {
        const el = document.getElementById('live-clock');
        if (!el) return;
        const tick = () => {
            el.textContent = new Date().toLocaleString('en-PH', {
                weekday: 'long', year: 'numeric', month: 'short', day: 'numeric',
                hour: 'numeric', minute: '2-digit', second: '2-digit', hour12: true
            });
        };
        tick();
        setInterval(tick, 1000);
    }

    function fillSegmentationKpis(charts) {
        const spend = charts.spend_data || [];
        const types = charts.type_labels || [];
        const typeCounts = charts.type_data || [];
        const total = (charts.spend_labels || []).length;
        const active = spend.filter((amt) => Number(amt) > 0).length;
        const revenue = spend.reduce((sum, amt) => sum + Number(amt || 0), 0);
        const avg = active ? revenue / active : 0;
        let topType = '-';
        if (typeCounts.length) {
            let maxIdx = 0;
            typeCounts.forEach((count, idx) => {
                if (Number(count) > Number(typeCounts[maxIdx])) maxIdx = idx;
            });
            topType = types[maxIdx] || '-';
        }
        const setText = (id, value) => {
            const el = document.getElementById(id);
            if (el) el.textContent = value;
        };
        setText('segTotalCustomers', String(total));
        setText('segActiveCustomers', String(active));
        setText('segAvgSpend', '₱' + avg.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
        setText('segTopType', topType);
    }

    function refreshChart(chart, labels, data) {
        if (!chart || !chart.data) return;
        chart.data.labels = labels;
        if (chart.data.datasets && chart.data.datasets[0]) chart.data.datasets[0].data = data;
        chart.update();
    }
})();
