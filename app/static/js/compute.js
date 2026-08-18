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
  const imageSelect = document.getElementById("vm-image");
  const form = document.getElementById("create-vm-form");
  const createPanel = document.getElementById("vm-create-panel");
  const uploadPanel = document.getElementById("image-upload-panel");
  const uploadForm = document.getElementById("image-upload-form");
  const uploadError = document.getElementById("image-upload-error");
  const errorBox = document.getElementById("create-vm-error");

  // Nothing to do on pages without the compute view (there are none today, but
  // this keeps the file harmless if it is ever loaded elsewhere).
  if (!body) return;

  const state = {
    instances: [],
    flavors: [],
    images: [],
    imageTimer: null,
    imageUploadLimit: 0,
    nextBeforeId: null,
    quota: null,
    pollTimer: null,
    detailId: null,
    detailInstance: null,
    detailTab: "overview",
    detailTimer: null,
    consoleOffset: 0,
    consoleStream: null,
    consoleReconnectTimer: null,
    pendingTerminalInput: "",
    terminalInputTimer: null,
    terminalInputSending: false,
  };

  // States in which the worker is about to change something, so the view keeps
  // polling until they settle.
  const BUSY = ["pending", "creating", "stopping", "resizing", "deleting"];

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

  function renderImages() {
    imageSelect.replaceChildren(new Option("HomeCloud base image", ""), ...state.images
      .filter((image) => image.status === "ready")
      .map((image) => new Option(image.name + " · " + Math.round(image.size_bytes / 1048576) + " MiB", image.id)));
    const libraryBody = document.getElementById("image-library-body");
    libraryBody.replaceChildren(...state.images.map((image) => {
      const row = document.createElement("tr");
      row.appendChild(HC.cell(image.name, "primary"));
      row.appendChild(HC.cell(image.source === "upload" ? "Uploaded" : "Snapshot"));
      const trust = image.status === "error" ? "Rejected"
        : (image.verified ? "Integrity checked" : "Verifying");
      row.appendChild(HC.cell(HC.tag(trust)));
      const status = HC.status(image.status);
      if (image.last_error) status.title = image.last_error;
      row.appendChild(HC.cell(status));
      row.appendChild(HC.cell(image.size_bytes ? Math.round(image.size_bytes / 1048576) + " MiB" : "–", "mono"));
      const digest = image.sha256 ? image.sha256.slice(0, 12) + "…" : "–";
      const digestCell = HC.cell(digest, "mono");
      if (image.sha256) digestCell.title = image.sha256;
      row.appendChild(digestCell);
      row.appendChild(HC.cell(HC.formatAge(image.created_at), "mono"));
      return row;
    }));
    document.getElementById("image-library-empty").classList.toggle("hidden", state.images.length > 0);
    document.getElementById("image-library-count").textContent = state.images.length
      + (state.images.length === 1 ? " image" : " images");
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
    row.className = "clickable-row";
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
    row.addEventListener("click", (event) => {
      if (!event.target.closest("button")) window.location.hash = "vm/" + instance.id;
    });
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

  async function loadImages() {
    const result = await HC.api("/compute/api/images");
    if (!result.ok) return;
    state.images = result.data.images || [];
    state.imageUploadLimit = result.data.upload_limit_bytes || 0;
    if (state.imageUploadLimit) {
      document.getElementById("image-upload-note").textContent =
        "Maximum " + Math.round(state.imageUploadLimit / 1048576) + " MiB. "
        + "The ext4 filesystem is checksum-verified and inspected before use; HomeCloud supplies the kernel.";
    }
    renderImages();
    if (state.detailInstance) renderSnapshots(state.detailInstance);
    if (state.imageTimer) window.clearTimeout(state.imageTimer);
    state.imageTimer = state.images.some((image) => ["pending", "creating"].includes(image.status))
      ? window.setTimeout(loadImages, 2000)
      : null;
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
        image_id: imageSelect.value || null,
      },
    });

    HC.setBusy(button, false);

    if (!result.ok) {
      showError(result.data.error || "HTTP " + result.status);
      return;
    }

    document.getElementById("vm-name").value = "";
    createPanel.classList.add("hidden");
    HC.toast("Instance queued", result.data.instance.name + " is being created", "success");
    await Promise.all([load(false), loadFlavors()]);
  });

  document.getElementById("vm-create-toggle").addEventListener("click", () => {
    uploadPanel.classList.add("hidden");
    createPanel.classList.remove("hidden");
    document.getElementById("vm-name").focus();
  });
  document.getElementById("vm-create-cancel").addEventListener("click", () => {
    createPanel.classList.add("hidden");
    errorBox.classList.add("hidden");
  });

  document.getElementById("image-upload-toggle").addEventListener("click", () => {
    createPanel.classList.add("hidden");
    uploadPanel.classList.remove("hidden");
    document.getElementById("image-upload-name").focus();
  });
  document.getElementById("image-upload-cancel").addEventListener("click", () => {
    uploadPanel.classList.add("hidden");
    uploadError.classList.add("hidden");
  });

  uploadForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    uploadError.classList.add("hidden");
    const file = document.getElementById("image-upload-file").files[0];
    if (!file) return;
    if (state.imageUploadLimit && file.size > state.imageUploadLimit) {
      uploadError.textContent = "The selected image exceeds the upload limit.";
      uploadError.classList.remove("hidden");
      return;
    }
    const imageName = document.getElementById("image-upload-name").value;
    const button = document.getElementById("image-upload-submit");
    const cancel = document.getElementById("image-upload-cancel");
    const progress = document.getElementById("image-upload-progress");
    const meter = document.getElementById("image-upload-meter");
    const percent = document.getElementById("image-upload-percent");
    progress.classList.remove("hidden");
    meter.value = 0;
    percent.textContent = "0%";
    HC.setBusy(button, true);
    cancel.disabled = true;
    const uploadPath = "/compute/api/images/uploads?name=" + encodeURIComponent(imageName)
      + "&filename=" + encodeURIComponent(file.name);
    const result = await HC.api(uploadPath, {
      method: "POST", body: file, headers: { "Content-Type": "application/octet-stream" },
      onUploadProgress: (loaded, total) => {
        const value = Math.min(100, Math.round(loaded / total * 100));
        meter.value = value;
        percent.textContent = value + "%";
      },
    });
    HC.setBusy(button, false);
    cancel.disabled = false;
    if (!result.ok) {
      uploadError.textContent = result.data.error || "Upload failed (HTTP " + result.status + ").";
      uploadError.classList.remove("hidden");
      progress.classList.add("hidden");
      return;
    }
    uploadForm.reset();
    uploadPanel.classList.add("hidden");
    progress.classList.add("hidden");
    HC.toast("Image uploaded", "Verification is running in the background.", "success");
    await loadImages();
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

  function disconnectConsoleStream() {
    if (state.consoleReconnectTimer) window.clearTimeout(state.consoleReconnectTimer);
    state.consoleReconnectTimer = null;
    if (state.consoleStream) state.consoleStream.close();
    state.consoleStream = null;
  }

  function setStreamState(label, connected) {
    const element = document.getElementById("console-stream-state");
    element.textContent = label;
    element.classList.toggle("is-connected", Boolean(connected));
  }

  function applyConsoleChunk(chunk) {
    if (chunk.reset || state.consoleOffset === 0) {
      terminal.clear();
      consoleDecoder = new TextDecoder("utf-8");
    }
    if (chunk.data) {
      const binary = window.atob(chunk.data);
      const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0));
      terminal.write(consoleDecoder.decode(bytes, { stream: true }));
    }
    state.consoleOffset = chunk.offset || state.consoleOffset;
  }

  function connectConsoleStream(delay) {
    disconnectConsoleStream();
    if (!state.detailId || state.detailTab !== "terminal") return;
    setStreamState(delay ? "Reconnecting…" : "Connecting…", false);
    state.consoleReconnectTimer = window.setTimeout(() => {
      state.consoleReconnectTimer = null;
      const source = new EventSource(
        "/compute/api/instances/" + state.detailId + "/console/stream?after=" + state.consoleOffset
      );
      state.consoleStream = source;
      source.onopen = () => setStreamState("Live", true);
      source.onmessage = (event) => {
        try { applyConsoleChunk(JSON.parse(event.data)); }
        catch (error) { setStreamState("Stream error", false); }
      };
      source.onerror = () => {
        source.close();
        if (state.consoleStream === source) state.consoleStream = null;
        if (state.detailId && state.detailTab === "terminal") connectConsoleStream(1000);
      };
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
    document.getElementById("vm-detail-id").textContent = "#" + instance.id;
    document.getElementById("vm-detail-flavor").textContent = instance.flavor || "Custom";
    renderFlavorEditor(instance);
    document.getElementById("vm-detail-address").textContent = instance.ip || "Not assigned";
    document.getElementById("vm-detail-created").textContent = HC.formatDateTime(instance.created_at);
    const firewallRules = document.getElementById("vm-firewall-rules");
    const rules = instance.firewall || [];
    firewallRules.replaceChildren(...rules.map((rule, index) => {
      const button = document.createElement("button");
      button.className = "firewall-rule";
      button.type = "button";
      const endpoint = document.createElement("strong");
      endpoint.textContent = rule.protocol.toUpperCase() + " " + rule.port;
      const source = document.createElement("span");
      source.textContent = rule.source;
      const remove = document.createElement("span");
      remove.textContent = "Remove";
      remove.setAttribute("aria-hidden", "true");
      button.append(endpoint, source, remove);
      button.addEventListener("click", () => saveFirewall(instance, rules.filter((_, i) => i !== index)));
      return button;
    }));
    document.getElementById("vm-firewall-empty").classList.toggle("hidden", rules.length > 0);
    renderSnapshots(instance);

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

  function renderFlavorEditor(instance) {
    const select = document.getElementById("vm-detail-flavor-select");
    const submit = document.getElementById("vm-detail-flavor-submit");
    select.replaceChildren(...state.flavors.map((flavor) => {
      const option = new Option(flavor.name, flavor.name);
      option.textContent = flavor.name + " · " + flavor.vcpu + " vCPU · " +
        flavor.memory_mb + " MB · " + flavor.disk_gb + " GB";
      return option;
    }));
    select.value = instance.flavor || "";
    const disabled = BUSY.includes(instance.status) || instance.status === "deleted";
    select.disabled = disabled;
    submit.disabled = disabled;
  }

  async function createSnapshot(instance, name, button) {
    HC.setBusy(button, true);
    const result = await HC.api("/compute/api/images/snapshots", {
      method: "POST", body: { instance_id: instance.id, name },
    });
    HC.setBusy(button, false);
    if (!result.ok) { HC.toast("Snapshot failed", result.data.error || "HTTP " + result.status, "error"); return; }
    HC.toast("Snapshot queued", name, "success");
    document.getElementById("snapshot-name").value = "";
    await loadImages();
    renderSnapshots(instance);
  }

  function renderSnapshots(instance) {
    const images = state.images.filter((image) => image.source_instance_id === instance.id);
    document.getElementById("vm-snapshot-body").replaceChildren(...images.map((image) => {
      const row = document.createElement("tr");
      row.appendChild(HC.cell(image.name, "primary"));
      row.appendChild(HC.cell(HC.status(image.status)));
      row.appendChild(HC.cell(image.size_bytes ? Math.round(image.size_bytes / 1048576) + " MiB" : "–", "mono"));
      row.appendChild(HC.cell(HC.formatAge(image.created_at), "mono"));
      return row;
    }));
    document.getElementById("vm-snapshot-empty").classList.toggle("hidden", images.length > 0);
    const allowed = instance.status === "stopped";
    document.getElementById("snapshot-submit").disabled = !allowed;
    document.getElementById("snapshot-note").textContent = allowed
      ? "The disk is idle and ready for a consistent snapshot."
      : "Stop the instance before creating a snapshot.";
  }

  function setDetailTab(tab, updateHash) {
    const allowed = ["overview", "terminal", "firewall", "snapshots"];
    state.detailTab = allowed.includes(tab) ? tab : "overview";
    document.querySelectorAll("[data-vm-panel]").forEach((panel) => {
      panel.hidden = panel.dataset.vmPanel !== state.detailTab;
    });
    document.querySelectorAll("[data-vm-tab]").forEach((button) => {
      if (button.dataset.vmTab === state.detailTab) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
    if (updateHash && state.detailId) window.location.hash = "vm/" + state.detailId + "/" + state.detailTab;
    if (state.detailTab === "terminal" && state.detailInstance && state.detailInstance.status === "running") connectConsoleStream(0);
    else { disconnectConsoleStream(); setStreamState("Disconnected", false); }
  }

  document.querySelectorAll("[data-vm-tab]").forEach((button) => {
    button.addEventListener("click", () => setDetailTab(button.dataset.vmTab, true));
  });

  document.getElementById("vm-snapshot-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (!state.detailInstance) return;
    createSnapshot(state.detailInstance, document.getElementById("snapshot-name").value, document.getElementById("snapshot-submit"));
  });

  function scheduleDetailPoll(instance) {
    if (state.detailTimer) window.clearTimeout(state.detailTimer);
    state.detailTimer = null;
    const delay = instance.status === "running" ? 2000 : (BUSY.includes(instance.status) ? 1500 : 0);
    if (!delay) return;
    state.detailTimer = window.setTimeout(() => loadDetail(instance.id), delay);
  }

  async function loadDetail(resourceId) {
    const result = await HC.api("/compute/api/instances/" + resourceId);
    if (!window.location.hash.startsWith("#vm/" + resourceId)) return;
    if (!result.ok) {
      HC.toast("Instance failed to load", result.data.error || "HTTP " + result.status, "error");
      window.location.hash = "compute";
      return;
    }
    const instance = result.data.instance;
    state.detailId = instance.id;
    state.detailInstance = instance;
    renderDetail(instance);
    scheduleDetailPoll(instance);
    if (instance.status === "running") {
      if (state.detailTab === "terminal" && !state.consoleStream && !state.consoleReconnectTimer) {
        if (state.consoleOffset === 0) terminal.clear();
        connectConsoleStream(0);
      }
    } else {
      disconnectConsoleStream();
      setStreamState("Offline", false);
      terminal.clear();
      terminal.write("Terminal is available while the instance is running.");
    }
  }

  document.getElementById("console-refresh").addEventListener("click", () => {
    state.consoleOffset = 0;
    consoleDecoder = new TextDecoder("utf-8");
    terminal.clear();
    connectConsoleStream(0);
    consoleOut.focus();
  });

  terminal.onData((input) => {
    if (!state.detailId) return;
    const pasted = input.length > 1;
    queueTerminalInput(input, pasted ? "paste" : undefined, pasted ? 0 : undefined);
  });

  async function saveFirewall(instance, rules) {
    const result = await HC.api("/compute/api/instances/" + instance.id + "/firewall", {
      method: "PUT", body: { rules },
    });
    if (!result.ok) {
      HC.toast("Firewall update failed", result.data.error || "HTTP " + result.status, "error");
      return;
    }
    HC.toast("Firewall update queued", instance.name, "success");
    await loadDetail(instance.id);
  }

  document.getElementById("vm-firewall-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const instance = state.detailInstance;
    if (!instance) return;
    const rule = {
      protocol: document.getElementById("firewall-protocol").value,
      port: Number(document.getElementById("firewall-port").value),
      source: document.getElementById("firewall-source").value,
    };
    saveFirewall(instance, (instance.firewall || []).concat(rule));
  });

  document.getElementById("vm-flavor-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const instance = state.detailInstance;
    if (!instance) return;
    const flavor = document.getElementById("vm-detail-flavor-select").value;
    if (!flavor || flavor === instance.flavor) return;
    const message = instance.status === "running"
      ? "Changing the instance type will restart this VM. Continue?"
      : "Apply this new instance type?";
    if (!window.confirm(message)) return;
    const button = document.getElementById("vm-detail-flavor-submit");
    HC.setBusy(button, true);
    const result = await HC.api("/compute/api/instances/" + instance.id + "/flavor", {
      method: "PUT", body: { flavor },
    });
    HC.setBusy(button, false);
    if (!result.ok) {
      HC.toast("Instance type change failed", result.data.error || "HTTP " + result.status, "error");
      return;
    }
    HC.toast("Instance type queued", flavor, "success");
    await Promise.all([load(false), loadFlavors(), loadDetail(instance.id)]);
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
      }
    } finally {
      state.terminalInputSending = false;
      if (state.pendingTerminalInput) queueTerminalInput("", label, 0);
    }
  }

  function routeDetail() {
    const match = window.location.hash.match(/^#vm\/(\d+)(?:\/(overview|terminal|firewall|snapshots))?$/);
    if (!match) {
      state.detailId = null;
      state.detailInstance = null;
      state.consoleOffset = 0;
      state.pendingTerminalInput = "";
      if (state.terminalInputTimer) window.clearTimeout(state.terminalInputTimer);
      state.terminalInputTimer = null;
      disconnectConsoleStream();
      if (state.detailTimer) window.clearTimeout(state.detailTimer);
      state.detailTimer = null;
      return;
    }
    const resourceId = Number(match[1]);
    setDetailTab(match[2] || "overview", false);
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
    load: () => Promise.all([load(false), loadFlavors(), loadImages()]).then(routeDetail),
  };
})();
