/* ==========================================================================
   PHARMALINK - shared interaction layer
   Adds the same "premium" micro-interactions to every portal without
   touching any existing page logic: button ripple feedback, a global
   toast helper, and auto-dismiss for any .message/.toast the app already
   creates. Safe to include everywhere - it only ever ADDS behaviour.
   ========================================================================== */
(function () {
  "use strict";

  /* ---- 0. In-app alert / confirm / prompt (no browser "localhost says") ---- */
  var sysQueue = [];
  var sysBusy = false;
  var sysCurrent = null;

  function sysEnsure() {
    var overlay = document.getElementById('systemDialogOverlay');
    if (overlay) return overlay;
    overlay = document.createElement('div');
    overlay.id = 'systemDialogOverlay';
    overlay.className = 'staff-notif-modal-overlay';
    overlay.setAttribute('hidden', 'hidden');
    overlay.innerHTML =
      '<div class="staff-notif-modal staff-notif-modal--system" role="alertdialog" aria-modal="true" aria-labelledby="systemDialogTitle">' +
        '<div class="staff-notif-modal-head">' +
          '<h3 id="systemDialogTitle"><i data-sys-icon class="fas fa-circle-info"></i><span data-sys-title>Notice</span></h3>' +
          '<button type="button" class="staff-notif-modal-close" data-sys-dismiss aria-label="Close">&times;</button>' +
        '</div>' +
        '<div class="staff-notif-modal-body">' +
          '<p data-sys-msg></p>' +
          '<input type="text" class="staff-notif-prompt-input" data-sys-input autocomplete="off">' +
        '</div>' +
        '<div class="staff-notif-modal-actions">' +
          '<button type="button" class="staff-notif-modal-yes" data-sys-yes><i class="fas fa-check"></i> <span data-sys-yes-label>OK</span></button>' +
          '<button type="button" class="staff-notif-modal-no" data-sys-no><i class="fas fa-xmark"></i> No</button>' +
        '</div>' +
      '</div>';
    document.body.appendChild(overlay);

    function accept() {
      if (!sysCurrent) return;
      if (sysCurrent.kind === 'prompt') {
        var inp = overlay.querySelector('[data-sys-input]');
        sysFinish(inp ? inp.value : '');
      } else if (sysCurrent.kind === 'confirm') {
        sysFinish(true);
      } else {
        sysFinish(undefined);
      }
    }
    function dismiss() {
      if (!sysCurrent) return;
      if (sysCurrent.kind === 'prompt') sysFinish(null);
      else if (sysCurrent.kind === 'confirm') sysFinish(false);
      else sysFinish(undefined);
    }

    overlay.addEventListener('click', function (ev) {
      if (ev.target === overlay) dismiss();
    });
    overlay.querySelector('[data-sys-yes]').addEventListener('click', accept);
    overlay.querySelector('[data-sys-no]').addEventListener('click', dismiss);
    overlay.querySelector('[data-sys-dismiss]').addEventListener('click', dismiss);
    overlay.querySelector('[data-sys-input]').addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter') {
        ev.preventDefault();
        accept();
      }
    });
    document.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape' && overlay.classList.contains('open')) {
        ev.preventDefault();
        dismiss();
      }
    });
    return overlay;
  }

  function sysFinish(value) {
    var job = sysCurrent;
    sysCurrent = null;
    sysBusy = false;
    var overlay = document.getElementById('systemDialogOverlay');
    if (overlay) {
      overlay.classList.remove('open');
      overlay.setAttribute('hidden', 'hidden');
    }
    if (job && typeof job.resolve === 'function') {
      try { job.resolve(value); } catch (e) {}
    }
    setTimeout(sysPump, 0);
  }

  function sysPump() {
    if (sysBusy || !sysQueue.length) return;
    if (!document.body) {
      document.addEventListener('DOMContentLoaded', sysPump);
      return;
    }
    sysBusy = true;
    var job = sysQueue.shift();
    sysCurrent = job;
    var overlay = sysEnsure();
    var title = overlay.querySelector('[data-sys-title]');
    var msg = overlay.querySelector('[data-sys-msg]');
    var icon = overlay.querySelector('[data-sys-icon]');
    var input = overlay.querySelector('[data-sys-input]');
    var noBtn = overlay.querySelector('[data-sys-no]');
    var yesLabel = overlay.querySelector('[data-sys-yes-label]');
    if (title) title.textContent = job.title;
    if (msg) msg.textContent = job.msg;
    if (icon) {
      icon.className = 'fas ' + (job.kind === 'confirm' ? 'fa-circle-question' : job.kind === 'prompt' ? 'fa-pen' : 'fa-circle-info');
    }
    if (input) {
      if (job.kind === 'prompt') {
        input.hidden = false;
        input.value = job.deflt || '';
        input.style.display = 'block';
      } else {
        input.hidden = true;
        input.value = '';
        input.style.display = 'none';
      }
    }
    var box = overlay.querySelector('.staff-notif-modal--system');
    if (box) {
      box.classList.toggle('staff-notif-modal--alert', job.kind === 'alert');
      box.classList.toggle('staff-notif-modal--confirm', job.kind === 'confirm');
      box.classList.toggle('staff-notif-modal--prompt', job.kind === 'prompt');
    }
    if (noBtn) {
      noBtn.hidden = job.kind === 'alert';
      noBtn.style.setProperty('display', job.kind === 'alert' ? 'none' : 'flex', 'important');
    }
    if (yesLabel) yesLabel.textContent = job.kind === 'alert' ? 'OK' : 'Yes';
    overlay.removeAttribute('hidden');
    overlay.classList.add('open');
    setTimeout(function () {
      if (job.kind === 'prompt' && input) input.focus();
      else {
        var yes = overlay.querySelector('[data-sys-yes]');
        if (yes) yes.focus();
      }
    }, 0);
  }

  function enqueueSys(job) {
    return new Promise(function (resolve) {
      job.resolve = resolve;
      sysQueue.push(job);
      sysPump();
    });
  }

  window.phAlert = function (message, title) {
    return enqueueSys({
      kind: 'alert',
      msg: String(message == null ? '' : message),
      title: title || 'Notice'
    });
  };
  window.phConfirm = function (message, title) {
    return enqueueSys({
      kind: 'confirm',
      msg: String(message == null ? '' : message),
      title: title || 'Please confirm'
    });
  };
  window.phPrompt = function (message, deflt, title) {
    return enqueueSys({
      kind: 'prompt',
      msg: String(message == null ? '' : message),
      deflt: deflt == null ? '' : String(deflt),
      title: title || 'Input'
    });
  };

  window.alert = function (message) {
    window.phAlert(message);
  };

  /* ---- 1. Ripple feedback on every clickable button ---- */
  function attachRipple(el) {
    if (el.dataset.phRippleBound) return;
    el.dataset.phRippleBound = "1";
    el.classList.add("ph-ripple-host");
    el.addEventListener("click", function (e) {
      const rect = el.getBoundingClientRect();
      const size = Math.max(rect.width, rect.height);
      const span = document.createElement("span");
      span.className = "ph-ripple";
      span.style.width = span.style.height = size + "px";
      span.style.left = (e.clientX - rect.left - size / 2) + "px";
      span.style.top = (e.clientY - rect.top - size / 2) + "px";
      el.appendChild(span);
      setTimeout(() => span.remove(), 600);
    });
  }

  function bindAllButtons(root) {
    (root || document)
      .querySelectorAll("button, .add-btn, .btn-primary, .nav-item, .pos-mode-btn")
      .forEach(attachRipple);
  }

  document.addEventListener("DOMContentLoaded", function () {
    bindAllButtons(document);
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.getRegistrations().then(function (regs) {
        regs.forEach(function (reg) { reg.unregister(); });
      }).catch(function () {});
    }
  });

  // Re-bind when the app injects new DOM (common in these SPA-style pages)
  const observer = new MutationObserver((mutations) => {
    mutations.forEach((m) => {
      m.addedNodes.forEach((node) => {
        if (node.nodeType === 1) bindAllButtons(node.parentElement || document);
      });
    });
  });
  document.addEventListener("DOMContentLoaded", function () {
    observer.observe(document.body, { childList: true, subtree: true });
  });

  /* ---- 2. Global toast helper: window.phToast('Saved!', 'success') ---- */
  window.phToast = function (text, type) {
    const el = document.createElement("div");
    el.className = "toast " + (type || "success");
    el.textContent = text;
    document.body.appendChild(el);
    setTimeout(() => {
      el.style.opacity = "0";
      el.style.transform = "translateY(10px)";
      el.style.transition = "opacity .3s, transform .3s";
      setTimeout(() => el.remove(), 300);
    }, 2600);
  };

  /* ---- 3. Auto-dismiss any legacy .message toast the app builds itself --- */
  const msgObserver = new MutationObserver((mutations) => {
    mutations.forEach((m) => {
      m.addedNodes.forEach((node) => {
        if (node.nodeType === 1 && node.classList && node.classList.contains("message")) {
          setTimeout(() => {
            node.style.opacity = "0";
            setTimeout(() => node.remove(), 300);
          }, 2600);
        }
      });
    });
  });
  document.addEventListener("DOMContentLoaded", function () {
    msgObserver.observe(document.body, { childList: true });
  });

  /* ---- 4. Staff (Admin/Cashier) notification bell ----
     Purely additive: only runs if #staffNotificationBell exists on the
     page (Admin & Cashier portals). Polls get_notifications_staff.php
     for low-stock / expiring-soon alerts. */
  document.addEventListener('DOMContentLoaded', function () {
    var bell = document.getElementById('staffNotificationBell');
    var dropdown = document.getElementById('staff-notification-dropdown');
    if (!bell || !dropdown) return;

    function iconFor(type) {
      if (type === 'low_stock' || type === 'low') return 'fa-box-open';
      if (type === 'out_of_stock' || type === 'out') return 'fa-circle-xmark';
      if (type === 'auto_po') return 'fa-cart-plus';
      if (type === 'expired') return 'fa-ban';
      return 'fa-triangle-exclamation';
    }

    function escHtml(s) {
      return String(s || '').replace(/[&<>"']/g, function (c) {
        return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c];
      });
    }

    var notesByKey = {};

    function goToStaffNotification(target, invFilter) {
      var adminNav = document.querySelector('.nav-item[data-target="' + target + '"]');
      if (adminNav) {
        adminNav.click();
        if (target === 'inventory' && invFilter && typeof window.applyInventoryCardFilter === 'function') {
          setTimeout(function () { window.applyInventoryCardFilter(invFilter); }, 350);
        }
        return;
      }
      var cashierPage = (target === 'dashboard') ? 'dashboard-page' : 'pos-page';
      var cashierNav = document.querySelector('.nav-item[data-page="' + cashierPage + '"]');
      if (cashierNav) cashierNav.click();
    }

    function hideStaffNotifPopup() {
      var overlay = document.getElementById('staffNotifDetailModal');
      if (!overlay) return;
      overlay.classList.remove('open');
      overlay.setAttribute('hidden', 'hidden');
    }

    function ensureStaffNotifPopup() {
      var overlay = document.getElementById('staffNotifDetailModal');
      if (overlay) return overlay;
      overlay = document.createElement('div');
      overlay.id = 'staffNotifDetailModal';
      overlay.className = 'staff-notif-modal-overlay';
      overlay.setAttribute('hidden', 'hidden');
      overlay.innerHTML =
        '<div class="staff-notif-modal" role="dialog" aria-modal="true" aria-labelledby="staffNotifDetailTitle">' +
          '<div class="staff-notif-modal-head">' +
            '<h3 id="staffNotifDetailTitle"><i data-icon class="fas fa-bell"></i><span data-title>Alert</span></h3>' +
            '<button type="button" class="staff-notif-modal-close" data-close aria-label="Close">&times;</button>' +
          '</div>' +
          '<div class="staff-notif-modal-body">' +
            '<p data-message></p>' +
            '<small data-date></small>' +
          '</div>' +
          '<div class="staff-notif-modal-actions">' +
            '<button type="button" class="btn-primary" data-close>Close</button>' +
          '</div>' +
        '</div>';
      document.body.appendChild(overlay);
      overlay.addEventListener('click', function (e) {
        if (e.target === overlay) hideStaffNotifPopup();
      });
      overlay.querySelectorAll('[data-close]').forEach(function (btn) {
        btn.addEventListener('click', hideStaffNotifPopup);
      });
      document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape' && overlay.classList.contains('open')) hideStaffNotifPopup();
      });
      return overlay;
    }

    function titleFor(n) {
      var type = (n && n.type) || '';
      var raw = ((n && n.title) || '').trim();
      if (type === 'low_stock' || type === 'low') return 'Low stock';
      if (type === 'out_of_stock' || type === 'out') return 'Out of stock';
      if (type === 'expired') return 'Expired lot';
      if (type === 'expiring_30' || type === 'expiring') return 'Near expiry (30 days)';
      if (type === 'expiring_90') return 'Near expiry (90 days)';
      if (type === 'auto_po') return 'Automatic purchase order';
      if (raw && raw.toLowerCase() !== 'out' && raw.toLowerCase() !== 'low') return raw;
      return 'Alert';
    }

    function showStaffNotifPopup(note) {
      var n = note || {};
      var overlay = ensureStaffNotifPopup();
      var icon = overlay.querySelector('[data-icon]');
      var title = overlay.querySelector('[data-title]');
      var message = overlay.querySelector('[data-message]');
      var date = overlay.querySelector('[data-date]');
      if (icon) icon.className = 'fas ' + iconFor(n.type);
      if (title) title.textContent = titleFor(n);
      if (message) message.textContent = n.message || '';
      if (date) date.textContent = n.date || '';
      overlay.removeAttribute('hidden');
      overlay.classList.add('open');
    }

    var lastUnreadCount = 0;

    function applyBadge(count) {
      lastUnreadCount = Math.max(0, Number(count) || 0);
      bell.classList.toggle('has-unread', lastUnreadCount > 0);
      var badge = bell.querySelector('.notification-badge');
      if (lastUnreadCount > 0) {
        if (!badge) {
          badge = document.createElement('span');
          badge.className = 'notification-badge';
          bell.appendChild(badge);
        }
        badge.textContent = lastUnreadCount > 99 ? '99+' : String(lastUnreadCount);
      } else if (badge) {
        badge.remove();
      }
    }

    function markItemReadLocally(item, notifKey) {
      if (item && item.parentNode) item.remove();
      if (notifKey) delete notesByKey[notifKey];
      lastUnreadCount = Math.max(0, lastUnreadCount - 1);
      applyBadge(lastUnreadCount);
      if (!dropdown.querySelector('.staff-notif-item')) {
        dropdown.innerHTML = '<div class="staff-notif-empty">No new alerts. You are all caught up!</div>';
      }
    }

    function markStaffNotificationRead(notifKey) {
      if (!notifKey) return;
      fetch('/api/staff/notifications/read?notif_key=' + encodeURIComponent(notifKey), {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ notif_key: notifKey })
      })
        .then(function (res) { return res.json(); })
        .then(function (data) {
          if (data && data.success) {
            applyBadge(data.count);
            if (Array.isArray(data.notifications)) render(data.notifications);
            return;
          }
          refresh();
        })
        .catch(function (err) {
          console.error('Failed to mark staff notification read:', err);
          refresh();
        });
    }

    function render(notifications) {
      notesByKey = {};
      (notifications || []).forEach(function (n) {
        if (n && n.notif_key) notesByKey[n.notif_key] = n;
      });
      if (!notifications || notifications.length === 0) {
        dropdown.innerHTML = '<div class="staff-notif-empty">No new alerts. You are all caught up!</div>';
        return;
      }
      dropdown.innerHTML = notifications.map(function (n) {
        var dateLine = n.date
          ? '<br><small style="color:#9ca3af;">' + escHtml(n.date) + '</small>'
          : '';
        return '<div class="staff-notif-item ' + escHtml(n.type) + '"' +
               ' data-notif-key="' + escHtml(n.notif_key) + '"' +
               ' data-target="' + escHtml(n.target || 'inventory') + '"' +
               ' data-inv-filter="' + escHtml(n.inv_filter || '') + '"' +
               ' role="button" tabindex="0">' +
               '<i class="fas ' + iconFor(n.type) + '"></i>' +
               '<span>' + escHtml(n.message) + dateLine + '</span></div>';
      }).join('');

      dropdown.querySelectorAll('.staff-notif-item[data-notif-key]').forEach(function (item) {
        item.addEventListener('click', function (e) {
          e.stopPropagation();
          var key = item.getAttribute('data-notif-key') || '';
          var note = notesByKey[key] || {
            notif_key: key,
            type: (item.className || '').replace('staff-notif-item', '').trim(),
            target: item.getAttribute('data-target') || 'inventory',
            inv_filter: item.getAttribute('data-inv-filter') || '',
            message: (item.querySelector('span') || {}).textContent || ''
          };
          markItemReadLocally(item, key);
          markStaffNotificationRead(key);
          goToStaffNotification(note.target || 'inventory', note.inv_filter || '');
          dropdown.classList.remove('open');
          showStaffNotifPopup(note);
        });
        item.addEventListener('keydown', function (e) {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            item.click();
          }
        });
      });
    }

    function refresh() {
      fetch('/api/staff/notifications', { credentials: 'same-origin' })
        .then(function (res) { return res.json(); })
        .then(function (data) {
          render(data.notifications);
          applyBadge(data.count || 0);
        })
        .catch(function (err) { console.error('Notification fetch error:', err); });
    }

    dropdown.addEventListener('click', function (e) { e.stopPropagation(); });

    bell.addEventListener('click', function (e) {
      e.stopPropagation();
      dropdown.classList.toggle('open');
      if (dropdown.classList.contains('open')) refresh();
    });

    document.addEventListener('click', function (e) {
      if (!bell.contains(e.target)) dropdown.classList.remove('open');
    });

    refresh();
    setInterval(refresh, 30000);
  });

  /* ---- 5. Logout Yes / No confirm (Admin, Cashier, Customer) ---- */
  function portalLabel() {
    var roleEl = document.querySelector('.sidebar .role, .sidebar-header .role');
    var t = roleEl ? String(roleEl.textContent || '').toUpperCase() : '';
    if (t.indexOf('ADMIN') !== -1) return 'Admin';
    if (t.indexOf('CASHIER') !== -1) return 'Cashier';
    if (t.indexOf('CUSTOMER') !== -1) return 'Customer';
    return 'this account';
  }

  function hideLogoutConfirm() {
    var overlay = document.getElementById('logoutConfirmModal');
    if (!overlay) return;
    overlay.classList.remove('open');
    overlay.setAttribute('hidden', 'hidden');
  }

  function showLogoutConfirm() {
    var overlay = document.getElementById('logoutConfirmModal');
    if (!overlay) {
      overlay = document.createElement('div');
      overlay.id = 'logoutConfirmModal';
      overlay.className = 'staff-notif-modal-overlay';
      overlay.setAttribute('hidden', 'hidden');
      overlay.innerHTML =
        '<div class="staff-notif-modal" role="dialog" aria-modal="true" aria-labelledby="logoutConfirmTitle">' +
          '<div class="staff-notif-modal-head">' +
            '<h3 id="logoutConfirmTitle"><i class="fas fa-right-from-bracket"></i><span data-logout-title>Log out?</span></h3>' +
            '<button type="button" class="staff-notif-modal-close" data-logout-no aria-label="Close">&times;</button>' +
          '</div>' +
          '<div class="staff-notif-modal-body">' +
            '<p data-logout-msg></p>' +
          '</div>' +
          '<div class="staff-notif-modal-actions">' +
            '<button type="button" class="staff-notif-modal-yes" data-logout-yes><i class="fas fa-check"></i> Yes</button>' +
            '<button type="button" class="staff-notif-modal-no" data-logout-no><i class="fas fa-xmark"></i> No</button>' +
          '</div>' +
        '</div>';
      document.body.appendChild(overlay);
      overlay.addEventListener('click', function (ev) {
        if (ev.target === overlay) hideLogoutConfirm();
      });
      overlay.querySelectorAll('[data-logout-no]').forEach(function (btn) {
        btn.addEventListener('click', hideLogoutConfirm);
      });
      overlay.querySelector('[data-logout-yes]').addEventListener('click', function () {
        window.location.href = '/api/auth/logout';
      });
      document.addEventListener('keydown', function (ev) {
        if (ev.key === 'Escape' && overlay.classList.contains('open')) hideLogoutConfirm();
      });
    }
    var label = portalLabel();
    var title = overlay.querySelector('[data-logout-title]');
    var msg = overlay.querySelector('[data-logout-msg]');
    if (title) title.textContent = 'Log out as ' + label + '?';
    if (msg) msg.textContent = 'Are you sure you want to log out of the ' + label + ' portal?';
    overlay.removeAttribute('hidden');
    overlay.classList.add('open');
  }

  document.addEventListener('click', function (e) {
    var trigger = e.target.closest && e.target.closest('a[href*="/api/auth/logout"], #logoutBtn');
    if (!trigger) return;
    e.preventDefault();
    e.stopPropagation();
    showLogoutConfirm();
  }, true);
})();