/**
 * Logs meaningful UI clicks for logged-in users (nav, buttons, links, tabs).
 * Skips typing, password fields, and empty icon-only noise when possible.
 */
(function () {
    const SELECTOR = [
        'button',
        'a',
        '.nav-item',
        '[role="button"]',
        '[role="tab"]',
        'input[type="submit"]',
        'input[type="button"]',
        'input[type="checkbox"]',
        'select',
        'summary',
        '.um-tab',
        '.inv-tab-btn',
        '.rpt-tab',
        '.maint-tab',
        '.ad-dash-tab',
        '.quick-action-btn'
    ].join(',');

    let lastKey = '';
    let lastAt = 0;

    function labelFor(el) {
        if (!el) return '';
        const named = el.getAttribute('aria-label') || el.getAttribute('title') || '';
        if (named.trim()) return named.trim();
        if (el.matches && el.matches('select')) {
            const opt = el.options && el.options[el.selectedIndex];
            const name = el.getAttribute('name') || el.id || 'Dropdown';
            return name + ': ' + ((opt && opt.text) || el.value || '');
        }
        if (el.matches && el.matches('input[type="checkbox"]')) {
            const lab = el.closest('label');
            const t = (lab && lab.textContent) || el.id || 'Checkbox';
            return t.trim() + (el.checked ? ' (on)' : ' (off)');
        }
        const text = (el.innerText || el.textContent || el.value || '').replace(/\s+/g, ' ').trim();
        return text.slice(0, 120);
    }

    function skip(el) {
        if (!el || el.closest('#posPayQr')) return true;
        if (el.closest('input[type="password"], [type="password"]')) return true;
        const type = (el.getAttribute && el.getAttribute('type')) || '';
        if (String(type).toLowerCase() === 'password') return true;
        return false;
    }

    function send(label) {
        const key = label + '|' + location.pathname;
        const now = Date.now();
        if (key === lastKey && now - lastAt < 400) return;
        lastKey = key;
        lastAt = now;
        const body = JSON.stringify({ label: label, page: location.pathname + location.hash });
        try {
            fetch('/api/activity/click', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'same-origin',
                body: body,
                keepalive: true
            }).catch(function () {});
        } catch (e) {}
    }

    document.addEventListener('click', function (e) {
        const el = e.target && e.target.closest ? e.target.closest(SELECTOR) : null;
        if (!el || skip(el)) return;
        const label = labelFor(el);
        if (!label) return;
        send(label);
    }, true);
})();
