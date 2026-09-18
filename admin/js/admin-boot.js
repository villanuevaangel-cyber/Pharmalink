(function () {
    function ready(fn) {
        if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", fn);
        else fn();
    }

    function escapeHtml(str) {
        return String(str == null ? "" : str).replace(/[&<>"']/g, (c) => ({
            "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
        }[c]));
    }

    ready(async function () {
        try {
            const meRes = await fetch("/api/auth/me", { credentials: "same-origin" });
            if (!meRes.ok) { window.location.href = "/"; return; }
            const me = await meRes.json();
            if (!me.success || String(me.role || "").toLowerCase() !== "admin") {
                window.location.href = "/";
                return;
            }
            window.ADMIN_USER_ID = me.user_id;
            let staff = me.staff || {};
            try {
                const profileRes = await fetch("/api/admin/profile", { credentials: "same-origin" });
                const profile = await profileRes.json();
                if (profile.success && profile.staff) staff = profile.staff;
            } catch (err) {}
            applyProfile(staff, me.firstName, me.lastName, me.profile_image || staff.profile_image);
        } catch (err) {
            window.location.href = "/";
            return;
        }

        const yearSel = document.getElementById("analytics-year");
        if (yearSel) {
            const y = new Date().getFullYear();
            const current = yearSel.value;
            yearSel.innerHTML = "";
            for (let i = 0; i < 5; i++) {
                const opt = document.createElement("option");
                opt.value = String(y - i);
                opt.textContent = String(y - i);
                if (String(y - i) === current || (!current && i === 0)) opt.selected = true;
                yearSel.appendChild(opt);
            }
            yearSel.dispatchEvent(new Event("change"));
        }

        loadStaffTable();
        loadCustomerTable();
    });

    function personName(staff, firstName, lastName, fallback) {
        const full = [staff.first_name || firstName, staff.last_name || lastName]
            .filter(Boolean).join(" ").replace(/\s+/g, " ").trim();
        return full || fallback;
    }

    function applyProfile(staff, firstName, lastName, profileImage) {
        const name = personName(staff, firstName, lastName, "Admin");
        const welcome = document.getElementById("headerWelcomeName");
        if (welcome) welcome.textContent = "Welcome, " + name;
        ["first_name", "middle_name", "last_name", "email", "phone_number", "address"].forEach((id) => {
            const el = document.getElementById(id);
            if (!el || staff[id] == null) return;
            if (id === "phone_number" && window.phProfileValidate) window.phProfileValidate.fillPhone(el, staff[id]);
            else el.value = staff[id];
        });
        const cardName = document.getElementById("profileCardName");
        if (cardName) cardName.textContent = `${staff.first_name || ""} ${staff.last_name || ""}`.trim() || name;
        const userEl = document.getElementById("profileUsername");
        if (userEl && staff.username) userEl.textContent = "@" + staff.username;
        const DEFAULT_AVATAR = "https://cdn-icons-png.flaticon.com/512/2922/2922510.png";
        const avatar = [profileImage, staff.profile_image].find((src) => src && src !== DEFAULT_AVATAR) || DEFAULT_AVATAR;
        const preview = document.getElementById("profile_avatar");
        if (preview) {
            preview.src = avatar;
            preview.onerror = function () { this.onerror = null; this.src = DEFAULT_AVATAR; };
        }
        const headerAvatar = document.getElementById("headerProfileAvatar");
        if (headerAvatar) {
            headerAvatar.src = avatar;
            headerAvatar.onerror = function () { this.onerror = null; this.src = DEFAULT_AVATAR; };
        }
        window.profileOriginal = window.profileOriginal || {};
        document.querySelectorAll("#profile .p-input").forEach((input) => {
            window.profileOriginal[input.id] = input.value;
        });
    }

    function isAdminRole(role) {
        return String(role || "").toLowerCase() === "admin";
    }

    function umIsActive(v) {
        return v === true || v === 1 || v === "1" || Number(v) === 1;
    }

    function staffRowHtml(s) {
        const fullName = `${s.first_name || ""} ${s.middle_name || ""} ${s.last_name || ""}`.replace(/\s+/g, " ").trim();
        const active = umIsActive(s.is_active);
        return `<tr data-id="${s.user_id}" data-um-row="1" data-role="${escapeHtml(s.role_name)}" data-active="${active ? "1" : "0"}">
            <td>${escapeHtml(fullName)}</td>
            <td>${escapeHtml(s.email || "")}</td>
            <td>${escapeHtml(s.phone_number || "")}</td>
            <td>${active ? '<span class="um-status active">Active</span>' : '<span class="um-status inactive">Deactivated</span>'}</td>
            <td class="action-btn-group">
                <button type="button" class="um-btn um-btn-edit edit-staff-btn" data-id="${s.user_id}"><i class="fas fa-pen"></i> Edit</button>
                ${active
                    ? `<button type="button" class="um-btn um-btn-reset reset-password-btn" data-id="${s.user_id}"><i class="fas fa-key"></i> Reset</button>
                       <button type="button" class="um-btn um-btn-danger delete-staff-btn" data-id="${s.user_id}"><i class="fas fa-ban"></i> Deactivate</button>`
                    : `<button type="button" class="um-btn um-btn-activate activate-staff-btn" data-id="${s.user_id}"><i class="fas fa-check"></i> Activate</button>`}
            </td>
        </tr>`;
    }

    function loadStaffTable() {
        const adminBody = document.getElementById("admin-table-body");
        const cashierBody = document.getElementById("cashier-table-body");
        if (!adminBody && !cashierBody) return;
        fetch("/api/admin/staff", { credentials: "same-origin" })
            .then((r) => r.json())
            .then((rows) => {
                const list = Array.isArray(rows) ? rows : [];
                const admins = list.filter((s) => isAdminRole(s.role_name));
                const cashiers = list.filter((s) => !isAdminRole(s.role_name));
                const emptyAdmin = '<tr><td colspan="5" style="padding:15px;text-align:center;color:#6b7280;">No admin accounts.</td></tr>';
                const emptyCashier = '<tr><td colspan="5" style="padding:15px;text-align:center;color:#6b7280;">No cashier accounts.</td></tr>';
                if (adminBody) adminBody.innerHTML = admins.map(staffRowHtml).join("") || emptyAdmin;
                if (cashierBody) cashierBody.innerHTML = cashiers.map(staffRowHtml).join("") || emptyCashier;
            })
            .catch(() => {
                const fail = '<tr><td colspan="5" style="padding:15px;text-align:center;color:#dc2626;">Failed to load accounts.</td></tr>';
                if (adminBody) adminBody.innerHTML = fail;
                if (cashierBody) cashierBody.innerHTML = fail;
            });
    }

    function loadCustomerTable() {
        const body = document.getElementById("customer-table-body");
        if (!body) return;
        fetch("/api/admin/customers", { credentials: "same-origin" })
            .then((r) => r.json())
            .then((rows) => {
                const list = Array.isArray(rows) ? rows : [];
                body.innerHTML = list.map((c) => {
                    const fullName = `${c.first_name || ""} ${c.middle_name || ""} ${c.last_name || ""}`.replace(/\s+/g, " ").trim();
                    const active = umIsActive(c.is_active);
                    return `<tr data-id="${c.customer_id}" data-um-row="1" data-active="${active ? "1" : "0"}">
                        <td>${escapeHtml(fullName)}</td>
                        <td>${escapeHtml(c.email || "")}</td>
                        <td>${escapeHtml(c.phone_number || "")}</td>
                        <td>${escapeHtml(c.loyalty_points)}</td>
                        <td>${active ? '<span class="um-status active">Active</span>' : '<span class="um-status inactive">Deactivated</span>'}</td>
                        <td class="action-btn-group">
                            <button type="button" class="um-btn um-btn-edit edit-customer-btn" data-id="${c.customer_id}"><i class="fas fa-pen"></i> Edit</button>
                            ${active
                                ? `<button type="button" class="um-btn um-btn-danger delete-customer-btn" data-id="${c.customer_id}"><i class="fas fa-ban"></i> Deactivate</button>`
                                : `<button type="button" class="um-btn um-btn-activate activate-customer-btn" data-id="${c.customer_id}"><i class="fas fa-check"></i> Activate</button>`}
                        </td>
                    </tr>`;
                }).join("") || '<tr><td colspan="6" style="padding:15px;text-align:center;color:#6b7280;">No customers.</td></tr>';
            })
            .catch(() => {
                body.innerHTML = '<tr><td colspan="6" style="padding:15px;text-align:center;color:#dc2626;">Failed to load customers.</td></tr>';
            });
    }

    window.reloadAdminUserTables = function () {
        loadStaffTable();
        loadCustomerTable();
    };
})();
