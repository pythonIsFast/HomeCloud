/* ===========================================================================
   Compute view: Firecracker microVMs.
   Loaded after dashboard.js and registers itself with the shell through
   window.HCViews, so dashboard.js does not need to know about services.
   =========================================================================== */

(function () {
  "use strict";

  const body = document.getElementById("vm-body");
  const empty = document.getElementById("vm-empty");
  const foot = document.getElementById("vm-foot");
  const moreButton = document.getElementById("vm-more");
  const flavorSelect = document.getElementById("vm-flavor");
  const flavorNote = document.getElementById("vm-flavor-note");
  const form = document.getElementById("create-vm-form");
  const errorBox = document.getElementById("create-vm-error");

  // Nothing to do on pages without the compute view (there are none today, but
  // this keeps the file harmless if it is ever loaded elsewhere).
  if (!body) return;

  const state = {
    instances: [],
    flavors: [],
    nextBeforeId: null,
    quota: null,
    pollTimer: null,
    consoleId: null,
    consoleTimer: null,
  };

  // States in which the worker is about to change something, so the view keeps
  // polling until they settle.
  const BUSY = ["pending", "creating", "stopping", "deleting"];

  /* --- quota strip -------------------------------------------------------- */

  function quotaCell(used, cap, suffix) {
    const wrap = document.createElement("span");
    wrap.className = "quota" + (used >= cap ? " is-full" : "");
    const usedEl = document.createElement("span");
    usedEl.className = "used";
    usedEl.textContent = used;
    wrap.appendChild(usedEl);
    const rest = document.createElement("span");
    rest.className = "cap";
    rest.textContent = " / " + cap + (suffix || "");
    wrap.appendChild(rest);
    return wrap;
  }

  function renderQuota() {
    if (!state.quota) return;
    const used = state.quota.usage;
    const allowed = state.quota.limits;

    const pairs = [
      ["quota-vms", used.vms, allowed.max_vms, ""],
      ["quota-vcpu", used.vcpu, allowed.max_vcpu, ""],
      ["quota-memory", used.memory_mb, allowed.max_memory_mb, " MB"],
      ["quota-disk", used.disk_gb, allowed.max_disk_gb, " GB"],
    ];

    for (const [id, usedValue, cap, suffix] of pairs) {
      const el = document.getElementById(id);
      el.textContent = usedValue;
      el.classList.toggle("is-zero", usedValue === 0);
      document.getElementById(id + "-note").textContent =
        "of " + cap + suffix + (allowed.source === "override" ? " (override)" : "");
    }
  }

  /* --- flavor select ------------------------------------------------------ */

  function renderFlavors() {
    flavorSelect.replaceChildren(
      ...state.flavors.map((flavor) => {
        const option = new Option(flavor.name, flavor.name);
        option.dataset.spec =
          flavor.vcpu + " vCPU · " + flavor.memory_mb + " MB · " + flavor.disk_gb + " GB";
        return option;
      })
    );
    if (state.defaultFlavor) flavorSelect.value = state.defaultFlavor;
    showFlavorSpec();
  }

  function showFlavorSpec() {
    const option = flavorSelect.selectedOptions[0];
    flavorNote.textContent = option ? option.dataset.spec : "";
  }

  flavorSelect.addEventListener("change", showFlavorSpec);

  /* --- table -------------------------------------------------------------- */

  function actionButton(label, instance, action, danger) {
    const button = document.createElement("button");
    button.className = "btn btn-sm" + (danger ? " btn-danger" : "");
    button.type = "button";
    button.textContent = label;
    button.addEventListener("click", () => runAction(instance, action, button));
    return button;
  }

  function actionsFor(instance) {
    const wrap = document.createElement("div");
    wrap.className = "row-actions";

    if (BUSY.includes(instance.status)) {
      const note = document.createElement("span");
      note.className = "label-micro";
      note.textContent = "working";
      wrap.appendChild(note);
      return wrap;
    }
    if (instance.status === "deleted") return wrap;

    if (instance.status === "running") {
      wrap.appendChild(actionButton("Stop", instance, "stop"));
      wrap.appendChild(actionButton("Restart", instance, "restart"));
    } else {
      wrap.appendChild(actionButton("Start", instance, "start"));
    }

    const consoleButton = document.createElement("button");
    consoleButton.className = "btn btn-sm btn-quiet";
    consoleButton.type = "button";
    consoleButton.textContent = "Console";
    consoleButton.addEventListener("click", () => openConsole(instance));
    wrap.appendChild(consoleButton);

    wrap.appendChild(actionButton("Delete", instance, "delete", true));
    return wrap;
  }

  function addressCell(instance) {
    if (!instance.ip) return HC.cell("");
    const wrap = document.createElement("span");
    wrap.className = "mono";
    wrap.textContent = instance.ip;
    return HC.cell(wrap);
  }

  function instanceRow(instance) {
    const row = document.createElement("tr");
    row.appendChild(HC.cell(instance.id, "num"));
    row.appendChild(HC.cell(instance.name, "primary"));
    row.appendChild(HC.cell(HC.tag(instance.flavor || "—")));

    const statusCell = HC.cell(HC.status(instance.status));
    if (instance.last_error) statusCell.title = instance.last_error;
    row.appendChild(statusCell);

    row.appendChild(addressCell(instance));
    row.appendChild(HC.cell(HC.formatAge(instance.created_at), "mono"));
    row.appendChild(HC.cell(actionsFor(instance), "right"));
    return row;
  }

  function render() {
    body.replaceChildren(...state.instances.map(instanceRow));
    empty.classList.toggle("hidden", state.instances.length > 0);
    foot.textContent =
      state.instances.length + (state.instances.length === 1 ? " instance" : " instances");
    moreButton.classList.toggle("hidden", !state.nextBeforeId);
    document.getElementById("nav-count-compute").textContent = state.instances.length;
    schedulePoll();
  }

  /* --- loading ------------------------------------------------------------ */

  async function loadFlavors() {
    const result = await HC.api("/compute/api/flavors");
    if (!result.ok) return;
    state.flavors = result.data.flavors || [];
    state.defaultFlavor = result.data.default;
    state.quota = { limits: result.data.limits, usage: result.data.usage };
    renderFlavors();
    renderQuota();
  }

  async function load(append) {
    if (!append) HC.renderLoadingRows(body, 2, 7);

    const query = append && state.nextBeforeId ? "?before_id=" + state.nextBeforeId : "";
    const result = await HC.api("/compute/api/instances" + query);
    if (!result.ok) {
      if (!append) body.replaceChildren();
      HC.toast("Instances failed to load", result.data.error || "HTTP " + result.status, "error");
      return;
    }

    const page = result.data.instances || [];
    state.instances = append ? state.instances.concat(page) : page;
    state.nextBeforeId = result.data.next_before_id || null;
    render();
  }

  moreButton.addEventListener("click", () => load(true));

  /** Refresh on a timer only while something is in flight. */
  function schedulePoll() {
    if (state.pollTimer) {
      window.clearTimeout(state.pollTimer);
      state.pollTimer = null;
    }
    const busy = state.instances.some((instance) => BUSY.includes(instance.status));
    document.getElementById("vm-poll-note").textContent = busy
      ? "refreshing every 3 s"
      : "auto-refresh while busy";
    if (!busy) return;

    state.pollTimer = window.setTimeout(async () => {
      await Promise.all([load(false), loadFlavors()]);
    }, 3000);
  }

  /* --- create ------------------------------------------------------------- */

  function showError(message) {
    errorBox.textContent = message;
    errorBox.classList.remove("hidden");
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    errorBox.classList.add("hidden");

    const button = document.getElementById("create-vm-submit");
    HC.setBusy(button, true);

    const result = await HC.api("/compute/api/instances", {
      method: "POST",
      body: {
        name: document.getElementById("vm-name").value,
        flavor: flavorSelect.value,
      },
    });

    HC.setBusy(button, false);

    if (!result.ok) {
      showError(result.data.error || "HTTP " + result.status);
      return;
    }

    document.getElementById("vm-name").value = "";
    HC.toast("Instance queued", result.data.instance.name + " is being created", "success");
    await Promise.all([load(false), loadFlavors()]);
  });

  /* --- actions ------------------------------------------------------------ */

  async function runAction(instance, action, button) {
    if (action === "delete") {
      const confirmed = window.confirm(
        "Delete " + instance.name + "? The disk is removed and cannot be recovered."
      );
      if (!confirmed) return;
    }

    HC.setBusy(button, true);
    const path = "/compute/api/instances/" + instance.id;
    const result =
      action === "delete"
        ? await HC.api(path, { method: "DELETE" })
        : await HC.api(path + "/actions/" + action, { method: "POST" });

    if (!result.ok) {
      HC.setBusy(button, false);
      HC.toast(action + " failed", result.data.error || "HTTP " + result.status, "error");
      return;
    }

    HC.toast(action + " queued", instance.name, "success");
    await Promise.all([load(false), loadFlavors()]);
  }

  /* --- serial console ----------------------------------------------------- */

  const consolePanel = document.getElementById("console-panel");
  const consoleOut = document.getElementById("console-out");

  async function openConsole(instance) {
    state.consoleId = instance.id;
    document.getElementById("console-name").textContent = instance.name;
    consolePanel.classList.remove("hidden");
    consoleOut.textContent = "loading ...";
    consolePanel.scrollIntoView({ block: "nearest" });
    await refreshConsole();
    consoleOut.focus();
    if (state.consoleTimer) window.clearInterval(state.consoleTimer);
    state.consoleTimer = window.setInterval(refreshConsole, 1000);
  }

  async function refreshConsole() {
    if (!state.consoleId) return;
    const result = await HC.api(
      "/compute/api/instances/" + state.consoleId + "/console"
    );
    if (!result.ok) {
      consoleOut.textContent = result.data.error || "HTTP " + result.status;
      return;
    }
    consoleOut.textContent = result.data.console || "(console is still empty)";
    consoleOut.scrollTop = consoleOut.scrollHeight;
  }

  document.getElementById("console-refresh").addEventListener("click", refreshConsole);
  document.getElementById("console-close").addEventListener("click", () => {
    state.consoleId = null;
    if (state.consoleTimer) window.clearInterval(state.consoleTimer);
    state.consoleTimer = null;
    consolePanel.classList.add("hidden");
  });

  function keyToTerminalInput(event) {
    if (event.ctrlKey && event.key.length === 1) {
      const code = event.key.toUpperCase().charCodeAt(0);
      return code >= 64 && code <= 95 ? String.fromCharCode(code - 64) : null;
    }
    const special = {
      Enter: "\r", Backspace: "\u007f", Tab: "\t", Escape: "\u001b",
      ArrowUp: "\u001b[A", ArrowDown: "\u001b[B",
      ArrowRight: "\u001b[C", ArrowLeft: "\u001b[D",
    };
    if (special[event.key]) return special[event.key];
    return event.key.length === 1 && !event.metaKey && !event.altKey ? event.key : null;
  }

  consoleOut.addEventListener("keydown", async (event) => {
    const input = keyToTerminalInput(event);
    if (!state.consoleId || !input) return;
    event.preventDefault();
    const result = await HC.api(
      "/compute/api/instances/" + state.consoleId + "/console/input",
      { method: "POST", body: { input } }
    );
    if (!result.ok) {
      HC.toast("Terminal input failed", result.data.error || "HTTP " + result.status, "error");
    }
  });

  consoleOut.addEventListener("paste", async (event) => {
    if (!state.consoleId) return;
    const input = event.clipboardData.getData("text");
    if (!input) return;
    event.preventDefault();
    const result = await HC.api(
      "/compute/api/instances/" + state.consoleId + "/console/input",
      { method: "POST", body: { input } }
    );
    if (!result.ok) {
      HC.toast("Terminal paste failed", result.data.error || "HTTP " + result.status, "error");
    }
  });

  /* --- registration with the shell --------------------------------------- */

  window.HCViews = window.HCViews || {};
  window.HCViews.compute = {
    load: () => Promise.all([load(false), loadFlavors()]),
  };
})();
