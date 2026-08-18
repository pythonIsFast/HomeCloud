// Login / registration page. Vanilla JS, no framework.
//
// The server answers with JSON and sets an HttpOnly cookie containing the JWT.
// That means this script never touches the token itself -- JavaScript cannot
// read the cookie, which keeps the token out of reach of XSS.

const messageBox = document.getElementById("auth-message");

function showMessage(text, kind) {
  messageBox.textContent = text;
  messageBox.className = "message " + (kind || "");
}

async function postJson(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  // Every endpoint answers with JSON; fall back to an empty object just in case.
  const data = await response.json().catch(() => ({}));
  return { ok: response.ok, status: response.status, data };
}

document.getElementById("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  showMessage("Signing in...");

  const result = await postJson("/auth/api/login", {
    email: document.getElementById("login-email").value,
    password: document.getElementById("login-password").value,
  });

  if (result.ok) {
    // ?next=/some/path is set when a guard redirected us here.
    const next = new URLSearchParams(window.location.search).get("next");
    window.location.href = next && next.startsWith("/") ? next : "/";
  } else {
    showMessage(result.data.error || "Sign in failed.", "error");
  }
});

const registerForm = document.getElementById("register-form");
if (registerForm) {
  registerForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    showMessage("Creating account...");

    const result = await postJson("/auth/api/register", {
      email: document.getElementById("register-email").value,
      password: document.getElementById("register-password").value,
    });

    if (result.ok) {
      showMessage(
        `Account ${result.data.user.email} created (role: ${result.data.user.role}). You can sign in now.`,
        "ok"
      );
      registerForm.reset();
    } else {
      showMessage(result.data.error || "Registration failed.", "error");
    }
  });
}
