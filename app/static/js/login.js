/* Sign-in / registration.
 *
 * The server replies with JSON and sets the JWT as an HttpOnly cookie, so this
 * script never handles the token itself -- JavaScript cannot read that cookie,
 * which keeps the session out of reach of XSS.
 */

(function () {
  "use strict";

  const panels = {
    login: document.getElementById("panel-login"),
    register: document.getElementById("panel-register"),
  };

  /** Show one of the two panels (registration may be disabled server side). */
  function show(name) {
    if (!panels[name]) return;
    Object.keys(panels).forEach((key) => {
      if (panels[key]) panels[key].hidden = key !== name;
    });
    const focusTarget = document.getElementById(name + "-email");
    if (focusTarget) focusTarget.focus();
  }

  const toRegister = document.getElementById("tab-register");
  const toLogin = document.getElementById("tab-login");
  if (toRegister) toRegister.addEventListener("click", () => show("register"));
  if (toLogin) toLogin.addEventListener("click", () => show("login"));

  // /auth/login?register=1 opens the registration form directly.
  if (panels.register && new URLSearchParams(window.location.search).has("register")) {
    show("register");
  }

  /* --- reveal password ---------------------------------------------------- */

  document.querySelectorAll("[data-reveal]").forEach((button) => {
    button.addEventListener("click", () => {
      const input = document.getElementById(button.dataset.reveal);
      const revealed = input.type === "text";
      input.type = revealed ? "password" : "text";
      button.textContent = revealed ? "Show" : "Hide";
    });
  });

  /* --- inline errors ------------------------------------------------------ */

  function showError(id, message) {
    const el = document.getElementById(id);
    el.textContent = message;
    el.classList.remove("hidden");
  }

  function clearError(id) {
    const el = document.getElementById(id);
    el.textContent = "";
    el.classList.add("hidden");
  }

  /* --- sign in ------------------------------------------------------------ */

  document.getElementById("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    clearError("login-error");

    const email = document.getElementById("login-email").value.trim();
    const password = document.getElementById("login-password").value;

    if (!email || !password) {
      showError("login-error", "Enter your email and password.");
      return;
    }

    const button = document.getElementById("login-submit");
    HC.setBusy(button, true);

    const result = await HC.api("/auth/api/login", {
      method: "POST",
      body: { email: email, password: password },
    });

    if (result.ok) {
      // ?next=/some/path is set when a guard redirected us here.
      const next = new URLSearchParams(window.location.search).get("next");
      window.location.href = next && next.startsWith("/") ? next : "/";
      return;
    }

    HC.setBusy(button, false);
    showError("login-error", result.data.error || "Sign in failed.");
  });

  /* --- register ----------------------------------------------------------- */

  const registerForm = document.getElementById("register-form");
  if (registerForm) {
    registerForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      clearError("register-error");

      const email = document.getElementById("register-email").value.trim();
      const password = document.getElementById("register-password").value;

      if (password.length < 8) {
        showError("register-error", "The password must be at least 8 characters.");
        return;
      }

      const button = document.getElementById("register-submit");
      HC.setBusy(button, true);

      const result = await HC.api("/auth/api/register", {
        method: "POST",
        body: { email: email, password: password },
      });

      HC.setBusy(button, false);

      if (result.ok) {
        HC.toast("Account created", result.data.user.email + " · " + result.data.user.role, "success");
        registerForm.reset();
        // Carry the address over so only the password has to be typed again.
        document.getElementById("login-email").value = result.data.user.email;
        show("login");
        document.getElementById("login-password").focus();
      } else {
        showError("register-error", result.data.error || "Registration failed.");
      }
    });
  }
})();
