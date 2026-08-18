// Dashboard: fetches the resource registry and renders it into the table.
// Vanilla JS only -- no framework, no bundler.

const tableBody = document.getElementById("resources-body");
const emptyNotice = document.getElementById("resources-empty");
const errorNotice = document.getElementById("resources-error");
const serviceFilter = document.getElementById("service-filter");

function showError(text) {
  errorNotice.textContent = text;
  errorNotice.classList.remove("hidden");
}

function clearError() {
  errorNotice.textContent = "";
  errorNotice.classList.add("hidden");
}

// Build one <tr>. Values go in via textContent, never innerHTML, so a resource
// name containing HTML can never inject markup.
function renderRow(resource) {
  const row = document.createElement("tr");

  const status = document.createElement("span");
  status.className = "status status-" + resource.status;
  status.textContent = resource.status;

  const cells = [
    String(resource.id),
    resource.service_type,
    resource.name,
    status,
    resource.created_at,
    resource.updated_at,
  ];

  for (const value of cells) {
    const cell = document.createElement("td");
    if (value instanceof Node) {
      cell.appendChild(value);
    } else {
      cell.textContent = value;
    }
    row.appendChild(cell);
  }
  return row;
}

// Keep the filter dropdown in sync with the service types actually present.
function updateServiceFilter(resources) {
  const known = new Set(resources.map((r) => r.service_type));
  const selected = serviceFilter.value;

  serviceFilter.innerHTML = "";
  const allOption = document.createElement("option");
  allOption.value = "";
  allOption.textContent = "All services";
  serviceFilter.appendChild(allOption);

  for (const serviceType of [...known].sort()) {
    const option = document.createElement("option");
    option.value = serviceType;
    option.textContent = serviceType;
    serviceFilter.appendChild(option);
  }
  serviceFilter.value = selected;
}

async function loadResources() {
  clearError();
  const serviceType = serviceFilter.value;
  const url = serviceType
    ? `/api/resources?service_type=${encodeURIComponent(serviceType)}`
    : "/api/resources";

  let response;
  try {
    response = await fetch(url, { headers: { Accept: "application/json" } });
  } catch (networkError) {
    showError("Could not reach the server.");
    return;
  }

  if (response.status === 401) {
    // Token expired while the page was open.
    window.location.href = "/auth/login?next=/";
    return;
  }
  if (!response.ok) {
    showError(`Failed to load resources (HTTP ${response.status}).`);
    return;
  }

  const data = await response.json();
  const resources = data.resources || [];

  tableBody.replaceChildren(...resources.map(renderRow));
  emptyNotice.classList.toggle("hidden", resources.length > 0);
  if (!serviceType) {
    updateServiceFilter(resources);
  }
}

document.getElementById("refresh-button").addEventListener("click", loadResources);
serviceFilter.addEventListener("change", loadResources);

document.getElementById("logout-button").addEventListener("click", async () => {
  await fetch("/auth/api/logout", { method: "POST" });
  window.location.href = "/auth/login";
});

loadResources();
