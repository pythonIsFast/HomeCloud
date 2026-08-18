/* ===========================================================================
   HomeCloud shared frontend helpers.
   Loaded on every page before the page script. Exposes one global object, HC,
   instead of ES modules -- no build step, no import maps.
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
      /* private mode: the choice just does not persist */
    }
    syncThemeIcons();
  }

  // The control shows the icon of the theme it would switch *to*.
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
      button.addEventListener("click", () =>
        applyTheme(effectiveTheme() === "dark" ? "light" : "dark")
      );
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
    // Errors linger a little longer so they can be read.
    window.setTimeout(() => el.remove(), kind === "error" ? 7000 : 4000);
  }

  /* --- fetch wrapper ------------------------------------------------------ */

  /**
   * Call the JSON API. Resolves to { ok, status, data } and never throws.
   * A 401 means the session expired -> back to the sign-in page.
   */
  async function api(path, options) {
    const settings = Object.assign({ method: "GET", headers: {} }, options || {});
    const uploadProgress = settings.onUploadProgress;
    delete settings.onUploadProgress;
    settings.headers = Object.assign({ Accept: "application/json" }, settings.headers);

    if (settings.body !== undefined && typeof settings.body !== "string"
        && !(settings.body instanceof FormData) && !(settings.body instanceof Blob)) {
      settings.headers["Content-Type"] = "application/json";
      settings.body = JSON.stringify(settings.body);
    }

    if (uploadProgress && (settings.body instanceof FormData || settings.body instanceof Blob)) {
      return new Promise((resolve) => {
        const xhr = new XMLHttpRequest();
        xhr.open(settings.method, path);
        Object.entries(settings.headers).forEach(([name, value]) => xhr.setRequestHeader(name, value));
        xhr.upload.onprogress = (event) => {
          if (event.lengthComputable) uploadProgress(event.loaded, event.total);
        };
        xhr.onerror = () => resolve({ ok: false, status: 0, data: { error: "Could not reach the server." } });
        xhr.onload = () => {
          let data = {};
          try { data = xhr.responseText ? JSON.parse(xhr.responseText) : {}; } catch (error) { data = {}; }
          if (xhr.status === 401 && !path.startsWith("/auth/api/login")) {
            window.location.href = "/auth/login?next=" + encodeURIComponent(window.location.pathname);
          }
          resolve({ ok: xhr.status >= 200 && xhr.status < 300, status: xhr.status, data });
        };
        xhr.send(settings.body);
      });
    }

    let response;
    try {
      response = await fetch(path, settings);
    } catch (networkError) {
      return { ok: false, status: 0, data: { error: "Could not reach the server." } };
    }

    if (response.status === 401 && !path.startsWith("/auth/api/login")) {
      window.location.href = "/auth/login?next=" + encodeURIComponent(window.location.pathname);
      return { ok: false, status: 401, data: {} };
    }

    let data = {};
    if (response.status !== 204) {
      data = await response.json().catch(() => ({}));
    }
    return { ok: response.ok, status: response.status, data };
  }

  /* --- button busy state -------------------------------------------------- */

  function setBusy(button, busy) {
    if (!button) return;
    if (busy) {
      button.dataset.originalHtml = button.innerHTML;
      button.setAttribute("aria-busy", "true");
      button.disabled = true;
      button.replaceChildren();
      const spinner = document.createElement("span");
      spinner.className = "spinner";
      button.appendChild(spinner);
    } else {
      button.removeAttribute("aria-busy");
      button.disabled = false;
      if (button.dataset.originalHtml) button.innerHTML = button.dataset.originalHtml;
    }
  }

  /* --- formatting --------------------------------------------------------- */

  /** SQLite writes "YYYY-MM-DD HH:MM:SS" in UTC -- make the zone explicit. */
  function parseTimestamp(value) {
    if (!value) return null;
    const date = new Date(String(value).replace(" ", "T") + "Z");
    return isNaN(date.getTime()) ? null : date;
  }

  /** Sortable-looking absolute stamp, e.g. "2026-08-18 16:22". */
  function formatDateTime(value) {
    const date = parseTimestamp(value);
    if (!date) return "—";
    const pad = (n) => String(n).padStart(2, "0");
    return (
      date.getFullYear() +
      "-" + pad(date.getMonth() + 1) +
      "-" + pad(date.getDate()) +
      " " + pad(date.getHours()) +
      ":" + pad(date.getMinutes())
    );
  }

  /** Compact relative age: "12s", "5m", "3h", "2d", else the absolute stamp. */
  function formatAge(value) {
    const date = parseTimestamp(value);
    if (!date) return "—";

    const seconds = Math.max(0, Math.round((Date.now() - date.getTime()) / 1000));
    if (seconds < 60) return seconds + "s ago";
    if (seconds < 3600) return Math.round(seconds / 60) + "m ago";
    if (seconds < 86400) return Math.round(seconds / 3600) + "h ago";
    if (seconds < 604800) return Math.round(seconds / 86400) + "d ago";
    return formatDateTime(value);
  }

  /* --- table cell builders (never innerHTML with server data) -------------- */

  function cell(value, className) {
    const td = document.createElement("td");
    if (className) td.className = className;
    if (value instanceof Node) {
      td.appendChild(value);
    } else {
      const empty = value === null || value === undefined || value === "";
      td.textContent = empty ? "—" : String(value);
      if (empty) td.style.color = "var(--fg-faint)";
    }
    return td;
  }

  // Which lifecycle states count as good / transitional / bad.
  const STATUS_TONE = {
    running: "is-ok",
    ready: "is-ok",
    pending: "is-warn",
    creating: "is-warn",
    stopping: "is-warn",
    deleting: "is-warn",
    error: "is-bad",
    stopped: "is-idle",
    deleted: "is-idle",
  };

  /** Status as a coloured dot plus the word -- no pill, no background fill. */
  function status(value) {
    const wrap = document.createElement("span");
    wrap.className = "status " + (STATUS_TONE[value] || "is-idle");
    const dot = document.createElement("i");
    dot.className = "dot";
    wrap.appendChild(dot);
    wrap.appendChild(document.createTextNode(value));
    return wrap;
  }

  /** Mono tag for identifiers such as a service type. */
  function tag(value) {
    const span = document.createElement("span");
    span.className = "tag";
    span.textContent = value;
    return span;
  }

  /** Thin placeholder bars while a table loads. */
  function renderLoadingRows(tbody, rows, columns) {
    const fragment = document.createDocumentFragment();
    for (let r = 0; r < rows; r++) {
      const tr = document.createElement("tr");
      for (let c = 0; c < columns; c++) {
        const td = document.createElement("td");
        const bar = document.createElement("div");
        bar.className = "bar";
        bar.style.width = 35 + ((r + c) % 4) * 14 + "%";
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
      // The clipboard API needs a secure context (HTTPS or localhost).
      return false;
    }
  }

  document.addEventListener("DOMContentLoaded", initThemeToggle);

  return {
    api,
    toast,
    setBusy,
    cell,
    status,
    tag,
    renderLoadingRows,
    formatDateTime,
    formatAge,
    copyToClipboard,
    applyTheme,
    effectiveTheme,
  };
})();
