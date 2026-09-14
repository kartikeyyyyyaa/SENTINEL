/* login.js — the dedicated sign-in page. A trimmed copy of app.js's own
 * wireAuth() logic (same field-validation behaviour, same API calls) but
 * standalone: this page has no console shell to hide behind a login overlay,
 * so a successful sign-in navigates straight to index.html.
 */
(function () {
  "use strict";

  function validateField(input, errEl, message) {
    const ok = input.value.trim().length > 0;
    errEl.textContent = ok ? "" : message;
    input.classList.toggle("invalid", !ok);
    return ok;
  }

  function wireAuth() {
    const form = document.getElementById("login-form");
    const err = document.getElementById("login-error");
    const btn = document.getElementById("login-submit");
    const userIn = document.getElementById("login-username");
    const passIn = document.getElementById("login-password");
    const userErr = document.getElementById("login-username-err");
    const passErr = document.getElementById("login-password-err");

    if (window.SentinelAPI && window.SentinelAPI.getSession()) {
      // Already signed in — no reason to show the form again.
      window.location.href = "index.html";
      return;
    }

    userIn.addEventListener("blur", () => validateField(userIn, userErr, "Enter your username."));
    passIn.addEventListener("blur", () => validateField(passIn, passErr, "Enter your password."));
    userIn.addEventListener("input", () => { if (userErr.textContent) validateField(userIn, userErr, "Enter your username."); });
    passIn.addEventListener("input", () => { if (passErr.textContent) validateField(passIn, passErr, "Enter your password."); });

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      err.textContent = "";
      const userOk = validateField(userIn, userErr, "Enter your username.");
      const passOk = validateField(passIn, passErr, "Enter your password.");
      if (!userOk || !passOk) { (userOk ? passIn : userIn).focus(); return; }

      btn.disabled = true;
      btn.textContent = "Signing in…";
      try {
        await window.SentinelAPI.login(userIn.value.trim(), passIn.value);
        window.location.href = "index.html";
      } catch (e2) {
        err.textContent = e2.message || "Sign-in failed. Check your credentials and try again.";
        btn.disabled = false;
        btn.textContent = "Sign in";
        passIn.focus();
      }
    });
  }

  document.addEventListener("DOMContentLoaded", wireAuth);
})();
