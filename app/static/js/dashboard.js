/* ===========================================================================
   HomeCloud console.
   One document, four client-side views (overview / resources / activity /
   keys) switched by URL hash. No router library, no build step. All data
   comes from the JSON API via HC.api().
   =========================================================================== */

(function () {
  "use strict";

  // Hash -> breadcrumb label. The keys double as the valid view names.
  const VIEWS = {
    overview: "overview",
    resources: "resources",
    activity: "activity",
    keys: "api-keys",
  };

  // Last responses, so switching views or typing in the filter costs no request.
  const state = { resources: [], keys: [], activity: [], view: "overview" };

  /* =========================================================================
     Views
     ====================================================================== */

  function showView(name) {
    const view = VIEWS[name] ? name : "overview";
    state.view = view;

    document.querySelectorAll(".view").forEach((section) => {
      section.hidden = section.dataset.view !== view;
    });

    document.querySelectorAll(".nav-item[data-view]").forEach((item) => {
      if (item.dataset.view === view) {
        item.setAttribute("aria-current", "page");
      } else {
        item.removeAttribute("aria-current");
      }
    });

    document.getElementById("breadcrumb-current").textContent = VIEWS[view];
    document.title = VIEWS[view] + " · HomeCloud";
    closeNav();
    closeUserMenu();
  }

  function navigate(name) {
    // Setting the hash routes through the hashchange handler below.
    if (window.location.hash === "#" + name) {
      showView(name);
    } else {
      window.location.hash = name;
    }
  }

  document.querySelectorAll("[data-view]").forEach((element) => {
    if (element.classList.contains("view")) return;
    if (element.getAttribute("aria-disabled") === "true") return;
    element.addEventListener("click", () => navigate(element.dataset.view));
  });

  window.addEventListener("hashchange", () =>
    showView(window.location.hash.replace("#", ""))
  );

  /* =========================================================================
     Off-canvas navigation and the account menu
     ====================================================================== */

  const shell = document.getElementById("shell");
  const navToggle = document.getElementById("sidebar-toggle");

  function closeNav() {
    shell.classList.remove("nav-open");
    navToggle.setAttribute("aria-expanded", "false");
  }

  navToggle.addEventListener("click", () => {
    const open = shell.classList.toggle("nav-open");
    navToggle.setAttribute("aria-expanded", open ? "true" : "false");
  });

  const userMenu = document.getElementById("user-menu");
  const userMenuTrigger = document.getElementById("user-menu-trigger");

  function closeUserMenu() {
    userMenu.classList.add("hidden");
    userMenuTrigger.setAttribute("aria-expanded", "false");
  }

  userMenuTrigger.addEventListener("click", (event) => {
    event.stopPropagation();
    const open = !userMenu.classList.toggle("hidden");
    userMenuTrigger.setAttribute("aria-expanded", open ? "true" : "false");
  });

  document.addEventListener("click", (event) => {
    if (!userMenu.contains(event.target) && !userMenuTrigger.contains(event.target)) {
      closeUserMenu();
    }
  });

  /* =========================================================================
     Keyboard: "/" jumps to the resource filter, Escape closes things
     ====================================================================== */

  const resourceSearch = document.getElementById("resource-search");

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closeUserMenu();
      closeNav();
      closeKeyModal();
      return;
    }

    const inField = /^(INPUT|SELECT|TEXTAREA)$/.test(event.target.tagName);
    if (event.key === "/" && !inField && !event.metaKey && !event.ctrlKey) {
      event.preventDefault();
      navigate("resources");
      resourceSearch.focus();
      resourceSearch.select();
    }
  });

  /* =========================================================================
     Resources
     ====================================================================== */

  const resourcesBody = document.getElementById("resources-body");
  const resourcesEmpty = document.getElementById("resources-empty");
  const resourcesNoMatch = document.getElementById("resources-no-match");
  const resourcesFoot = document.getElementById("resources-foot");
  const serviceFilter = document.getElementById("service-filter");

  function resourceRow(resource) {
    const row = document.createElement("tr");
    row.appendChild(HC.cell(resource.id, "num"));
    row.appendChild(HC.cell(resource.name, "primary"));
    row.appendChild(HC.cell(HC.tag(resource.service_type)));
    row.appendChild(HC.cell(HC.status(resource.status)));
    row.appendChild(HC.cell(HC.formatDateTime(resource.created_at), "mono"));
    row.appendChild(HC.cell(HC.formatAge(resource.updated_at), "mono"));
    return row;
  }

  function visibleResources() {
    const term = resourceSearch.value.trim().toLowerCase();
    const service = serviceFilter.value;

    return state.resources.filter((resource) => {
      if (service && resource.service_type !== service) return false;
      if (!term) return true;
      return (
        resource.name.toLowerCase().includes(term) ||
        resource.status.toLowerCase().includes(term) ||
        resource.service_type.toLowerCase().includes(term)
      );
    });
  }

  function renderResources() {
    const rows = visibleResources();
    const hasAny = state.resources.length > 0;

    resourcesBody.replaceChildren(...rows.map(resourceRow));
    resourcesEmpty.classList.toggle("hidden", hasAny);
    resourcesNoMatch.classList.toggle("hidden", !hasAny || rows.length > 0);

    resourcesFoot.textContent = hasAny
      ? rows.length + " / " + state.resources.length + " rows"
      : "0 rows";

    document.getElementById("nav-count-resources").textContent = state.resources.length;
  }

  // Keep the type filter in sync with the service types actually present.
  function renderServiceFilter() {
    const selected = serviceFilter.value;
    const types = [...new Set(state.resources.map((r) => r.service_type))].sort();

    const options = [new Option("All types", "")];
    types.forEach((type) => options.push(new Option(type, type)));
    serviceFilter.replaceChildren(...options);
    serviceFilter.value = types.includes(selected) ? selected : "";
  }

  async function loadResources() {
    HC.renderLoadingRows(resourcesBody, 3, 6);
    const result = await HC.api("/api/resources");
    if (!result.ok) {
      resourcesBody.replaceChildren();
      HC.toast("Resources failed to load", result.data.error || "HTTP " + result.status, "error");
      return;
    }
    state.resources = result.data.resources || [];
    renderServiceFilter();
    renderResources();
    renderMetrics();
  }

  resourceSearch.addEventListener("input", renderResources);
  serviceFilter.addEventListener("change", renderResources);

  /* =========================================================================
     Overview metrics
     ====================================================================== */

  function setMetric(id, value) {
    const el = document.getElementById(id);
    el.textContent = value;
    el.classList.toggle("is-zero", value === 0);
  }

  function renderMetrics() {
    const resources = state.resources;
    const running = resources.filter((r) => r.status === "running").length;
    const unsettled = resources.filter((r) =>
      ["pending", "creating", "error"].includes(r.status)
    ).length;
    const types = new Set(resources.map((r) => r.service_type)).size;

    setMetric("stat-total", resources.length);
    setMetric("stat-running", running);
    setMetric("stat-attention", unsettled);
    setMetric("stat-keys", state.keys.length);

    document.getElementById("stat-total-hint").textContent =
      types === 0 ? "no service registered" : types + (types === 1 ? " type" : " types");
  }

  /* =========================================================================
     Activity
     ====================================================================== */

  /** Turn details_json into a compact "key=value" line instead of raw JSON. */
  function summarizeDetails(entry) {
    let details;
    try {
      details = JSON.parse(entry.details_json || "{}");
    } catch (e) {
      details = {};
    }
    const parts = Object.keys(details).map((key) => key + "=" + details[key]);
    return parts.length ? parts.join(" ") : "";
  }

  function eventName(action) {
    const span = document.createElement("span");
    span.className = "status " + (action.endsWith("_failed") ? "is-bad" : "is-idle");
    const dot = document.createElement("i");
    dot.className = "dot";
    span.appendChild(dot);
    const text = document.createElement("span");
    text.className = "mono";
    text.textContent = action;
    span.appendChild(text);
    return span;
  }

  function activityRow(entry) {
    const row = document.createElement("tr");
    row.appendChild(HC.cell(entry.id, "num"));
    row.appendChild(HC.cell(eventName(entry.action)));
    row.appendChild(HC.cell(entry.resource_id, "num"));
    row.appendChild(HC.cell(summarizeDetails(entry), "mono"));
    row.appendChild(HC.cell(HC.formatAge(entry.created_at), "mono"));
    return row;
  }

  function renderOverviewActivity() {
    const body = document.getElementById("overview-activity-body");
    const empty = document.getElementById("overview-activity-empty");
    const recent = state.activity.slice(0, 6);

    body.replaceChildren(
      ...recent.map((entry) => {
        const row = document.createElement("tr");
        row.appendChild(HC.cell(eventName(entry.action)));
        row.appendChild(HC.cell(summarizeDetails(entry), "mono"));
        row.appendChild(HC.cell(HC.formatAge(entry.created_at), "mono"));
        return row;
      })
    );
    empty.classList.toggle("hidden", recent.length > 0);
  }

  async function loadActivity() {
    const body = document.getElementById("activity-body");
    HC.renderLoadingRows(body, 4, 5);

    const result = await HC.api("/api/audit");
    if (!result.ok) {
      body.replaceChildren();
      HC.toast("Activity failed to load", result.data.error || "HTTP " + result.status, "error");
      return;
    }

    state.activity = result.data.entries || [];
    body.replaceChildren(...state.activity.map(activityRow));
    document
      .getElementById("activity-empty")
      .classList.toggle("hidden", state.activity.length > 0);
    renderOverviewActivity();
  }

  /* =========================================================================
     API keys
     ====================================================================== */

  const keysBody = document.getElementById("keys-body");
  const keysEmpty = document.getElementById("keys-empty");

  function keyRow(key) {
    const row = document.createElement("tr");
    row.appendChild(HC.cell(key.id, "num"));
    row.appendChild(HC.cell(key.label || "", "primary"));
    row.appendChild(HC.cell(HC.formatDateTime(key.created_at), "mono"));
    row.appendChild(HC.cell(key.last_used_at ? HC.formatAge(key.last_used_at) : "never", "mono"));

    const revoke = document.createElement("button");
    revoke.className = "btn btn-danger";
    revoke.type = "button";
    revoke.textContent = "Revoke";
    revoke.addEventListener("click", () => revokeKey(key, revoke));
    row.appendChild(HC.cell(revoke, "right"));
    return row;
  }

  function renderKeys() {
    keysBody.replaceChildren(...state.keys.map(keyRow));
    keysEmpty.classList.toggle("hidden", state.keys.length > 0);
    document.getElementById("nav-count-keys").textContent = state.keys.length;
    setMetric("stat-keys", state.keys.length);
  }

  async function loadKeys() {
    HC.renderLoadingRows(keysBody, 2, 5);
    const result = await HC.api("/auth/api/keys");
    if (!result.ok) {
      keysBody.replaceChildren();
      HC.toast("API keys failed to load", result.data.error || "HTTP " + result.status, "error");
      return;
    }
    state.keys = result.data.keys || [];
    renderKeys();
  }

  async function revokeKey(key, button) {
    const name = key.label || "key " + key.id;
    if (!window.confirm("Revoke " + name + "? Clients using it stop working immediately.")) {
      return;
    }

    HC.setBusy(button, true);
    const result = await HC.api("/auth/api/keys/" + key.id, { method: "DELETE" });
    if (!result.ok) {
      HC.setBusy(button, false);
      HC.toast("Revoke failed", result.data.error || "HTTP " + result.status, "error");
      return;
    }

    HC.toast("Key revoked", name, "success");
    await Promise.all([loadKeys(), loadActivity()]);
  }

  document.getElementById("create-key-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const labelInput = document.getElementById("key-label");
    const button = document.getElementById("create-key-submit");

    HC.setBusy(button, true);
    const result = await HC.api("/auth/api/keys", {
      method: "POST",
      body: { label: labelInput.value.trim() },
    });
    HC.setBusy(button, false);

    if (!result.ok) {
      HC.toast("Create failed", result.data.error || "HTTP " + result.status, "error");
      return;
    }

    labelInput.value = "";
    openKeyModal(result.data.key);
    await Promise.all([loadKeys(), loadActivity()]);
  });

  /* --- the one-time secret dialog ---------------------------------------- */

  const keyModal = document.getElementById("key-modal");
  const keyModalSecret = document.getElementById("key-modal-secret");

  function openKeyModal(secret) {
    keyModalSecret.textContent = secret;
    keyModal.classList.remove("hidden");
    document.getElementById("key-modal-copy").focus();
  }

  function closeKeyModal() {
    keyModal.classList.add("hidden");
    keyModalSecret.textContent = "";
  }

  document.getElementById("key-modal-close").addEventListener("click", closeKeyModal);
  keyModal.addEventListener("click", (event) => {
    if (event.target === keyModal) closeKeyModal();
  });

  document.getElementById("key-modal-copy").addEventListener("click", async () => {
    const copied = await HC.copyToClipboard(keyModalSecret.textContent);
    HC.toast(
      copied ? "Copied" : "Copy unavailable",
      copied ? null : "Select the key and copy it manually — the clipboard API needs HTTPS.",
      copied ? "success" : "error"
    );
  });

  /* =========================================================================
     Sign out, refresh, boot
     ====================================================================== */

  document.getElementById("logout-button").addEventListener("click", async () => {
    await HC.api("/auth/api/logout", { method: "POST" });
    window.location.href = "/auth/login";
  });

  document.querySelectorAll('[data-action="reload"]').forEach((button) => {
    button.addEventListener("click", () => reloadAll(button));
  });

  async function reloadAll(button) {
    HC.setBusy(button, true);
    await Promise.all([loadResources(), loadKeys(), loadActivity()]);
    HC.setBusy(button, false);
  }

  showView(window.location.hash.replace("#", ""));
  Promise.all([loadResources(), loadKeys(), loadActivity()]);
})();
