const ADMIN_TOKEN_KEY = "travelerguard_admin_token";
const adminState = { token: sessionStorage.getItem(ADMIN_TOKEN_KEY), toastTimer: null };
const byId = (id) => document.getElementById(id);
const escapeHTML = (value = "") =>
  String(value).replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[character]));

async function adminApi(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (adminState.token) headers.set("Authorization", `Bearer ${adminState.token}`);
  if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
  const response = await fetch(`/api${path}`, { ...options, headers });
  const payload = response.status === 204 ? null : await response.json();
  if (!response.ok) {
    if (response.status === 401 || response.status === 403) clearAdminSession();
    throw new Error(payload?.error?.message || "The request could not be completed.");
  }
  return payload;
}

function showToast(message, isError = false) {
  const toast = byId("admin-toast");
  toast.textContent = message;
  toast.classList.toggle("error", isError);
  toast.classList.remove("hidden");
  window.clearTimeout(adminState.toastTimer);
  adminState.toastTimer = window.setTimeout(() => toast.classList.add("hidden"), 3500);
}

function clearAdminSession() {
  adminState.token = null;
  sessionStorage.removeItem(ADMIN_TOKEN_KEY);
  byId("admin-login-view").classList.remove("hidden");
  byId("admin-records-view").classList.add("hidden");
}

async function loadVehicleRecords(plateNumber = "") {
  const list = byId("vehicle-list");
  const error = byId("vehicle-list-error");
  error.textContent = "";
  list.innerHTML = '<div class="empty-state"><strong>Loading records…</strong></div>';
  try {
    const query = plateNumber ? `?plate_number=${encodeURIComponent(plateNumber)}` : "";
    const records = await adminApi(`/admin/vehicles${query}`);
    if (!records.length) {
      list.innerHTML = '<div class="empty-state"><strong>No vehicle records found.</strong></div>';
      return;
    }
    list.innerHTML = records.map((record) => {
      const details = [record.owner_name, record.make, record.model, record.color, record.region]
        .filter(Boolean)
        .map(escapeHTML)
        .join(" · ");
      return `<div class="scan-observation"><strong>${escapeHTML(record.plate_number)}</strong><small>${details || "No additional details"}</small>${record.notes ? `<small>${escapeHTML(record.notes)}</small>` : ""}<button class="button button-outline button-small" type="button" data-vehicle-delete="${escapeHTML(record.id)}">Delete</button></div>`;
    }).join("");
  } catch (errorValue) {
    list.innerHTML = "";
    error.textContent = errorValue.message;
  }
}

byId("admin-login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = byId("admin-login-submit");
  const error = byId("admin-login-error");
  button.disabled = true;
  error.textContent = "";
  try {
    const result = await adminApi("/admin/login", {
      method: "POST",
      body: JSON.stringify({
        username: byId("admin-username").value.trim(),
        password: byId("admin-password").value,
      }),
    });
    adminState.token = result.access_token;
    sessionStorage.setItem(ADMIN_TOKEN_KEY, adminState.token);
    byId("admin-login-form").reset();
    byId("admin-login-view").classList.add("hidden");
    byId("admin-records-view").classList.remove("hidden");
    await loadVehicleRecords();
  } catch (errorValue) {
    error.textContent = errorValue.message;
  } finally {
    button.disabled = false;
  }
});

byId("vehicle-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = byId("vehicle-submit");
  const error = byId("vehicle-form-error");
  button.disabled = true;
  error.textContent = "";
  const record = {
    plate_number: byId("vehicle-plate").value.trim(),
    owner_name: byId("vehicle-owner").value.trim(),
    make: byId("vehicle-make").value.trim(),
    model: byId("vehicle-model").value.trim(),
    color: byId("vehicle-color").value.trim(),
    region: byId("vehicle-region").value.trim(),
    notes: byId("vehicle-notes").value.trim(),
  };
  try {
    await adminApi("/admin/vehicles", { method: "POST", body: JSON.stringify(record) });
    byId("vehicle-form").reset();
    await loadVehicleRecords();
    showToast("Vehicle record added.");
  } catch (errorValue) {
    error.textContent = errorValue.message;
  } finally {
    button.disabled = false;
  }
});

byId("vehicle-search-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  await loadVehicleRecords(byId("vehicle-search").value.trim());
});

byId("admin-plate-image").addEventListener("change", async (event) => {
  const file = event.target.files?.[0];
  if (!file) return;
  const results = byId("plate-match-results");
  if (!["image/jpeg", "image/png", "image/webp"].includes(file.type) || file.size > 5 * 1024 * 1024) {
    event.target.value = "";
    results.innerHTML = '<p class="inline-error">Choose a JPEG, PNG or WEBP image up to 5 MB.</p>';
    return;
  }
  results.innerHTML = '<div class="empty-state"><strong>Reading plate text…</strong></div>';
  const form = new FormData();
  form.append("file", file);
  try {
    const scan = await adminApi("/admin/ocr/scan", { method: "POST", body: form });
    if (!scan.plate_candidates?.length) {
      results.innerHTML = `<div class="scan-observation"><strong>No likely plate text found</strong><small>${escapeHTML(scan.text || "No text could be read.")}</small><small>OCR confidence: ${scan.confidence === null ? "unavailable" : `${escapeHTML(scan.confidence)}%`}</small></div>`;
      return;
    }
    const matches = await Promise.all(scan.plate_candidates.map(async (plate) => {
      const records = await adminApi(`/admin/vehicles?plate_number=${encodeURIComponent(plate)}`);
      return { plate, record: records[0] || null };
    }));
    results.innerHTML = matches.map(({ plate, record }) => {
      if (!record) return `<div class="scan-observation"><strong>${escapeHTML(plate)}</strong><small>No administrator-entered vehicle record matches this possible plate.</small></div>`;
      const details = [record.owner_name, record.make, record.model, record.color, record.region]
        .filter(Boolean)
        .map(escapeHTML)
        .join(" · ");
      return `<div class="scan-observation"><strong>Record match: ${escapeHTML(record.plate_number)}</strong><small>${details || "No additional details"}</small>${record.notes ? `<small>${escapeHTML(record.notes)}</small>` : ""}</div>`;
    }).join("");
  } catch (errorValue) {
    results.innerHTML = `<p class="inline-error">${escapeHTML(errorValue.message)}</p>`;
  }
});

byId("vehicle-show-all").addEventListener("click", () => {
  byId("vehicle-search").value = "";
  void loadVehicleRecords();
});

byId("vehicle-list").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-vehicle-delete]");
  if (!button || !window.confirm("Permanently delete this vehicle record?")) return;
  try {
    await adminApi(`/admin/vehicles/${encodeURIComponent(button.dataset.vehicleDelete)}`, { method: "DELETE" });
    await loadVehicleRecords(byId("vehicle-search").value.trim());
    showToast("Vehicle record deleted.");
  } catch (errorValue) {
    byId("vehicle-list-error").textContent = errorValue.message;
  }
});

byId("admin-sign-out").addEventListener("click", clearAdminSession);

if (adminState.token) {
  byId("admin-login-view").classList.add("hidden");
  byId("admin-records-view").classList.remove("hidden");
  void loadVehicleRecords();
}
