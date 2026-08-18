/* ===========================================================================
   HomeCloud shared frontend helpers.
   Loaded on every page before the page-specific script. Exposes one global
   object, HC, instead of using modules -- no build step, no import maps.
   =========================================================================== */

const HC = (function () {
  "use strict";

  /* --- theme -------------------------------------------------------------- */

  const THEME_KEY = "homecloud-theme";

  function storedTheme() {
    try {
      return localStorage.getItem(THEME_KEY);
    } catch (e) {
      return null;
    }
  }

  function effectiveTheme() {
    const stored = storedTheme();
    if (stored === "light" || stored === "dark") return stored;
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }

  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    try {
      localStorage.setItem(THEME_KEY, theme);
    } catch (e) {
      /* private mode: the choice simply does not persist */
    }
    syncThemeIcons();
  }

  // The button shows the icon of the theme you would switch *to*.
  function syncThemeIcons() {
    const isDark = effectiveTheme() === "dark";
    document.querySelectorAll(".theme-icon-light").forEach((el) =>
      el.classList.toggle("hidden", !isDark)
    );
    document.querySelectorAll(".theme-icon-dark").forEach((el) =>
      el.classList.toggle("hidden", isDark)
    );
  }

  function initThemeToggle() {
    syncThemeIcons();
    document.querySelectorAll("#theme-toggle").forEach((button) => {
      button.addEventListener("click", () => {
        applyTheme(effectiveTheme() === "dark" ? "light" : "dark");
      });
    });
  }

  /* --- toasts ------------------------------------------------------------- */

  function toast(title, text, kind) {
    const region = document.getElementById("toast-region");
    if (!region) return;

    const el = document.createElement("div");
    el.className = "toast" + (kind ? " is-" + kind : "");

    const body = document.createElement("div");
    body.className = "toast-body";

    const titleEl = document.createElement("div");
    titleEl.className = "toast-title";
    titleEl.textContent = title;
    body.appendChild(titleEl);

    if (text) {
      const textEl = document.createElement("div");
      textEl.className = "toast-text";
      textEl.textContent = text;
      body.appendChild(textEl);
    }

    el.appendChild(body);
    region.appendChild(el);
    // Auto-dismiss; errors stay a bit longer so they can be read.
    window.setTimeout(() => el.remove(), kind === "error" ? 7000 : 4000);
  }

  /* --- fetch wrapper ------------------------------------------------------ */

  /**
   * Call the JSON API.
   * Resolves to { ok, status, data }. A 401 means the session expired, so we
   * send the user back to the login page instead of showing a broken view.
   */
  async function api(path, options) {
    const settings = Object.assign({ method: "GET", headers: {} }, options || {});
    settings.headers = Object.assign({ Accept: "application/json" }, settings.headers);

    if (settings.body !== undefined && typeof settings.body !== "string") {
      settings.headers["Content-Type"] = "application/json";
      settings.body = JSON.stringify(settings.body);
    }

    let response;
    try {
      response = await fetch(path, settings);
    } catch (networkError) {
      return { ok: false, status: 0, data: { error: "Could not reach the server." } };
    }

    if (response.status === 401 && !path.startsWith("/auth/api/login")) {
      const next = encodeURIComponent(window.location.pathname);
      window.location.href = "/auth/login?next=" + next;
      return { ok: false, status: 401, data: {} };
    }

    let data = {};
    if (response.status !== 204) {
      data = await response.json().catch(() => ({}));
    }
    return { ok: response.ok, status: response.status, data };
  }

  /* --- button loading state ---------------------------------------------- */

  function setBusy(button, busy, busyLabel) {
    if (!button) return;
    if (busy) {
      button.dataset.originalHtml = button.innerHTML;
      button.setAttribute("aria-busy", "true");
      button.disabled = true;
      button.replaceChildren();
      const spinner = document.createElement("span");
      spinner.className = "spinner";
      button.appendChild(spinner);
      if (busyLabel) button.appendChild(document.createTextNode(" " + busyLabel));
    } else {
      button.removeAttribute("aria-busy");
      button.disabled = false;
      if (button.dataset.originalHtml) button.innerHTML = button.dataset.originalHtml;
    }
  }

  /* --- formatting --------------------------------------------------------- */

  /** SQLite writes "YYYY-MM-DD HH:MM:SS" in UTC -- make that explicit. */
  function parseTimestamp(value) {
    if (!value) return null;
    const date = new Date(value.replace(" ", "T") + "Z");
    return isNaN(date.getTime()) ? null : date;
  }

  /** Short absolute time in the viewer's locale, e.g. "18 Aug, 16:22". */
  function formatDateTime(value) {
    const date = parseTimestamp(value);
    if (!date) return "–";
    return date.toLocaleString(undefined, {
      day: "2-digit",
      month: "short",
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  /** "just now", "5 min ago", "3 h ago", "2 d ago", else an absolute date. */
  function formatRelative(value) {
    const date = parseTimestamp(value);
    if (!date) return "–";

    const seconds = Math.round((Date.now() - date.getTime()) / 1000);
    if (seconds < 45) return "just now";
    if (seconds < 3600) return Math.round(seconds / 60) + " min ago";
    if (seconds < 86400) return Math.round(seconds / 3600) + " h ago";
    if (seconds < 604800) return Math.round(seconds / 86400) + " d ago";
    return formatDateTime(value);
  }

  /** Cell helper: a <td> with plain text (never innerHTML, never unescaped). */
  function cell(value, className) {
    const td = document.createElement("td");
    if (className) td.className = className;
    if (value instanceof Node) {
      td.appendChild(value);
    } else {
      td.textContent = value === null || value === undefined || value === "" ? "–" : String(value);
    }
    return td;
  }

  /** Status pill for a resource state. */
  function statusBadge(status) {
    const span = document.createElement("span");
    span.className = "badge badge-" + String(status).replace(/[^a-z0-9-]/gi, "");
    span.textContent = status;
    return span;
  }

  /** Render N shimmering placeholder rows while a request is in flight. */
  function renderSkeletonRows(tbody, rows, columns) {
    const fragment = document.createDocumentFragment();
    for (let r = 0; r < rows; r++) {
      const tr = document.createElement("tr");
      for (let c = 0; c < columns; c++) {
        const td = document.createElement("td");
        const bar = document.createElement("div");
        bar.className = "skeleton";
        bar.style.width = 40 + ((r + c) % 4) * 15 + "%";
        td.appendChild(bar);
        tr.appendChild(td);
      }
      fragment.appendChild(tr);
    }
    tbody.replaceChildren(fragment);
  }

  /* --- misc --------------------------------------------------------------- */

  async function copyToClipboard(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (e) {
      // Clipboard API needs a secure context (HTTPS or localhost).
      return false;
    }
  }

  document.addEventListener("DOMContentLoaded", initThemeToggle);

  return {
    api,
    toast,
    setBusy,
    cell,
    statusBadge,
    renderSkeletonRows,
    formatDateTime,
    formatRelative,
    copyToClipboard,
    applyTheme,
    effectiveTheme,
  };
})();
