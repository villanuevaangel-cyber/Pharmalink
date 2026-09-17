// FILE: login.js

document.addEventListener('DOMContentLoaded', function() {
    const loginForm = document.getElementById('loginForm');
    const passwordInput = document.getElementById('password');
    const loginMessage = document.getElementById('loginMessage');

    function roleDest(role) {
        const r = String(role || '').toLowerCase();
        if (r === 'admin') return 'admin/admin.html';
        if (r === 'cashier/pharmacist') return 'cashier/cashier.html';
        if (r === 'customer') return 'customer/customer.html';
        return '';
    }

    function showLoginError(text) {
        if (loginMessage) {
            loginMessage.innerHTML = '<div class="message error">' + text + '</div>';
        } else {
            alert(text);
        }
    }

    if (loginForm) {
        loginForm.addEventListener('submit', async function(e) {
            e.preventDefault();
            if (loginMessage) loginMessage.innerHTML = '';

            const usernameValue = document.getElementById('email').value.trim();
            const passwordValue = passwordInput.value.trim();
            const submitBtn = loginForm.querySelector('button[type="submit"]');
            if (submitBtn) {
                submitBtn.disabled = true;
                submitBtn.textContent = 'Signing in...';
            }

            try {
                const response = await fetch('/api/auth/login', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    credentials: 'same-origin',
                    body: JSON.stringify({ username: usernameValue, password: passwordValue })
                });

                let result = {};
                try {
                    result = await response.json();
                } catch (parseErr) {
                    showLoginError(response.ok
                        ? 'Could not sign in. Please try again.'
                        : 'Server is not responding. Make sure PharmaLink is running, then try again.');
                    return;
                }

                if (result.success) {
                    const dest = roleDest(result.role);
                    if (!dest) {
                        showLoginError('Login successful, but this role is unrecognized. Please contact support.');
                        return;
                    }
                    window.location.href = dest;
                } else {
                    showLoginError(result.message || 'Invalid username or password!');
                }
            } catch (err) {
                showLoginError('Server is not running. Start PharmaLink, then try again.');
            } finally {
                if (submitBtn) {
                    submitBtn.disabled = false;
                    submitBtn.textContent = 'Log In';
                }
            }
        });
    }

    document.querySelectorAll('.toggle-password').forEach(function (toggle) {
        toggle.addEventListener('click', function () {
            const group = toggle.closest('.input-group');
            const input = (toggle.getAttribute('data-target')
                ? document.getElementById(toggle.getAttribute('data-target'))
                : null) || (group && group.querySelector('input')) || passwordInput;
            if (!input) return;
            const show = input.getAttribute('type') === 'password';
            input.setAttribute('type', show ? 'text' : 'password');
            const icon = toggle.matches('i') ? toggle : toggle.querySelector('i');
            if (icon) {
                icon.classList.toggle('fa-eye-slash', show);
                icon.classList.toggle('fa-eye', !show);
            }
            toggle.setAttribute('aria-label', show ? 'Hide password' : 'Show password');
        });
    });
});
