/* Sign-in / registration page.
 *
 * The server replies with JSON and sets the JWT as an HttpOnly cookie, so this
 * script never handles the token itself -- JavaScript cannot read the cookie,
 * which keeps the session out of reach of XSS.
 */

(function () {
  "use strict";

  /* --- tabs --------------------------------------------------------------- */

  const tabs = {
    login: document.getElementById("tab-login"),
    register: document.getElementById("tab-register"),
  };
  const panels = {
    login: document.getElementById("panel-login"),
    register: document.getElementById("panel-register"),
  };

  function selectTab(name) {
    Object.keys(tabs).forEach((key) => {
      if (!tabs[key] || !panels[key]) return;
      const active = key === name;
      tabs[key].setAttribute("aria-selected", active ? "true" : "false");
      panels[key].hidden = !active;
    });
  }

  if (tabs.login && tabs.register) {
    tabs.login.addEventListener("click", () => selectTab("login"));
    tabs.register.addEventListener("click", () => selectTab("register"));
    // ?register=1 opens the registration tab directly.
    if (new URLSearchParams(window.location.search).has("register")) {
      selectTab("register");
    }
  }

  /* --- show/hide password ------------------------------------------------- */

  document.querySelectorAll("[data-toggle-password]").forEach((button) => {
    button.addEventListener("click", () => {
      const input = document.getElementById(button.dataset.togglePassword);
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
      showError("login-error", "Please enter your email and password.");
      return;
    }

    const button = document.getElementById("login-submit");
    HC.setBusy(button, true, "Signing in");

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
      HC.setBusy(button, true, "Creating account");

      const result = await HC.api("/auth/api/register", {
        method: "POST",
        body: { email: email, password: password },
      });

      HC.setBusy(button, false);

      if (result.ok) {
        HC.toast(
          "Account created",
          result.data.user.email + " · role: " + result.data.user.role,
          "success"
        );
        registerForm.reset();
        // Prefill the sign-in form so the user only types the password again.
        document.getElementById("login-email").value = result.data.user.email;
        selectTab("login");
        document.getElementById("login-password").focus();
      } else {
        showError("register-error", result.data.error || "Registration failed.");
      }
    });
  }
})();
