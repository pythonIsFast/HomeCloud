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

  const state = {
    users: [], defaults: null, nextBeforeId: null, query: "",
    instances: [], instanceNextBeforeId: null, flavors: [], settings: null,
  };

  function selectAdminTab(name) {
    adminTabs.forEach((tab) => {
      const active = tab.dataset.adminTab === name;
      tab.toggleAttribute("aria-current", active);
      if (active) tab.setAttribute("aria-current", "page");
      tab.setAttribute("aria-selected", active ? "true" : "false");
    });
    adminPanels.forEach((panel) => panel.classList.toggle("hidden", panel.dataset.adminPanel !== name));
    if (name === "instances") loadInstances(false);
    if (name === "flavors") loadFlavors();
    if (name === "policy") loadPolicy();
    if (name === "updates") loadUpdateStatus();
  }

  adminTabs.forEach((tab) => tab.addEventListener("click", () => selectAdminTab(tab.dataset.adminTab)));

  let updateTimer = null;
  let updatePolling = false;

  function scheduleUpdateRefresh(delay) {
    if (updateTimer) window.clearTimeout(updateTimer);
    if (!updatePolling) return;
    updateTimer = window.setTimeout(async () => {
      updateTimer = null;
      await loadUpdateStatus();
    }, delay || 5000);
  }

  function renderUpdateStatus(data) {
    const check = data.check || {};
    const job = data.job;
    const runtime = data.runtime || { state: "idle" };
    const running = ["starting", "running"].includes(runtime.state) ||
      Boolean(job && ["queued", "running"].includes(job.status));
    updateRun.disabled = running || !check.ok || !check.update_available;

    if (running) {
      updatePolling = true;
      updateStatus.textContent = "Update in progress";
      updateDetail.textContent = runtime.message ||
        (job && job.id
          ? "The worker has accepted update job #" + job.id + ". Services may restart briefly."
          : "The update service is starting. Services may restart briefly.");
      scheduleUpdateRefresh();
      return;
    }

    // A successful Git comparison is newer evidence than a historical queue
    // failure (including the old synchronous systemctl timeout bug).
    if (check.ok && !check.update_available) {
      updatePolling = false;
      updateStatus.textContent = "HomeCloud is up to date";
      updateDetail.textContent = "Branch " + check.branch + " · current " + check.current.slice(0, 8) +
        " · remote " + check.latest.slice(0, 8);
      return;
    }

    if (runtime.state === "failed") {
      updatePolling = false;
      updateStatus.textContent = "Last update failed";
      updateDetail.textContent = runtime.message || "The update script reported an error.";
      return;
    }

    if (job && job.status === "failed") {
      updatePolling = false;
      updateStatus.textContent = "Last update failed";
      updateDetail.textContent = job.error || "The update service reported an error.";
      return;
    }

    if (!check.ok) {
      updatePolling = false;
      updateStatus.textContent = "Update check failed";
      updateDetail.textContent = check.error || "Could not inspect the Git origin.";
      return;
    }

    updatePolling = false;
    updateStatus.textContent = "Update available";
    updateDetail.textContent = "Branch " + check.branch + " · current " + check.current.slice(0, 8) +
      " · remote " + check.latest.slice(0, 8);
  }

  async function loadUpdateStatus() {
    const result = await HC.api("/api/admin/update");
    if (!result.ok) {
      updateRun.disabled = true;
      if (updatePolling) {
        updateStatus.textContent = "HomeCloud is restarting";
        updateDetail.textContent = "Waiting for the web service to become available again.";
        scheduleUpdateRefresh();
      } else {
        updateStatus.textContent = "Update check failed";
        updateDetail.textContent = result.data.error || "HTTP " + result.status;
      }
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
      await loadUpdateStatus();
      return;
    }
    updatePolling = true;
    updateStatus.textContent = "Update queued";
    updateDetail.textContent = "Waiting for the privileged worker to start the update service.";
    updateRun.disabled = true;
    HC.toast("Update queued", "The privileged worker will apply it shortly", "success");
    await loadUpdateStatus();
  });

  /* --- platform policy ---------------------------------------------------- */

  const POLICY_INPUTS = {
    allow_registration: "policy-registration",
    jwt_ttl_hours: "policy-jwt",
    image_upload_max_mb: "policy-upload",
    max_images_per_user: "policy-images",
    auth_rate_limit: "policy-auth-rate",
    auth_rate_window_seconds: "policy-auth-window",
    write_rate_limit: "policy-write-rate",
    api_rate_limit: "policy-api-rate",
  };

  function renderPolicy(data) {
    state.settings = data.settings;
    for (const [key, id] of Object.entries(POLICY_INPUTS)) {
      const input = document.getElementById(id);
      if (input.type === "checkbox") input.checked = Boolean(data.settings[key]);
      else input.value = data.settings[key];
    }
    document.getElementById("policy-upload").max = data.host.upload_ceiling_mb;
    const facts = document.getElementById("admin-host-facts");
    facts.replaceChildren();
    const labels = {
      vm_subnet_prefix: "VM subnet prefix", vm_egress_if: "Egress interface",
      vm_kernel: "Guest kernel", base_image: "Base image", upload_ceiling_mb: "Host upload ceiling (MiB)",
    };
    for (const [key, label] of Object.entries(labels)) {
      const term = document.createElement("dt");
      term.textContent = label;
      const value = document.createElement("dd");
      value.textContent = data.host[key];
      facts.append(term, value);
    }
  }

  async function loadPolicy() {
    const result = await HC.api("/api/admin/settings");
    if (!result.ok) {
      HC.toast("Platform policy failed to load", result.data.error || "HTTP " + result.status, "error");
      return;
    }
    renderPolicy(result.data);
  }

  document.getElementById("admin-policy-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const payload = {};
    for (const [key, id] of Object.entries(POLICY_INPUTS)) {
      const input = document.getElementById(id);
      payload[key] = input.type === "checkbox" ? input.checked : Number(input.value);
    }
    const button = document.getElementById("policy-save");
    HC.setBusy(button, true);
    const result = await HC.api("/api/admin/settings", { method: "PUT", body: payload });
    HC.setBusy(button, false);
    if (!result.ok) {
      HC.toast("Could not save platform policy", result.data.error || "HTTP " + result.status, "error");
      return;
    }
    state.settings = result.data.settings;
    HC.toast("Platform policy saved", "New requests use the new policy immediately", "success");
    await loadPolicy();
  });

  /* --- instance type catalogue ------------------------------------------- */

  function flavorNumber(value) {
    const input = numberInput(value);
    input.min = "1";
    return input;
  }

  function flavorRow(flavor) {
    const row = document.createElement("tr");
    row.appendChild(HC.cell(flavor.name, "primary"));
    const vcpu = flavorNumber(flavor.vcpu);
    const memory = flavorNumber(flavor.memory_mb);
    const disk = flavorNumber(flavor.disk_gb);
    row.append(HC.cell(vcpu), HC.cell(memory), HC.cell(disk));
    const enabled = document.createElement("input");
    enabled.type = "checkbox";
    enabled.checked = Boolean(flavor.enabled);
    enabled.setAttribute("aria-label", "Make " + flavor.name + " available");
    const isDefault = document.createElement("input");
    isDefault.type = "radio";
    isDefault.name = "admin-default-flavor";
    isDefault.checked = Boolean(flavor.is_default);
    isDefault.setAttribute("aria-label", "Make " + flavor.name + " the default");
    row.append(HC.cell(enabled), HC.cell(isDefault));
    const save = document.createElement("button");
    save.className = "btn btn-sm";
    save.type = "button";
    save.textContent = "Save";
    save.addEventListener("click", async () => {
      const payload = { vcpu: Number(vcpu.value), memory_mb: Number(memory.value),
        disk_gb: Number(disk.value), enabled: enabled.checked, is_default: isDefault.checked };
      HC.setBusy(save, true);
      const result = await HC.api("/api/admin/flavors/" + encodeURIComponent(flavor.name), {
        method: "PUT", body: payload,
      });
      HC.setBusy(save, false);
      if (!result.ok) {
        HC.toast("Could not save instance type", result.data.error || "HTTP " + result.status, "error");
        return;
      }
      HC.toast("Instance type saved", flavor.name, "success");
      await loadFlavors();
    });
    row.appendChild(HC.cell(save, "right"));
    return row;
  }

  async function loadFlavors() {
    const result = await HC.api("/api/admin/flavors");
    if (!result.ok) {
      HC.toast("Instance types failed to load", result.data.error || "HTTP " + result.status, "error");
      return;
    }
    state.flavors = result.data.flavors || [];
    document.getElementById("admin-flavors-body").replaceChildren(...state.flavors.map(flavorRow));
  }

  document.getElementById("admin-flavor-create-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = document.getElementById("admin-flavor-create");
    const payload = {
      name: document.getElementById("admin-flavor-name").value.trim(),
      vcpu: Number(document.getElementById("admin-flavor-vcpu").value),
      memory_mb: Number(document.getElementById("admin-flavor-memory").value),
      disk_gb: Number(document.getElementById("admin-flavor-disk").value),
      enabled: true,
    };
    HC.setBusy(button, true);
    const result = await HC.api("/api/admin/flavors", { method: "POST", body: payload });
    HC.setBusy(button, false);
    if (!result.ok) {
      HC.toast("Could not add instance type", result.data.error || "HTTP " + result.status, "error");
      return;
    }
    event.target.reset();
    HC.toast("Instance type added", payload.name, "success");
    await loadFlavors();
  });

  /* --- every compute instance -------------------------------------------- */

  const instancesBody = document.getElementById("admin-instances-body");
  const instancesEmpty = document.getElementById("admin-instances-empty");
  const instancesFoot = document.getElementById("admin-instances-foot");
  const instancesMore = document.getElementById("admin-instances-more");

  function instanceRow(instance) {
    const row = document.createElement("tr");
    row.append(HC.cell(instance.id, "num"), HC.cell(instance.name, "primary"),
      HC.cell(instance.owner.email), HC.cell(HC.status(instance.status)),
      HC.cell(instance.flavor || "—"), HC.cell(instance.ip || "—", "mono"));
    const actions = document.createElement("div");
    actions.className = "row-actions";
    const open = document.createElement("button");
    open.className = "btn btn-sm";
    open.type = "button";
    open.textContent = "Open";
    open.addEventListener("click", () => { window.location.hash = "vm/" + instance.id; });
    actions.appendChild(open);
    const action = instance.status === "running" ? "stop" : "start";
    if (["running", "stopped", "error"].includes(instance.status)) {
      const control = document.createElement("button");
      control.className = "btn btn-sm btn-quiet";
      control.type = "button";
      control.textContent = action[0].toUpperCase() + action.slice(1);
      control.addEventListener("click", async () => {
        HC.setBusy(control, true);
        const result = await HC.api("/compute/api/instances/" + instance.id + "/actions/" + action,
          { method: "POST", body: {} });
        HC.setBusy(control, false);
        if (!result.ok) {
          HC.toast("Instance action failed", result.data.error || "HTTP " + result.status, "error");
          return;
        }
        HC.toast("Instance action queued", instance.name + " will " + action, "success");
        await loadInstances(false);
      });
      actions.appendChild(control);
    }
    row.appendChild(HC.cell(actions, "right"));
    return row;
  }

  function renderInstances() {
    instancesBody.replaceChildren(...state.instances.map(instanceRow));
    instancesEmpty.classList.toggle("hidden", state.instances.length > 0);
    instancesFoot.textContent = state.instances.length + " instance(s) shown";
    instancesMore.classList.toggle("hidden", !state.instanceNextBeforeId);
  }

  async function loadInstances(append) {
    if (!append) HC.renderLoadingRows(instancesBody, 3, 7);
    const suffix = append && state.instanceNextBeforeId ? "?before_id=" + state.instanceNextBeforeId : "";
    const result = await HC.api("/api/admin/instances" + suffix);
    if (!result.ok) {
      if (!append) instancesBody.replaceChildren();
      HC.toast("Instances failed to load", result.data.error || "HTTP " + result.status, "error");
      return;
    }
    const page = result.data.instances || [];
    state.instances = append ? state.instances.concat(page) : page;
    state.instanceNextBeforeId = result.data.next_before_id || null;
    renderInstances();
  }

  document.getElementById("admin-instances-refresh").addEventListener("click", () => loadInstances(false));
  instancesMore.addEventListener("click", () => loadInstances(true));

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
