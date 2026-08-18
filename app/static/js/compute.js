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
    detailId: null,
    detailTimer: null,
    consoleOffset: 0,
    consoleTimer: null,
    consoleLoading: false,
    pendingTerminalInput: "",
    terminalInputTimer: null,
    terminalInputSending: false,
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

  function openButton(instance) {
    const button = document.createElement("button");
    button.className = "btn btn-sm btn-quiet";
    button.type = "button";
    button.textContent = "Open";
    button.addEventListener("click", () => {
      window.location.hash = "vm/" + instance.id;
    });
    return button;
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
    const name = HC.cell(instance.name, "primary");
    name.tabIndex = 0;
    name.classList.add("instance-link");
    name.addEventListener("click", () => { window.location.hash = "vm/" + instance.id; });
    name.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        window.location.hash = "vm/" + instance.id;
      }
    });
    row.appendChild(name);
    row.appendChild(HC.cell(HC.tag(instance.flavor || "—")));

    const statusCell = HC.cell(HC.status(instance.status));
    if (instance.last_error) statusCell.title = instance.last_error;
    row.appendChild(statusCell);

    row.appendChild(addressCell(instance));
    row.appendChild(HC.cell(HC.formatAge(instance.created_at), "mono"));
    row.appendChild(HC.cell(openButton(instance), "right"));
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
    if (state.detailId === instance.id) await loadDetail(instance.id);
  }

  /* --- instance dashboard and serial terminal --------------------------- */

  const consoleOut = document.getElementById("console-out");
  const detailActions = document.getElementById("vm-detail-actions");
  const terminal = new window.HCTerminal(consoleOut, { scrollback: 800 });
  let consoleDecoder = new TextDecoder("utf-8");

  function formatMiB(bytes) {
    return Math.round((bytes || 0) / (1024 * 1024));
  }

  function formatGiB(bytes) {
    const gib = (bytes || 0) / (1024 * 1024 * 1024);
    return gib < 1 ? gib.toFixed(2) : gib.toFixed(1);
  }

  function clearConsoleTimer() {
    if (state.consoleTimer) window.clearTimeout(state.consoleTimer);
    state.consoleTimer = null;
  }

  function scheduleConsolePoll(delay) {
    clearConsoleTimer();
    if (!state.detailId) return;
    state.consoleTimer = window.setTimeout(async () => {
      const more = await refreshConsole();
      // A serial console is interactive: 350 ms made echoed keystrokes feel
      // remote. Keep the local poll light but fast, and drain a backlog in the
      // next frame instead of making the user wait for it.
      scheduleConsolePoll(more ? 0 : 80);
    }, delay);
  }

  function renderDetail(instance) {
    document.getElementById("vm-detail-name").textContent = instance.name;
    document.getElementById("vm-detail-subtitle").textContent =
      "Instance #" + instance.id + " · created " + HC.formatAge(instance.created_at);
    const usage = instance.usage;
    const cpuLimit = (instance.vcpu || 0) * 100;
    if (usage) {
      document.getElementById("vm-detail-vcpu").textContent = usage.cpu_percent + "%";
      document.getElementById("vm-detail-vcpu-note").textContent =
        "used of " + cpuLimit + "% allocation";
      document.getElementById("vm-detail-memory").textContent =
        formatMiB(usage.memory_bytes) + " MiB";
      document.getElementById("vm-detail-memory-note").textContent =
        "used of " + (instance.memory_mb || 0) + " MiB allocation";
      document.getElementById("vm-detail-disk").textContent =
        formatGiB(usage.disk_bytes) + " GiB";
      document.getElementById("vm-detail-disk-note").textContent =
        "actual host blocks of " + (instance.disk_gb || 0) + " GiB provisioned";
    } else {
      for (const id of ["vcpu", "memory", "disk"]) {
        document.getElementById("vm-detail-" + id).textContent = "–";
        document.getElementById("vm-detail-" + id + "-note").textContent =
          "waiting for a running-worker sample";
      }
    }
    document.getElementById("vm-detail-status").replaceChildren(HC.status(instance.status));
    document.getElementById("vm-detail-ip").textContent = instance.ip || "no network address yet";

    detailActions.replaceChildren();
    if (BUSY.includes(instance.status)) {
      const note = document.createElement("span");
      note.className = "label-micro";
      note.textContent = "operation in progress";
      detailActions.appendChild(note);
      return;
    }
    if (instance.status === "deleted") return;
    if (instance.status === "running") {
      detailActions.appendChild(actionButton("Stop", instance, "stop"));
      detailActions.appendChild(actionButton("Restart", instance, "restart"));
    } else {
      detailActions.appendChild(actionButton("Start", instance, "start"));
    }
    detailActions.appendChild(actionButton("Delete", instance, "delete", true));
  }

  function scheduleDetailPoll(instance) {
    if (state.detailTimer) window.clearTimeout(state.detailTimer);
    state.detailTimer = null;
    const delay = instance.status === "running" ? 2000 : (BUSY.includes(instance.status) ? 1500 : 0);
    if (!delay) return;
    state.detailTimer = window.setTimeout(() => loadDetail(instance.id), delay);
  }

  async function loadDetail(resourceId) {
    const result = await HC.api("/compute/api/instances/" + resourceId);
    if (window.location.hash !== "#vm/" + resourceId) return;
    if (!result.ok) {
      HC.toast("Instance failed to load", result.data.error || "HTTP " + result.status, "error");
      window.location.hash = "compute";
      return;
    }
    const instance = result.data.instance;
    state.detailId = instance.id;
    renderDetail(instance);
    scheduleDetailPoll(instance);
    if (instance.status === "running") {
      if (state.consoleOffset === 0) terminal.clear();
      scheduleConsolePoll(0);
    } else {
      clearConsoleTimer();
      terminal.clear();
      terminal.write("Terminal is available while the instance is running.");
    }
  }

  async function refreshConsole() {
    if (!state.detailId || state.consoleLoading) return false;
    state.consoleLoading = true;
    const result = await HC.api(
      "/compute/api/instances/" + state.detailId + "/console?after=" + state.consoleOffset
    );
    state.consoleLoading = false;
    if (!result.ok) {
      terminal.clear();
      terminal.write(result.data.error || "HTTP " + result.status);
      return false;
    }
    const wasAtBottom = consoleOut.scrollHeight - consoleOut.scrollTop - consoleOut.clientHeight < 24;
    if (result.data.reset || state.consoleOffset === 0) {
      terminal.clear();
      consoleDecoder = new TextDecoder("utf-8");
    }
    if (result.data.data) {
      const binary = window.atob(result.data.data);
      const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0));
      terminal.write(consoleDecoder.decode(bytes, { stream: true }));
    }
    state.consoleOffset = result.data.offset || state.consoleOffset;
    if (wasAtBottom) consoleOut.scrollTop = consoleOut.scrollHeight;
    return Boolean(result.data.more);
  }

  document.getElementById("console-refresh").addEventListener("click", () => {
    state.consoleOffset = 0;
    refreshConsole();
    consoleOut.focus();
  });

  terminal.onData((input) => {
    if (!state.detailId) return;
    const pasted = input.length > 1;
    queueTerminalInput(input, pasted ? "paste" : undefined, pasted ? 0 : undefined);
  });

  function queueTerminalInput(input, label, delay) {
    state.pendingTerminalInput += input;
    if (state.terminalInputTimer || state.terminalInputSending) return;
    state.terminalInputTimer = window.setTimeout(
      () => flushTerminalInput(label), delay === undefined ? 35 : delay
    );
  }

  async function flushTerminalInput(label) {
    state.terminalInputTimer = null;
    if (!state.detailId || !state.pendingTerminalInput) return;
    const input = state.pendingTerminalInput;
    state.pendingTerminalInput = "";
    state.terminalInputSending = true;
    try {
      const result = await HC.api(
        "/compute/api/instances/" + state.detailId + "/console/input",
        { method: "POST", body: { input } }
      );
      if (!result.ok) {
        HC.toast("Terminal " + (label || "input") + " failed",
          result.data.error || "HTTP " + result.status, "error");
      } else {
        // The guest normally echoes input. Ask for that output as soon as the
        // worker has accepted the keystroke instead of waiting for the timer.
        window.setTimeout(() => refreshConsole(), 10);
      }
    } finally {
      state.terminalInputSending = false;
      if (state.pendingTerminalInput) queueTerminalInput("", label, 0);
    }
  }

  function routeDetail() {
    const match = window.location.hash.match(/^#vm\/(\d+)$/);
    if (!match) {
      state.detailId = null;
      state.consoleOffset = 0;
      state.pendingTerminalInput = "";
      if (state.terminalInputTimer) window.clearTimeout(state.terminalInputTimer);
      state.terminalInputTimer = null;
      clearConsoleTimer();
      if (state.detailTimer) window.clearTimeout(state.detailTimer);
      state.detailTimer = null;
      return;
    }
    const resourceId = Number(match[1]);
    if (state.detailId !== resourceId) {
      state.consoleOffset = 0;
      state.pendingTerminalInput = "";
    }
    loadDetail(resourceId);
  }

  document.getElementById("vm-detail-back").addEventListener("click", () => {
    window.location.hash = "compute";
  });
  window.addEventListener("homecloud:viewchange", routeDetail);

  /* --- registration with the shell --------------------------------------- */

  window.HCViews = window.HCViews || {};
  window.HCViews.compute = {
    load: () => Promise.all([load(false), loadFlavors()]).then(routeDetail),
  };
})();
