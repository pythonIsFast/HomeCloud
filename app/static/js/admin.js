/* ===========================================================================
   Admin view: quota defaults and per-user overrides.
   Only rendered for admins (the section is not in the DOM otherwise), so every
   lookup below is guarded by the presence of the table body.
   =========================================================================== */

(function () {
  "use strict";

  const body = document.getElementById("admin-body");
  if (!body) return; // not an admin

  const empty = document.getElementById("admin-empty");
  const foot = document.getElementById("admin-foot");
  const moreButton = document.getElementById("admin-more");
  const search = document.getElementById("admin-search");
  const updateStatus = document.getElementById("admin-update-status");
  const updateDetail = document.getElementById("admin-update-detail");
  const updateCheck = document.getElementById("admin-update-check");
  const updateRun = document.getElementById("admin-update-run");
  const adminTabs = document.querySelectorAll("[data-admin-tab]");
  const adminPanels = document.querySelectorAll("[data-admin-panel]");

  const FIELDS = ["max_vms", "max_vcpu", "max_memory_mb", "max_disk_gb"];

  const state = { users: [], defaults: null, nextBeforeId: null, query: "" };

  function selectAdminTab(name) {
    adminTabs.forEach((tab) => {
      const active = tab.dataset.adminTab === name;
      tab.toggleAttribute("aria-current", active);
      if (active) tab.setAttribute("aria-current", "page");
      tab.setAttribute("aria-selected", active ? "true" : "false");
    });
    adminPanels.forEach((panel) => panel.classList.toggle("hidden", panel.dataset.adminPanel !== name));
  }

  adminTabs.forEach((tab) => tab.addEventListener("click", () => selectAdminTab(tab.dataset.adminTab)));

  function renderUpdateStatus(data) {
    const check = data.check || {};
    const job = data.job;
    updateRun.disabled = !check.ok || !check.update_available ||
      Boolean(job && ["queued", "running"].includes(job.status));

    if (!check.ok) {
      updateStatus.textContent = "Update check failed";
      updateDetail.textContent = check.error || "Could not inspect the Git origin.";
      return;
    }
    if (job && ["queued", "running"].includes(job.status)) {
      updateStatus.textContent = "Update in progress";
      updateDetail.textContent = "The worker has accepted update job #" + job.id + ". Services may restart briefly.";
      return;
    }
    if (job && job.status === "failed") {
      updateStatus.textContent = "Last update failed";
      updateDetail.textContent = job.error || "The update service reported an error.";
      return;
    }
    updateStatus.textContent = check.update_available ? "Update available" : "HomeCloud is up to date";
    updateDetail.textContent = "Branch " + check.branch + " · current " + check.current.slice(0, 8) +
      " · remote " + check.latest.slice(0, 8);
  }

  async function loadUpdateStatus() {
    const result = await HC.api("/api/admin/update");
    if (!result.ok) {
      updateStatus.textContent = "Update check failed";
      updateDetail.textContent = result.data.error || "HTTP " + result.status;
      return;
    }
    renderUpdateStatus(result.data);
  }

  updateCheck.addEventListener("click", async () => {
    HC.setBusy(updateCheck, true);
    await loadUpdateStatus();
    HC.setBusy(updateCheck, false);
  });

  updateRun.addEventListener("click", async () => {
    if (!window.confirm("Start the HomeCloud update now? Services may restart briefly.")) return;
    HC.setBusy(updateRun, true);
    const result = await HC.api("/api/admin/update", { method: "POST", body: {} });
    HC.setBusy(updateRun, false);
    if (!result.ok) {
      HC.toast("Update could not be queued", result.data.error || "HTTP " + result.status, "error");
      return;
    }
    HC.toast("Update queued", "The privileged worker will apply it shortly", "success");
    await loadUpdateStatus();
  });

  /* --- installation defaults ---------------------------------------------- */

  const DEFAULT_INPUTS = {
    max_vms: "default-max-vms",
    max_vcpu: "default-max-vcpu",
    max_memory_mb: "default-max-memory",
    max_disk_gb: "default-max-disk",
  };

  function renderDefaults() {
    if (!state.defaults) return;
    for (const field of FIELDS) {
      document.getElementById(DEFAULT_INPUTS[field]).value = state.defaults[field];
    }
  }

  document.getElementById("defaults-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = document.getElementById("defaults-submit");

    const payload = {};
    for (const field of FIELDS) {
      payload[field] = Number(document.getElementById(DEFAULT_INPUTS[field]).value);
    }

    HC.setBusy(button, true);
    const result = await HC.api("/api/admin/limits", { method: "PUT", body: payload });
    HC.setBusy(button, false);

    if (!result.ok) {
      HC.toast("Could not save defaults", result.data.error || "HTTP " + result.status, "error");
      return;
    }
    state.defaults = result.data.defaults;
    HC.toast("Defaults saved", "Applies to every user without an override", "success");
    await load(false);
  });

  /* --- per-user rows ------------------------------------------------------ */

  function numberInput(value) {
    const input = document.createElement("input");
    input.className = "input";
    input.type = "number";
    input.min = "0";
    input.value = value;
    return input;
  }

  function usageCell(usage) {
    const span = document.createElement("span");
    span.className = "quota";
    span.textContent =
      usage.vms + " vm · " + usage.vcpu + " cpu · " + usage.memory_mb + " MB · " +
      usage.disk_gb + " GB";
    return span;
  }

  function userRow(user) {
    const row = document.createElement("tr");
    row.appendChild(HC.cell(user.id, "num"));

    const emailCell = HC.cell(user.email, "primary");
    if (user.limits.source === "override") {
      const badge = document.createElement("span");
      badge.className = "badge-override";
      badge.textContent = "override";
      emailCell.appendChild(document.createTextNode(" "));
      emailCell.appendChild(badge);
    }
    row.appendChild(emailCell);

    row.appendChild(HC.cell(HC.tag(user.role)));
    row.appendChild(HC.cell(usageCell(user.usage)));

    // One input per limit; Save sends all four as an override.
    const inputs = {};
    for (const field of FIELDS) {
      inputs[field] = numberInput(user.limits[field]);
      row.appendChild(HC.cell(inputs[field]));
    }

    const actions = document.createElement("div");
    actions.className = "row-actions";

    const save = document.createElement("button");
    save.className = "btn btn-sm";
    save.type = "button";
    save.textContent = "Save";
    save.addEventListener("click", () => saveOverride(user, inputs, save));
    actions.appendChild(save);

    if (user.limits.source === "override") {
      const reset = document.createElement("button");
      reset.className = "btn btn-sm btn-quiet";
      reset.type = "button";
      reset.textContent = "Reset";
      reset.addEventListener("click", () => clearOverride(user, reset));
      actions.appendChild(reset);
    }

    row.appendChild(HC.cell(actions, "right"));
    return row;
  }

  async function saveOverride(user, inputs, button) {
    const payload = {};
    for (const field of FIELDS) payload[field] = Number(inputs[field].value);

    HC.setBusy(button, true);
    const result = await HC.api("/api/admin/limits/" + user.id, {
      method: "PUT",
      body: payload,
    });
    HC.setBusy(button, false);

    if (!result.ok) {
      HC.toast("Could not save", result.data.error || "HTTP " + result.status, "error");
      return;
    }
    HC.toast("Limits updated", user.email, "success");
    await load(false);
  }

  async function clearOverride(user, button) {
    HC.setBusy(button, true);
    const result = await HC.api("/api/admin/limits/" + user.id, { method: "DELETE" });
    HC.setBusy(button, false);

    if (!result.ok) {
      HC.toast("Could not reset", result.data.error || "HTTP " + result.status, "error");
      return;
    }
    HC.toast("Override removed", user.email + " follows the default again", "success");
    await load(false);
  }

  /* --- loading ------------------------------------------------------------ */

  function render() {
    body.replaceChildren(...state.users.map(userRow));
    empty.classList.toggle("hidden", state.users.length > 0);
    foot.textContent = state.users.length + " account(s) shown";
    moreButton.classList.toggle("hidden", !state.nextBeforeId);
  }

  async function load(append) {
    if (!append) HC.renderLoadingRows(body, 3, 9);

    const params = new URLSearchParams();
    if (append && state.nextBeforeId) params.set("before_id", state.nextBeforeId);
    if (state.query) params.set("q", state.query);
    const suffix = params.toString() ? "?" + params.toString() : "";

    const result = await HC.api("/api/admin/limits" + suffix);
    if (!result.ok) {
      if (!append) body.replaceChildren();
      HC.toast("Quotas failed to load", result.data.error || "HTTP " + result.status, "error");
      return;
    }

    state.defaults = result.data.defaults;
    const page = result.data.users || [];
    state.users = append ? state.users.concat(page) : page;
    state.nextBeforeId = result.data.next_before_id || null;

    renderDefaults();
    render();
  }

  moreButton.addEventListener("click", () => load(true));

  // Debounced search so typing does not fire a request per keystroke.
  let searchTimer = null;
  search.addEventListener("input", () => {
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(() => {
      state.query = search.value.trim();
      state.nextBeforeId = null;
      load(false);
    }, 250);
  });

  window.HCViews = window.HCViews || {};
  window.HCViews.admin = { load: () => { loadUpdateStatus(); return load(false); } };
})();
