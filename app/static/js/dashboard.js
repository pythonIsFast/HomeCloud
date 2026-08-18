/* ===========================================================================
   HomeCloud console.
   One page with four client-side views (overview / resources / keys /
   activity) switched via the URL hash -- no router library, no build step.
   All data comes from the JSON API through HC.api().
   =========================================================================== */

(function () {
  "use strict";

  const VIEWS = {
    overview: "Overview",
    resources: "Resources",
    keys: "API keys",
    activity: "Activity",
  };

  // Client-side cache of the last responses, so switching views and typing in
  // the search box do not trigger a request each time.
  const state = { resources: [], keys: [], activity: [], view: "overview" };

  /* =========================================================================
     View switching
     ====================================================================== */

  function showView(name) {
    const view = VIEWS[name] ? name : "overview";
    state.view = view;

    document.querySelectorAll(".view").forEach((section) => {
      section.hidden = section.dataset.view !== view;
    });
    document.querySelectorAll(".nav-item[data-view]").forEach((item) => {
      if (item.getAttribute("aria-disabled") === "true") return;
      if (item.dataset.view === view) {
        item.setAttribute("aria-current", "page");
      } else {
        item.removeAttribute("aria-current");
      }
    });

    document.getElementById("breadcrumb-current").textContent = VIEWS[view];
    document.title = VIEWS[view] + " · HomeCloud";
    closeSidebar();
    closeUserMenu();
  }

  function navigate(name) {
    // Writing the hash triggers showView() through the hashchange handler.
    if (window.location.hash === "#" + name) {
      showView(name);
    } else {
      window.location.hash = name;
    }
  }

  document.querySelectorAll("[data-view]").forEach((element) => {
    if (!element.dataset.view || element.classList.contains("view")) return;
    if (element.getAttribute("aria-disabled") === "true") return;
    element.addEventListener("click", () => navigate(element.dataset.view));
  });

  window.addEventListener("hashchange", () => {
    showView(window.location.hash.replace("#", ""));
  });

  /* =========================================================================
     Sidebar (mobile) and user menu
     ====================================================================== */

  const shell = document.getElementById("shell");
  const sidebarToggle = document.getElementById("sidebar-toggle");

  function closeSidebar() {
    shell.classList.remove("sidebar-open");
    sidebarToggle.setAttribute("aria-expanded", "false");
  }

  sidebarToggle.addEventListener("click", () => {
    const open = shell.classList.toggle("sidebar-open");
    sidebarToggle.setAttribute("aria-expanded", open ? "true" : "false");
  });

  const userMenu = document.getElementById("user-menu");
  const userMenuTrigger = document.getElementById("user-menu-trigger");

  function closeUserMenu() {
    userMenu.classList.add("hidden");
    userMenuTrigger.setAttribute("aria-expanded", "false");
  }

  userMenuTrigger.addEventListener("click", (event) => {
    event.stopPropagation();
    const open = userMenu.classList.toggle("hidden") === false;
    userMenuTrigger.setAttribute("aria-expanded", open ? "true" : "false");
  });

  document.addEventListener("click", (event) => {
    if (!userMenu.contains(event.target) && !userMenuTrigger.contains(event.target)) {
      closeUserMenu();
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closeUserMenu();
      closeSidebar();
      closeKeyModal();
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
  const resourceSearch = document.getElementById("resource-search");

  function resourceRow(resource) {
    const row = document.createElement("tr");
    row.appendChild(HC.cell(resource.id, "num"));
    row.appendChild(HC.cell(resource.name, "cell-strong"));

    const service = document.createElement("span");
    service.className = "chip";
    service.textContent = resource.service_type;
    row.appendChild(HC.cell(service));

    row.appendChild(HC.cell(HC.statusBadge(resource.status)));
    row.appendChild(HC.cell(HC.formatDateTime(resource.created_at)));
    row.appendChild(HC.cell(HC.formatRelative(resource.updated_at)));
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
      ? rows.length + " of " + state.resources.length + " resources shown"
      : " ";

    document.getElementById("nav-count-resources").textContent = state.resources.length;
  }

  // Keep the service dropdown in sync with the types actually present.
  function renderServiceFilter() {
    const selected = serviceFilter.value;
    const types = [...new Set(state.resources.map((r) => r.service_type))].sort();

    const options = [new Option("All services", "")];
    types.forEach((type) => options.push(new Option(type, type)));
    serviceFilter.replaceChildren(...options);
    serviceFilter.value = types.includes(selected) ? selected : "";
  }

  async function loadResources() {
    HC.renderSkeletonRows(resourcesBody, 3, 6);
    const result = await HC.api("/api/resources");
    if (!result.ok) {
      resourcesBody.replaceChildren();
      HC.toast("Could not load resources", result.data.error || "HTTP " + result.status, "error");
      return;
    }
    state.resources = result.data.resources || [];
    renderServiceFilter();
    renderResources();
    renderOverviewStats();
  }

  resourceSearch.addEventListener("input", renderResources);
  serviceFilter.addEventListener("change", renderResources);

  /* =========================================================================
     Overview
     ====================================================================== */

  function renderOverviewStats() {
    const resources = state.resources;
    const running = resources.filter((r) => r.status === "running").length;
    const attention = resources.filter((r) =>
      ["pending", "creating", "error"].includes(r.status)
    ).length;
    const services = new Set(resources.map((r) => r.service_type)).size;

    document.getElementById("stat-total").textContent = resources.length;
    document.getElementById("stat-running").textContent = running;
    document.getElementById("stat-attention").textContent = attention;
    document.getElementById("stat-keys").textContent = state.keys.length;
    document.getElementById("stat-total-hint").textContent =
      services === 0
        ? "no service installed yet"
        : "across " + services + (services === 1 ? " service" : " services");
  }

  function renderOverviewActivity() {
    const body = document.getElementById("overview-activity-body");
    const empty = document.getElementById("overview-activity-empty");
    const recent = state.activity.slice(0, 5);

    body.replaceChildren(
      ...recent.map((entry) => {
        const row = document.createElement("tr");
        row.appendChild(HC.cell(actionBadge(entry.action)));
        row.appendChild(HC.cell(summarizeDetails(entry)));
        row.appendChild(HC.cell(HC.formatRelative(entry.created_at)));
        return row;
      })
    );
    empty.classList.toggle("hidden", recent.length > 0);
  }

  /* =========================================================================
     Activity
     ====================================================================== */

  // Colour the pill by what kind of event it is.
  function actionBadge(action) {
    const span = document.createElement("span");
    let kind = "plain";
    if (action.endsWith("_failed") || action.includes("delete")) kind = "error";
    else if (action.includes("create") || action.includes("register")) kind = "running";
    else if (action.includes("login") || action.includes("logout")) kind = "info";
    span.className = "badge badge-" + kind;
    span.textContent = action;
    return span;
  }

  /** Turn details_json into a compact "key=value" line, without dumping JSON. */
  function summarizeDetails(entry) {
    let details;
    try {
      details = JSON.parse(entry.details_json || "{}");
    } catch (e) {
      details = {};
    }
    const parts = Object.keys(details).map((key) => key + "=" + details[key]);
    return parts.length ? parts.join(" · ") : "–";
  }

  function activityRow(entry) {
    const row = document.createElement("tr");
    row.appendChild(HC.cell(entry.id, "num"));
    row.appendChild(HC.cell(actionBadge(entry.action)));
    row.appendChild(HC.cell(entry.resource_id, "num"));
    row.appendChild(HC.cell(summarizeDetails(entry)));
    row.appendChild(HC.cell(HC.formatRelative(entry.created_at)));
    return row;
  }

  async function loadActivity() {
    const body = document.getElementById("activity-body");
    HC.renderSkeletonRows(body, 4, 5);

    const result = await HC.api("/api/audit");
    if (!result.ok) {
      body.replaceChildren();
      HC.toast("Could not load activity", result.data.error || "HTTP " + result.status, "error");
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
    row.appendChild(HC.cell(key.label || "(no label)", "cell-strong"));
    row.appendChild(HC.cell(HC.formatDateTime(key.created_at)));
    row.appendChild(HC.cell(key.last_used_at ? HC.formatRelative(key.last_used_at) : "never used"));

    const remove = document.createElement("button");
    remove.className = "btn btn-sm btn-danger";
    remove.type = "button";
    remove.innerHTML = '<svg><use href="#i-trash"></use></svg> Revoke';
    remove.addEventListener("click", () => revokeKey(key, remove));
    row.appendChild(HC.cell(remove, "cell-actions"));
    return row;
  }

  function renderKeys() {
    keysBody.replaceChildren(...state.keys.map(keyRow));
    keysEmpty.classList.toggle("hidden", state.keys.length > 0);
    document.getElementById("nav-count-keys").textContent = state.keys.length;
    document.getElementById("stat-keys").textContent = state.keys.length;
  }

  async function loadKeys() {
    HC.renderSkeletonRows(keysBody, 2, 5);
    const result = await HC.api("/auth/api/keys");
    if (!result.ok) {
      keysBody.replaceChildren();
      HC.toast("Could not load API keys", result.data.error || "HTTP " + result.status, "error");
      return;
    }
    state.keys = result.data.keys || [];
    renderKeys();
  }

  async function revokeKey(key, button) {
    const name = key.label || "key #" + key.id;
    if (!window.confirm("Revoke " + name + '? Any client using it stops working immediately.')) {
      return;
    }

    HC.setBusy(button, true);
    const result = await HC.api("/auth/api/keys/" + key.id, { method: "DELETE" });
    if (!result.ok) {
      HC.setBusy(button, false);
      HC.toast("Could not revoke key", result.data.error || "HTTP " + result.status, "error");
      return;
    }

    HC.toast("Key revoked", name, "success");
    await Promise.all([loadKeys(), loadActivity()]);
  }

  document.getElementById("create-key-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const labelInput = document.getElementById("key-label");
    const button = document.getElementById("create-key-submit");

    HC.setBusy(button, true, "Creating");
    const result = await HC.api("/auth/api/keys", {
      method: "POST",
      body: { label: labelInput.value.trim() },
    });
    HC.setBusy(button, false);

    if (!result.ok) {
      HC.toast("Could not create key", result.data.error || "HTTP " + result.status, "error");
      return;
    }

    labelInput.value = "";
    openKeyModal(result.data.key);
    await Promise.all([loadKeys(), loadActivity()]);
  });

  /* --- modal with the freshly created secret ------------------------------ */

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
      copied ? "Copied to clipboard" : "Copy failed",
      copied ? null : "Select the key and copy it manually (clipboard access needs HTTPS).",
      copied ? "success" : "error"
    );
  });

  /* =========================================================================
     Logout, refresh, boot
     ====================================================================== */

  document.getElementById("logout-button").addEventListener("click", async () => {
    await HC.api("/auth/api/logout", { method: "POST" });
    window.location.href = "/auth/login";
  });

  document.querySelectorAll('[data-action="reload"]').forEach((button) => {
    button.addEventListener("click", () => reloadAll(button));
  });

  async function reloadAll(button) {
    HC.setBusy(button, true, "Refreshing");
    await Promise.all([loadResources(), loadKeys(), loadActivity()]);
    HC.setBusy(button, false);
  }

  showView(window.location.hash.replace("#", ""));
  Promise.all([loadResources(), loadKeys(), loadActivity()]);
})();
