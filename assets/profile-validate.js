(function (w) {
  var NAME_RE = /^[A-Za-zÑñ][A-Za-zÑñ\s.'-]{0,48}$/;
  var EMAIL_RE = /^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$/;
  var PHONE_RE = /^(09\d{9}|\+639\d{9}|639\d{9}|9\d{9})$/;
  var USERNAME_RE = /^[A-Za-z][A-Za-z0-9._-]{2,29}$/;

  function cleanPhone(v) {
    return String(v || "").replace(/[\s\-()]/g, "");
  }

  function titleCaseName(v) {
    return String(v || "")
      .trim()
      .replace(/\s+/g, " ")
      .split(" ")
      .map(function (part) {
        if (!part) return "";
        return part.charAt(0).toUpperCase() + part.slice(1).toLowerCase();
      })
      .join(" ");
  }

  function nationalPhone(v) {
    var d = String(v || "").replace(/\D/g, "");
    if (d.indexOf("63") === 0 && d.length >= 12) d = d.slice(2);
    if (d.charAt(0) === "0" && d.length === 11) d = d.slice(1);
    if (d.length > 10) d = d.slice(-10);
    return d;
  }

  function e164Phone(v) {
    var n = nationalPhone(v);
    return /^9\d{9}$/.test(n) ? "+63" + n : "";
  }

  function displayPhone(v) {
    return nationalPhone(v);
  }

  function fillPhone(el, stored) {
    if (!el) return;
    el.value = displayPhone(stored);
  }

  function profileError(data) {
    var errors = fieldErrors(data);
    var order = ["first_name", "middle_name", "last_name", "email", "phone_number", "address"];
    for (var i = 0; i < order.length; i++) {
      if (errors[order[i]]) return errors[order[i]];
    }
    return "";
  }

  function passwordChecks(pw) {
    pw = String(pw || "");
    return {
      length: pw.length >= 8,
      upper: /[A-Z]/.test(pw),
      lower: /[a-z]/.test(pw),
      number: /\d/.test(pw),
      special: /[^A-Za-z0-9]/.test(pw)
    };
  }

  function passwordError(current, next, confirm) {
    if (!String(current || "").trim()) return "Current password is required.";
    if (!next) return "New password is required.";
    var c = passwordChecks(next);
    if (!c.length) return "Password must be at least 8 characters.";
    if (!c.upper) return "Password must include at least one uppercase letter.";
    if (!c.lower) return "Password must include at least one lowercase letter.";
    if (!c.number) return "Password must include at least one number.";
    if (!c.special) return "Password must include at least one special character.";
    if (next !== confirm) return "New passwords do not match.";
    if (current && next === current) return "New password must be different from the current password.";
    return "";
  }

  function renderHints(host) {
    if (!host) return;
    host.innerHTML =
      '<ul class="pw-rules">' +
        '<li data-rule="length">At least 8 characters</li>' +
        '<li data-rule="upper">One uppercase letter (A-Z)</li>' +
        '<li data-rule="lower">One lowercase letter (a-z)</li>' +
        '<li data-rule="number">One number (0-9)</li>' +
        '<li data-rule="special">One special character (!@#$…)</li>' +
      "</ul>";
  }

  function bindHints(newInput, host) {
    if (!newInput || !host) return;
    renderHints(host);
    function paint() {
      var c = passwordChecks(newInput.value);
      host.querySelectorAll("[data-rule]").forEach(function (li) {
        li.classList.toggle("ok", !!c[li.getAttribute("data-rule")]);
      });
    }
    newInput.addEventListener("input", paint);
    paint();
  }

  function usernameError(username) {
    var u = String(username || "").trim();
    if (!u) return "Username is required.";
    if (!USERNAME_RE.test(u)) return "Username must start with a letter and be 3-30 characters (letters, numbers, dot, underscore, hyphen).";
    return "";
  }

  function newPasswordError(next, optional) {
    if (optional && !String(next || "")) return "";
    if (!next) return "Password is required.";
    var c = passwordChecks(next);
    if (!c.length) return "Password must be at least 8 characters.";
    if (!c.upper) return "Password must include at least one uppercase letter.";
    if (!c.lower) return "Password must include at least one lowercase letter.";
    if (!c.number) return "Password must include at least one number.";
    if (!c.special) return "Password must include at least one special character.";
    return "";
  }

  function fieldErrors(data, opts) {
    opts = opts || {};
    var first = titleCaseName(data.first_name);
    var last = titleCaseName(data.last_name);
    var middle = titleCaseName(data.middle_name);
    var email = String(data.email || "").trim();
    var phone = cleanPhone(data.phone_number);
    var national = nationalPhone(phone);
    var address = String(data.address || "").trim();
    var errors = {};
    if (!first) errors.first_name = "First name is required.";
    else if (!NAME_RE.test(first)) errors.first_name = "Letters, spaces, periods, apostrophes, or hyphens only.";
    if (middle && !NAME_RE.test(middle)) errors.middle_name = "Letters, spaces, periods, apostrophes, or hyphens only.";
    if (!last) errors.last_name = "Last name is required.";
    else if (!NAME_RE.test(last)) errors.last_name = "Letters, spaces, periods, apostrophes, or hyphens only.";
    if (!email) errors.email = "Email is required.";
    else if (!EMAIL_RE.test(email)) errors.email = "Enter a valid email address.";
    if (!national) errors.phone_number = "Contact number is required.";
    else if (!PHONE_RE.test(national) && !PHONE_RE.test(phone)) {
      errors.phone_number = "Use a Philippine mobile: +63 9XXXXXXXXX.";
    }
    if (address.length < 5) errors.address = "Address must be at least 5 characters.";
    else if (address.length > 200) errors.address = "Address must be 200 characters or less.";
    if (opts.requireUsername) {
      var uErr = usernameError(data.username);
      if (uErr) errors.username = uErr;
    }
    if (opts.requirePassword || String(data.password || "")) {
      var pErr = newPasswordError(data.password, false);
      if (pErr) errors.password = pErr;
    }
    if (opts.requireCustomerType) {
      var ctype = String(data.customer_type || "");
      if (["Regular", "Senior", "PWD", "Other"].indexOf(ctype) < 0) errors.customer_type = "Select a valid customer type.";
    }
    if (data.loyalty_points != null && data.loyalty_points !== "") {
      var pts = Number(data.loyalty_points);
      if (!isFinite(pts) || pts < 0) errors.loyalty_points = "Points cannot be negative.";
    }
    return errors;
  }

  function paintFieldErrors(root, errors) {
    if (!root) return;
    errors = errors || {};
    root.querySelectorAll("[data-err]").forEach(function (el) {
      var key = el.getAttribute("data-err");
      var msg = errors[key] || "";
      el.textContent = msg;
      var field = root.querySelector("#" + key + ", [name='" + key + "']");
      if (field) field.classList.toggle("is-invalid", !!msg);
    });
  }

  function bindNameCaps(root) {
    if (!root) return;
    ["first_name", "middle_name", "last_name"].forEach(function (name) {
      var el = root.querySelector("#" + name + ", [name='" + name + "']");
      if (!el) return;
      el.addEventListener("blur", function () {
        el.value = titleCaseName(el.value);
      });
    });
  }

  function bindPhoneDigits(el) {
    if (!el) return;
    el.addEventListener("input", function () {
      el.value = nationalPhone(el.value);
    });
  }

  w.phProfileValidate = {
    profileError: profileError,
    fieldErrors: fieldErrors,
    paintFieldErrors: paintFieldErrors,
    usernameError: usernameError,
    newPasswordError: newPasswordError,
    passwordError: passwordError,
    passwordChecks: passwordChecks,
    bindHints: bindHints,
    bindNameCaps: bindNameCaps,
    bindPhoneDigits: bindPhoneDigits,
    cleanPhone: cleanPhone,
    titleCaseName: titleCaseName,
    nationalPhone: nationalPhone,
    e164Phone: e164Phone,
    displayPhone: displayPhone,
    fillPhone: fillPhone
  };
})(window);
