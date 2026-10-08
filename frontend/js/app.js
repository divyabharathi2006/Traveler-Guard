const API_BASE = "/api";
const AUTH_TOKEN_KEY = "travelerguard_access_token";

const state = {
  token: localStorage.getItem(AUTH_TOKEN_KEY),
  user: null,
  trips: [],
  contacts: [],
  locationWatch: null,
  currentLocation: null,
  lastLocationSentAt: 0,
  locationSyncWarned: false,
  dashboardMap: null,
  fullMap: null,
  markers: {},
  authMode: "login",
  mediaStream: null,
  imageBlob: null,
  activeEvent: null,
  shares: [],
  shareUrls: {},
  emergencyStartedTracking: false,
  toastTimer: null,
  socket: null,
};

const byId = (id) => document.getElementById(id);
const escapeHTML = (value = "") =>
  String(value).replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[character]));

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (state.token) headers.set("Authorization", `Bearer ${state.token}`);
  if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
  let response;
  try {
    response = await fetch(`${API_BASE}${path}`, { ...options, headers });
  } catch {
    throw new Error("The service could not be reached. Check your connection and try again.");
  }
  if (response.status === 204) return null;
  let payload;
  try {
    payload = await response.json();
  } catch {
    throw new Error("The server returned an unreadable response.");
  }
  if (!response.ok) {
    const message = payload?.error?.message || "The request could not be completed.";
    if (response.status === 401 && state.token) clearExpiredSession();
    throw new Error(message);
  }
  return payload;
}

function renderShares() {
  const list = byId("location-share-list");
  if (!list) return;
  if (!state.shares.length) {
    list.innerHTML = "";
    return;
  }
  list.innerHTML = state.shares.map((share) => {
    const url = state.shareUrls[share.id];
    return `<div class="list-item"><div class="list-item-icon">◎</div><div class="list-item-body"><strong>Private link active</strong><small>Expires ${new Date(share.expires_at).toLocaleString()}</small>${url ? `<small class="share-url">${escapeHTML(url)}</small>` : ""}</div><div class="list-item-actions">${url ? `<button class="small-action" data-share-copy="${escapeHTML(share.id)}">Copy link</button>` : ""}<button class="small-action danger" data-share-revoke="${escapeHTML(share.id)}">Revoke</button></div></div>`;
  }).join("");
}

async function editContact(id) {
  const contact = state.contacts.find((item) => item.id === id);
  if (!contact) return;
  const name = window.prompt("Contact name", contact.name);
  if (name === null) return;
  const phone = window.prompt("Phone number", contact.phone);
  if (phone === null) return;
  const relationship = window.prompt("Relationship (optional)", contact.relationship || "");
  if (relationship === null) return;
  const email = window.prompt("Email (optional)", contact.email || "");
  if (email === null) return;
  try {
    await api(`/emergency-contacts/${encodeURIComponent(id)}`, {
      method: "PUT",
      body: JSON.stringify({ name, phone, relationship, email }),
    });
    await refreshDashboard();
    notify("Emergency contact updated.");
  } catch (error) {
    notify(error.message, true);
  }
}

async function createLocationShare() {
  const accepted = window.confirm(
    "Create a private link that anyone who receives it can use to view your latest GPS location for up to four hours? This starts device location sharing. You can stop sharing or revoke the link at any time.",
  );
  if (!accepted) return;
  const button = byId("create-share-link");
  button.disabled = true;
  try {
    const share = await api("/location/shares", { method: "POST" });
    state.shareUrls[share.id] = share.url;
    await refreshDashboard();
    renderShares();
    startLocationSharing();
    try {
      await navigator.clipboard.writeText(share.url);
      notify("Private link created and copied. Your device will ask for location permission.");
    } catch {
      notify("Private link created. Use Copy link below to share it. Your device will ask for location permission.");
    }
  } catch (error) {
    notify(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function revokeLocationShare(id) {
  try {
    await api(`/location/shares/${encodeURIComponent(id)}`, { method: "DELETE" });
    delete state.shareUrls[id];
    await refreshDashboard();
    notify("Location share link revoked.");
  } catch (error) {
    notify(error.message, true);
  }
}

async function copyLocationShare(id) {
  const url = state.shareUrls[id];
  if (!url) {
    notify("This link is not available in this browser session. Revoke it and create a new share link.", true);
    return;
  }
  try {
    await navigator.clipboard.writeText(url);
    notify("Private location link copied.");
  } catch {
    window.prompt("Copy your private location link:", url);
  }
}

async function refreshSmsInbox() {
  try {
    const messages = await api("/emergency/sms/inbox");
    const list = byId("sms-inbox");
    list.innerHTML = messages.length
      ? messages.map((message) => `<div class="list-item"><div class="list-item-icon">↩</div><div class="list-item-body"><strong>Reply from ${escapeHTML(message.from_number)}</strong><small>${escapeHTML(message.body)}</small><small>${new Date(message.received_at).toLocaleString()} · Event ${escapeHTML(message.emergency_event_id || "no longer available")}</small></div></div>`).join("")
      : '<div class="empty-state compact"><strong>No replies recorded</strong><p>Verified SMS replies to an active event appear here.</p></div>';
  } catch (error) {
    notify(error.message, true);
  }
}

function notify(message, isError = false) {
  const toast = byId("toast");
  toast.textContent = message;
  toast.classList.toggle("error", isError);
  toast.classList.remove("hidden");
  window.clearTimeout(state.toastTimer);
  state.toastTimer = window.setTimeout(() => toast.classList.add("hidden"), 3800);
}

function setNotice(message, visible = true) {
  const notice = byId("app-notice");
  notice.textContent = message;
  notice.classList.toggle("hidden", !visible);
}

function setNetworkStatus() {
  const pill = byId("network-status");
  if (!pill) return;
  pill.classList.toggle("online", navigator.onLine);
  pill.classList.toggle("offline", !navigator.onLine);
  pill.innerHTML = `<i></i>${navigator.onLine ? "Internet connected" : "Offline"}`;
  if (!navigator.onLine && state.currentLocation) {
    setNotice(`Internet connection lost. Last location: ${state.currentLocation.latitude.toFixed(5)}, ${state.currentLocation.longitude.toFixed(5)}. Some emergency features may be unavailable.`);
  }
}

function showAuth(mode = "login") {
  state.authMode = mode;
  byId("auth-screen").classList.remove("hidden");
  byId("app-shell").classList.add("hidden");
  byId("name-field").classList.toggle("hidden", mode !== "register");
  byId("auth-title").textContent = mode === "register" ? "Create your TravelerGuard account." : "Travel with more peace of mind.";
  byId("auth-submit").textContent = mode === "register" ? "Create account" : "Sign in";
  byId("auth-switch").textContent = mode === "register" ? "Already have an account? Sign in" : "New here? Create an account";
  byId("auth-password").autocomplete = mode === "register" ? "new-password" : "current-password";
  byId("auth-password").minLength = mode === "register" ? 10 : 1;
  byId("auth-error").textContent = "";
}

async function signOut(showMessage = true) {
  await stopLocationSharing(false);
  clearExpiredSession();
  if (showMessage) notify("You have signed out.");
}

function clearExpiredSession() {
  stopLocationSharing(false, false);
  stopCamera();
  state.socket?.close();
  state.socket = null;
  state.token = null;
  state.user = null;
  localStorage.removeItem(AUTH_TOKEN_KEY);
  showAuth();
}

function showApp() {
  byId("auth-screen").classList.add("hidden");
  byId("app-shell").classList.remove("hidden");
  const name = state.user?.full_name || "Traveler";
  byId("user-name").textContent = name;
  byId("user-email").textContent = state.user?.email || "";
  byId("user-avatar").textContent = name.trim().charAt(0).toUpperCase() || "T";
  byId("welcome-name").textContent = name.split(/\s+/)[0];
  byId("today-label").textContent = new Intl.DateTimeFormat(undefined, { weekday: "long", month: "long", day: "numeric" }).format(new Date()).toUpperCase();
  setNetworkStatus();
  initializeMaps();
  refreshDashboard().catch((error) => notify(error.message, true));
  connectSocket();
}

async function submitAuth(event) {
  event.preventDefault();
  const email = byId("auth-email").value.trim();
  const password = byId("auth-password").value;
  const payload = { email, password };
  if (state.authMode === "register") payload.full_name = byId("auth-name").value.trim();
  byId("auth-submit").disabled = true;
  byId("auth-error").textContent = "";
  try {
    const result = await api(`/auth/${state.authMode === "register" ? "register" : "login"}`, {
      method: "POST", body: JSON.stringify(payload),
    });
    state.token = result.access_token;
    localStorage.setItem(AUTH_TOKEN_KEY, state.token);
    state.user = await api("/users/me");
    byId("auth-form").reset();
    showApp();
  } catch (error) {
    byId("auth-error").textContent = error.message;
  } finally {
    byId("auth-submit").disabled = false;
  }
}

function navigate(page) {
  const titles = {
    dashboard: "Overview", trip: "My trip", map: "Live map", scanner: "AI scanner",
    safety: "Safety check-in", emergency: "Emergency", history: "History",
    profile: "Profile & contacts", settings: "Settings",
  };
  document.querySelectorAll(".page-section").forEach((section) => section.classList.remove("active"));
  document.querySelectorAll(".nav-link[data-page]").forEach((link) => link.classList.toggle("active", link.dataset.page === page));
  byId(`page-${page}`)?.classList.add("active");
  byId("page-title").textContent = titles[page] || "Overview";
  byId("sidebar").classList.remove("open");
  if (page === "map") {
    window.setTimeout(() => {
      state.fullMap?.invalidateSize();
      if (state.currentLocation) updateMapMarkers(state.currentLocation);
    }, 80);
  }
  if (page === "scanner") byId("camera-start").focus({ preventScroll: true });
}

function initializeMaps() {
  if (!window.L) {
    setNotice("Map tiles could not be loaded. Location sharing and emergency event recording remain available.");
    return;
  }
  const createMap = (elementId) => {
    const mapElement = byId(elementId);
    if (!mapElement) return null;
    const map = L.map(mapElement, { zoomControl: true }).setView([20, 0], 2);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">OpenStreetMap</a> contributors',
    }).addTo(map);
    return map;
  };
  if (!state.dashboardMap) state.dashboardMap = createMap("dashboard-map");
  if (!state.fullMap) state.fullMap = createMap("full-map");
  if (state.currentLocation) updateMapMarkers(state.currentLocation);
}

function updateMapMarkers(location) {
  state.currentLocation = location;
  for (const map of [state.dashboardMap, state.fullMap]) {
    if (!map) continue;
    if (state.markers[map._leaflet_id]) state.markers[map._leaflet_id].remove();
    const marker = L.circleMarker([location.latitude, location.longitude], {
      radius: 8, color: "#fff", weight: 3, fillColor: "#17836a", fillOpacity: 1,
    }).addTo(map).bindPopup("Your current location");
    state.markers[map._leaflet_id] = marker;
    map.setView([location.latitude, location.longitude], Math.max(map.getZoom(), 14));
  }
  byId("dashboard-map-placeholder").classList.add("hidden");
  byId("full-map-placeholder").classList.add("hidden");
  const accuracy = Number.isFinite(location.accuracy) ? `±${Math.round(location.accuracy)} m` : "Accuracy unavailable";
  byId("map-accuracy").textContent = accuracy;
  byId("location-summary").textContent = "Sharing location";
  byId("location-detail").textContent = `${location.latitude.toFixed(5)}, ${location.longitude.toFixed(5)}`;
  byId("full-location-status").textContent = "Sharing location";
  byId("full-location-accuracy").textContent = accuracy;
  byId("full-location-time").textContent = new Date(location.timestamp || location.captured_at || Date.now()).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  byId("location-toggle").textContent = "Stop sharing location";
  byId("map-tracking-toggle").textContent = "Stop sharing location";
  byId("setting-location").textContent = "ON";
  byId("setting-location").classList.add("state-on");
}

function locationFailure(error) {
  const messages = {
    1: "Location permission was denied. Allow location access in your browser settings to use the live map.",
    2: "Your device could not determine a location. Check GPS or try again outdoors.",
    3: "Location request timed out. Check your device settings and try again.",
  };
  stopLocationSharing(false);
  const message = messages[error.code] || "Location is unavailable on this device.";
  setNotice(message);
  byId("location-summary").textContent = "Unavailable";
  byId("location-detail").textContent = message;
  byId("full-location-status").textContent = "Unavailable";
  byId("map-tracking-toggle").textContent = "Try location again";
}

function startLocationSharing() {
  if (!navigator.geolocation) {
    notify("Geolocation is not supported by this browser.", true);
    return;
  }
  if (state.locationWatch !== null) return;
  state.locationWatch = navigator.geolocation.watchPosition(onPosition, locationFailure, {
    enableHighAccuracy: true, maximumAge: 5000, timeout: 20000,
  });
  byId("location-summary").textContent = "Finding location…";
  byId("location-detail").textContent = "Waiting for device GPS";
  byId("map-tracking-toggle").textContent = "Finding location…";
  byId("map-tracking-toggle").disabled = true;
  byId("location-toggle").disabled = true;
  byId("map-enable-location").disabled = true;
}

async function stopLocationSharing(showToast = true, revokeShares = true) {
  if (state.locationWatch !== null && navigator.geolocation) navigator.geolocation.clearWatch(state.locationWatch);
  state.locationWatch = null;
  const sharing = byId("map-tracking-toggle");
  if (sharing) {
    sharing.disabled = false;
    sharing.textContent = "Start sharing location";
    byId("location-toggle").textContent = "Share my location";
    byId("location-toggle").disabled = false;
    byId("map-enable-location").disabled = false;
    byId("setting-location").textContent = "OFF";
    byId("setting-location").classList.remove("state-on");
    byId("location-summary").textContent = "Not active";
    byId("location-detail").textContent = "Your location stays private";
    byId("full-location-status").textContent = "Not sharing";
    byId("dashboard-map-placeholder").classList.remove("hidden");
    byId("full-map-placeholder").classList.remove("hidden");
  }
  if (revokeShares && state.token && state.shares.length) {
    const shares = [...state.shares];
    const results = await Promise.allSettled(
      shares.map((share) => api(`/location/shares/${encodeURIComponent(share.id)}`, { method: "DELETE" })),
    );
    const revokedIds = new Set(
      shares.filter((_, index) => results[index].status === "fulfilled").map((share) => share.id),
    );
    for (const id of revokedIds) delete state.shareUrls[id];
    state.shares = state.shares.filter((share) => !revokedIds.has(share.id));
    renderShares();
    if (results.some((result) => result.status === "rejected")) {
      notify("Location sharing stopped on this device, but one or more links could not be revoked. Revoke those links from the map when online.", true);
    }
  }
  if (showToast && sharing) notify("Location sharing stopped.");
}

async function onPosition(position) {
  if (state.locationWatch === null) return;
  const coords = position.coords;
  const location = {
    latitude: coords.latitude, longitude: coords.longitude, accuracy: coords.accuracy,
    speed: coords.speed, heading: coords.heading, altitude: coords.altitude,
    timestamp: position.timestamp,
  };
  updateMapMarkers(location);
  byId("map-tracking-toggle").disabled = false;
  byId("location-toggle").disabled = false;
  byId("map-enable-location").disabled = false;
  setNotice("", false);
  const now = Date.now();
  if (now - state.lastLocationSentAt < 30000) return;
  state.lastLocationSentAt = now;
  try {
    const activeTrip = state.trips.find((trip) => trip.status === "ACTIVE");
    await api("/location", {
      method: "POST",
      body: JSON.stringify({
        latitude: location.latitude, longitude: location.longitude, accuracy: location.accuracy,
        speed: location.speed, heading: location.heading, altitude: location.altitude,
        captured_at: new Date(position.timestamp).toISOString(), trip_id: activeTrip?.id || null,
      }),
    });
    state.locationSyncWarned = false;
  } catch (error) {
    if (!state.locationSyncWarned) {
      notify(`GPS is available on this device, but the latest location could not be synced: ${error.message}`, true);
      state.locationSyncWarned = true;
    }
  }
}

async function refreshDashboard() {
  const [trips, contacts, events, shares] = await Promise.all([
    api("/trips"), api("/emergency-contacts"), api("/emergency/events"), api("/location/shares"),
  ]);
  state.trips = trips;
  state.contacts = contacts;
  state.activeEvent = events.find((event) => event.status === "ACTIVE") || null;
  state.shares = shares;
  renderTrips();
  renderContacts();
  renderEmergencyContacts();
  renderEvents(events);
  renderShares();
  renderActiveTrip();
}

function renderTrips() {
  const list = byId("trip-list");
  if (!state.trips.length) {
    list.innerHTML = '<div class="empty-state"><span class="empty-illustration">↗</span><strong>No trips yet</strong><p>Trips you plan will appear here.</p></div>';
    byId("trip-summary").textContent = "No active trip";
    byId("trip-detail").textContent = "Plan a trip to get started";
    return;
  }
  list.innerHTML = state.trips.map((trip) => {
    const isOngoing = ["ACTIVE", "PAUSED", "PLANNED"].includes(trip.status);
    const action = trip.status === "PLANNED" ? `<button class="small-action" data-trip-action="ACTIVE" data-id="${escapeHTML(trip.id)}">Start</button>`
      : trip.status === "ACTIVE" ? `<button class="small-action" data-trip-action="PAUSED" data-id="${escapeHTML(trip.id)}">Pause</button>`
        : trip.status === "PAUSED" ? `<button class="small-action" data-trip-action="ACTIVE" data-id="${escapeHTML(trip.id)}">Resume</button>` : "";
    return `<div class="list-item"><div class="list-item-icon">↗</div><div class="list-item-body"><strong>${escapeHTML(trip.name)}</strong><small>${escapeHTML(trip.destination)} · ${escapeHTML(trip.status.toLowerCase())}</small></div><div class="list-item-actions">${action}${isOngoing ? `<button class="small-action" data-trip-action="COMPLETED" data-id="${escapeHTML(trip.id)}">End</button>` : ""}<button class="small-action danger" data-trip-delete="${escapeHTML(trip.id)}" aria-label="Delete trip">Delete</button></div></div>`;
  }).join("");
  const current = state.trips.find((trip) => trip.status === "ACTIVE") || state.trips.find((trip) => trip.status === "PLANNED");
  byId("trip-summary").textContent = current?.status === "ACTIVE" ? "Trip in progress" : current ? "Trip planned" : "No active trip";
  byId("trip-detail").textContent = current ? current.destination : "Plan a trip to get started";
}

function renderActiveTrip() {
  const current = state.trips.find((trip) => ["ACTIVE", "PAUSED"].includes(trip.status));
  byId("trip-status-badge").textContent = current?.status || "No trip";
  byId("trip-status-badge").className = `badge ${current ? "badge-active" : "badge-muted"}`;
  byId("current-trip-content").classList.toggle("hidden", Boolean(current));
  byId("current-trip-active").classList.toggle("hidden", !current);
  if (!current) return;
  byId("trip-destination-label").textContent = current.destination;
  byId("trip-name-label").textContent = current.name;
  byId("trip-arrival-label").textContent = current.expected_end_time ? new Date(current.expected_end_time).toLocaleString() : "Not set";
  byId("trip-pause").textContent = current.status === "ACTIVE" ? "Pause trip" : "Resume trip";
  byId("trip-pause").dataset.tripAction = current.status === "ACTIVE" ? "PAUSED" : "ACTIVE";
  byId("trip-pause").dataset.id = current.id;
}

function renderContacts() {
  const list = byId("contacts-list");
  byId("contact-count").textContent = `${state.contacts.length} contact${state.contacts.length === 1 ? "" : "s"} added`;
  if (!state.contacts.length) {
    list.innerHTML = '<div class="empty-state compact"><strong>No contacts saved</strong><p>Add someone you trust for reference.</p></div>';
    return;
  }
  list.innerHTML = state.contacts.map((contact) => `<div class="list-item"><div class="list-item-icon">${escapeHTML(contact.name.charAt(0).toUpperCase())}</div><div class="list-item-body"><strong>${escapeHTML(contact.name)}</strong><small>${escapeHTML(contact.phone)}${contact.relationship ? ` · ${escapeHTML(contact.relationship)}` : ""}</small></div><button class="small-action" data-contact-edit="${escapeHTML(contact.id)}">Edit</button><button class="small-action danger" data-contact-delete="${escapeHTML(contact.id)}" aria-label="Remove ${escapeHTML(contact.name)}">Remove</button></div>`).join("");
}

function renderEmergencyContacts() {
  const list = byId("emergency-contact-list");
  if (!state.contacts.length) {
    list.innerHTML = '<div class="empty-state compact"><strong>No emergency contacts yet</strong><p>Add someone you trust in Profile & contacts.</p></div>';
    return;
  }
  list.innerHTML = state.contacts.map((contact) => `<div class="list-item"><div class="list-item-icon">${escapeHTML(contact.name.charAt(0).toUpperCase())}</div><div class="list-item-body"><strong>${escapeHTML(contact.name)}</strong><small>${escapeHTML(contact.phone)}</small></div></div>`).join("");
}

function renderEvents(events) {
  const list = byId("history-events");
  if (!events.length) {
    list.innerHTML = '<div class="empty-state compact"><strong>No emergency events</strong><p>If you activate emergency mode, its status appears here.</p></div>';
  } else {
    list.innerHTML = events.map((event) => `<div class="list-item"><div class="list-item-icon">${event.status === "ACTIVE" ? "!" : "✓"}</div><div class="list-item-body"><strong>${escapeHTML(event.event_type.replaceAll("_", " "))} · ${escapeHTML(event.status)}</strong><small>${new Date(event.created_at).toLocaleString()} · SMS: ${escapeHTML(event.notification_status || "unknown")}</small></div></div>`).join("");
  }
  byId("cancel-sos").classList.toggle("hidden", !state.activeEvent);
  byId("emergency-event-status").textContent = state.activeEvent
    ? `Emergency mode is active. SMS status: ${state.activeEvent.notification_status || "unknown"}.`
    : "No active emergency event.";
  if (state.activeEvent) {
    byId("emergency-heading").textContent = "Emergency mode is active";
    byId("emergency-copy").textContent = `Emergency event recorded. SMS status: ${state.activeEvent.notification_status || "unknown"}. Contact local emergency services directly if needed.`;
  }
}

async function createTrip(event) {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  try {
    const arrival = byId("trip-arrival").value ? new Date(byId("trip-arrival").value).toISOString() : null;
    await api("/trips", { method: "POST", body: JSON.stringify({
      name: byId("trip-name").value.trim(),
      destination: byId("trip-destination").value.trim(),
      expected_end_time: arrival,
    }) });
    byId("trip-form").reset();
    await refreshDashboard();
    notify("Trip saved.");
  } catch (error) {
    notify(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function setTripStatus(id, status) {
  try {
    await api(`/trips/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify({ status }) });
    await refreshDashboard();
    notify(`Trip ${status.toLowerCase()}.`);
  } catch (error) {
    notify(error.message, true);
  }
}

async function removeTrip(id) {
  if (!window.confirm("Delete this trip and its saved trip details?")) return;
  try {
    await api(`/trips/${encodeURIComponent(id)}`, { method: "DELETE" });
    await refreshDashboard();
    notify("Trip deleted.");
  } catch (error) {
    notify(error.message, true);
  }
}

async function saveContact(event) {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  const payload = {
    name: byId("contact-name").value.trim(), phone: byId("contact-phone").value.trim(),
    relationship: byId("contact-relationship").value.trim(), email: byId("contact-email").value.trim(),
  };
  try {
    await api("/emergency-contacts", { method: "POST", body: JSON.stringify(payload) });
    byId("contact-form").reset();
    await refreshDashboard();
    notify("Emergency contact saved.");
  } catch (error) {
    notify(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function removeContact(id) {
  if (!window.confirm("Remove this emergency contact?")) return;
  try {
    await api(`/emergency-contacts/${encodeURIComponent(id)}`, { method: "DELETE" });
    await refreshDashboard();
    notify("Emergency contact removed.");
  } catch (error) {
    notify(error.message, true);
  }
}

async function recordCheckIn() {
  const button = byId("checkin-button");
  button.disabled = true;
  try {
    const activeTrip = state.trips.find((trip) => trip.status === "ACTIVE");
    const result = await api("/checkin", {
      method: "POST", body: JSON.stringify({ trip_id: activeTrip?.id || null, note: "" }),
    });
    byId("last-checkin").textContent = `Last check-in recorded at ${new Date(result.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}.`;
    byId("checkin-summary").textContent = "Just checked in";
    byId("checkin-detail").textContent = new Date(result.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    notify("Your check-in was saved.");
  } catch (error) {
    notify(error.message, true);
  } finally {
    button.disabled = false;
  }
}

let holdTimer = null;
let holdElapsed = false;
function showEmergencyConfirmation() {
  byId("confirm-modal").classList.remove("hidden");
  byId("modal-confirm").focus();
}

async function activateEmergency() {
  byId("modal-confirm").disabled = true;
  byId("modal-confirm").textContent = "Activating…";
  try {
    const activeTrip = state.trips.find((trip) => trip.status === "ACTIVE");
    const location = state.locationWatch === null ? await requestCurrentLocation() : state.currentLocation;
    const result = await api("/emergency/sos", {
      method: "POST", body: JSON.stringify({
        event_type: "SOS",
        latitude: location?.latitude ?? null,
        longitude: location?.longitude ?? null,
        accuracy: location?.accuracy ?? null,
        trip_id: activeTrip?.id || null,
      }),
    });
    byId("confirm-modal").classList.add("hidden");
    await refreshDashboard();
    byId("emergency-heading").textContent = "Emergency mode is active";
    byId("emergency-copy").textContent = result.message;
    if (location && state.locationWatch === null) {
      updateMapMarkers(location);
      startLocationSharing();
      state.emergencyStartedTracking = true;
    }
    notify(result.message, ["failed", "partially_failed"].includes(result.notification_status));
    navigate("emergency");
  } catch (error) {
    notify(error.message, true);
  } finally {
    byId("modal-confirm").disabled = false;
    byId("modal-confirm").textContent = "Activate emergency";
  }
}

function requestCurrentLocation() {
  if (!navigator.geolocation) return Promise.resolve(null);
  return new Promise((resolve) => {
    navigator.geolocation.getCurrentPosition(
      ({ coords, timestamp }) => resolve({
        latitude: coords.latitude,
        longitude: coords.longitude,
        accuracy: coords.accuracy,
        speed: coords.speed,
        heading: coords.heading,
        altitude: coords.altitude,
        timestamp,
      }),
      () => resolve(null),
      { enableHighAccuracy: true, maximumAge: 5000, timeout: 8000 },
    );
  });
}

async function cancelEmergency() {
  try {
    await api("/emergency/cancel", { method: "POST" });
    if (state.emergencyStartedTracking) {
      await stopLocationSharing(false);
      state.emergencyStartedTracking = false;
    }
    byId("emergency-heading").textContent = "Need immediate help?";
    byId("emergency-copy").textContent = "Use the button only if you need to record an emergency event. Confirm before activation.";
    await refreshDashboard();
    notify("Emergency mode ended.");
  } catch (error) {
    notify(error.message, true);
  }
}

function setCapturedImage(blob) {
  state.imageBlob = blob;
  byId("scan-detect").disabled = !blob;
  byId("scan-ocr").disabled = !blob;
}

async function startCamera() {
  const errorBox = byId("camera-error");
  errorBox.classList.add("hidden");
  if (!navigator.mediaDevices?.getUserMedia) {
    errorBox.textContent = "Camera access is unavailable in this browser or context. Use an image upload instead.";
    errorBox.classList.remove("hidden");
    return;
  }
  try {
    state.mediaStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: "environment" } }, audio: false });
    const video = byId("camera-video");
    video.srcObject = state.mediaStream;
    video.classList.remove("hidden");
    byId("camera-empty").classList.add("hidden");
    byId("camera-start").disabled = true;
    byId("camera-capture").disabled = false;
    byId("camera-stop").disabled = false;
  } catch (error) {
    errorBox.textContent = error.name === "NotAllowedError"
      ? "Camera permission was denied. Enable access in your browser settings or upload an image."
      : "The camera could not be opened. Check that another app is not using it, or upload an image.";
    errorBox.classList.remove("hidden");
  }
}

function stopCamera() {
  if (state.mediaStream) state.mediaStream.getTracks().forEach((track) => track.stop());
  state.mediaStream = null;
  const video = byId("camera-video");
  if (video) {
    video.srcObject = null;
    video.classList.add("hidden");
  }
  if (byId("camera-empty")) byId("camera-empty").classList.remove("hidden");
  if (byId("camera-start")) byId("camera-start").disabled = false;
  if (byId("camera-capture")) byId("camera-capture").disabled = true;
  if (byId("camera-stop")) byId("camera-stop").disabled = true;
}

async function captureCameraImage() {
  const video = byId("camera-video");
  if (!state.mediaStream || !video.videoWidth) {
    notify("Start the camera and wait for the preview before capturing.", true);
    return;
  }
  const canvas = byId("capture-canvas");
  canvas.width = video.videoWidth;
  canvas.height = video.videoHeight;
  canvas.getContext("2d").drawImage(video, 0, 0);
  canvas.toBlob((blob) => {
    if (blob) {
      setCapturedImage(blob);
      notify("Image captured. Scanning for plate text.");
      void runScan("ocr");
    } else notify("The camera frame could not be captured.", true);
  }, "image/jpeg", 0.88);
}

async function runScan(kind) {
  if (!state.imageBlob) return;
  const button = byId(kind === "detect" ? "scan-detect" : "scan-ocr");
  button.disabled = true;
  byId("scan-results").innerHTML = '<div class="empty-state"><strong>Working…</strong><p>Processing this image locally or through the configured service.</p></div>';
  byId("scan-result-title").textContent = kind === "detect" ? "Object observations" : "Plate text (OCR)";
  const form = new FormData();
  form.append("file", state.imageBlob, "traveler-scan.jpg");
  try {
    const result = await api(kind === "detect" ? "/ai/detect" : "/ocr/scan", { method: "POST", body: form });
    if (kind === "detect") {
      const rows = result.detections.length
        ? result.detections.map((detection) => `<div class="scan-observation"><strong>${escapeHTML(detection.class)}</strong><small>Confidence ${Math.round(detection.confidence * 100)}% · Box ${detection.bbox.map((v) => Math.round(v)).join(", ")}</small></div>`).join("")
        : '<div class="scan-observation"><strong>No detections returned</strong><small>This does not indicate whether an area is safe or unsafe.</small></div>';
      byId("scan-results").innerHTML = `${rows}<p class="scanner-note">${escapeHTML(result.notice)}</p>`;
    } else {
      const text = result.text ? escapeHTML(result.text) : "No text could be extracted from this image.";
      const confidence = result.confidence === null ? "Confidence unavailable" : `Confidence ${escapeHTML(result.confidence)}%`;
      const plateCandidates = result.plate_candidates?.length
        ? `<div class="scan-observation"><strong>Possible plate text — verify</strong><small>${escapeHTML(result.plate_candidates.join(" · "))}</small></div>`
        : "";
      byId("scan-results").innerHTML = `${plateCandidates}<div class="scan-observation"><strong>Detected text</strong><small style="white-space:pre-wrap">${text}</small><small>${confidence}</small></div><p class="scanner-note">${escapeHTML(result.warning || "Verify extracted text against the original image.")}</p><div class="camera-actions"><button id="copy-scan-text" class="button button-light" ${result.text ? "" : "disabled"}>Copy</button><button id="download-scan-text" class="button button-outline" ${result.text ? "" : "disabled"}>Download text</button></div>`;
      byId("copy-scan-text").addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(result.text);
          notify("Extracted text copied.");
        } catch {
          notify("Clipboard access is unavailable. Select and copy the text manually.", true);
        }
      });
      byId("download-scan-text").addEventListener("click", () => {
        const file = new Blob([result.text], { type: "text/plain;charset=utf-8" });
        const url = URL.createObjectURL(file);
        const link = document.createElement("a");
        link.href = url;
        link.download = "travelerguard-scan.txt";
        link.click();
        URL.revokeObjectURL(url);
      });
    }
  } catch (error) {
    byId("scan-results").innerHTML = `<div class="scan-observation"><strong>Scan unavailable</strong><small>${escapeHTML(error.message)}</small></div>`;
  } finally {
    button.disabled = !state.imageBlob;
  }
}

async function saveProfile(event) {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  try {
    state.user = await api("/users/me", { method: "PUT", body: JSON.stringify({ full_name: byId("profile-name").value.trim() }) });
    showApp();
    notify("Profile updated.");
  } catch (error) {
    notify(error.message, true);
  } finally {
    button.disabled = false;
  }
}

function renderProfile() {
  if (!state.user) return;
  byId("profile-name").value = state.user.full_name;
  byId("profile-email").value = state.user.email;
}

async function renderHistory() {
  try {
    const trips = await api("/trips");
    const tripHistory = byId("history-trips");
    tripHistory.innerHTML = trips.length ? trips.map((trip) => `<div class="list-item"><div class="list-item-icon">↗</div><div class="list-item-body"><strong>${escapeHTML(trip.name)}</strong><small>${escapeHTML(trip.destination)} · ${escapeHTML(trip.status)} · ${new Date(trip.created_at).toLocaleDateString()}</small></div></div>`).join("") : '<div class="empty-state compact"><strong>No trips recorded</strong><p>Your planned journeys will show here.</p></div>';
    const events = await api("/emergency/events");
    renderEvents(events);
  } catch (error) {
    notify(error.message, true);
  }
}

async function deleteAccount() {
  if (!window.confirm("Permanently delete your account and all associated trips, contacts, locations, and emergency records? This cannot be undone.")) return;
  try {
    await api("/users/me", { method: "DELETE" });
    signOut(false);
    notify("Your account and associated records were deleted.");
  } catch (error) {
    notify(error.message, true);
  }
}

function connectSocket() {
  if (!state.token || !window.WebSocket) return;
  state.socket?.close();
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${protocol}//${location.host}/ws?token=${encodeURIComponent(state.token)}`);
  state.socket = socket;
  socket.addEventListener("message", (message) => {
    try {
      const event = JSON.parse(message.data);
      if (event.type === "emergency_activated") notify("Emergency mode status updated.");
      if (event.type === "sms_received") {
        notify("A contact replied to the active emergency SMS.");
        refreshSmsInbox();
      }
    } catch {
      notify("A live update could not be read.", true);
    }
  });
  socket.addEventListener("error", () => {
    // REST remains available when the optional real-time channel is disconnected.
  });
}

function bindEvents() {
  byId("auth-form").addEventListener("submit", submitAuth);
  byId("auth-switch").addEventListener("click", () => showAuth(state.authMode === "login" ? "register" : "login"));
  byId("sign-out").addEventListener("click", () => signOut());
  byId("menu-toggle").addEventListener("click", () => byId("sidebar").classList.toggle("open"));
  byId("trip-form").addEventListener("submit", createTrip);
  byId("contact-form").addEventListener("submit", saveContact);
  byId("profile-form").addEventListener("submit", saveProfile);
  byId("checkin-button").addEventListener("click", recordCheckIn);
  byId("location-toggle").addEventListener("click", () => state.locationWatch === null ? startLocationSharing() : stopLocationSharing());
  byId("map-enable-location").addEventListener("click", startLocationSharing);
  byId("map-tracking-toggle").addEventListener("click", () => state.locationWatch === null ? startLocationSharing() : stopLocationSharing());
  byId("trip-pause").addEventListener("click", (event) => setTripStatus(event.currentTarget.dataset.id, event.currentTarget.dataset.tripAction));
  byId("refresh-history").addEventListener("click", renderHistory);
  byId("create-share-link").addEventListener("click", createLocationShare);
  byId("refresh-sms-inbox").addEventListener("click", refreshSmsInbox);
  byId("camera-start").addEventListener("click", startCamera);
  byId("camera-capture").addEventListener("click", captureCameraImage);
  byId("camera-stop").addEventListener("click", stopCamera);
  byId("scan-detect").addEventListener("click", () => runScan("detect"));
  byId("scan-ocr").addEventListener("click", () => runScan("ocr"));
  byId("sos-button").addEventListener("pointerdown", (event) => {
    if (event.pointerType === "mouse" && event.button !== 0) return;
    holdElapsed = false;
    holdTimer = window.setTimeout(() => {
      holdElapsed = true;
      showEmergencyConfirmation();
    }, 3000);
  });
  for (const name of ["pointerup", "pointerleave", "pointercancel"]) {
    byId("sos-button").addEventListener(name, () => window.clearTimeout(holdTimer));
  }
  byId("sos-button").addEventListener("click", (event) => {
    if (event.detail === 0 && !holdElapsed) showEmergencyConfirmation();
  });
  byId("modal-cancel").addEventListener("click", () => byId("confirm-modal").classList.add("hidden"));
  byId("modal-confirm").addEventListener("click", activateEmergency);
  byId("cancel-sos").addEventListener("click", cancelEmergency);
  byId("footer-disclaimer").addEventListener("click", () => notify("TravelerGuard supports awareness only. In immediate danger, call local emergency services directly."));
  byId("delete-account").addEventListener("click", deleteAccount);
  byId("image-upload").addEventListener("change", async (event) => {
    const file = event.target.files?.[0];
    if (!file) return;
    if (!["image/jpeg", "image/png", "image/webp"].includes(file.type) || file.size > 5 * 1024 * 1024) {
      setCapturedImage(null);
      event.target.value = "";
      notify("Choose a JPEG, PNG or WEBP image up to 5 MB.", true);
      return;
    }
    setCapturedImage(file);
    byId("camera-error").classList.add("hidden");
    byId("camera-empty").innerHTML = "<span>✓</span><strong>Image ready</strong><small>Choose an available scan to continue.</small>";
    await runScan("ocr");
  });
  document.addEventListener("click", (event) => {
    const pageTarget = event.target.closest("[data-page]");
    if (pageTarget) {
      event.preventDefault();
      navigate(pageTarget.dataset.page);
      if (pageTarget.dataset.page === "profile") renderProfile();
      if (pageTarget.dataset.page === "history") renderHistory();
    }
    const tripAction = event.target.closest("[data-trip-action]");
    if (tripAction) setTripStatus(tripAction.dataset.id, tripAction.dataset.tripAction);
    const tripDelete = event.target.closest("[data-trip-delete]");
    if (tripDelete) removeTrip(tripDelete.dataset.tripDelete);
    const contactDelete = event.target.closest("[data-contact-delete]");
    if (contactDelete) removeContact(contactDelete.dataset.contactDelete);
    const contactEdit = event.target.closest("[data-contact-edit]");
    if (contactEdit) editContact(contactEdit.dataset.contactEdit);
    const revokeShare = event.target.closest("[data-share-revoke]");
    if (revokeShare) revokeLocationShare(revokeShare.dataset.shareRevoke);
    const copyShare = event.target.closest("[data-share-copy]");
    if (copyShare) copyLocationShare(copyShare.dataset.shareCopy);
  });
  window.addEventListener("online", setNetworkStatus);
  window.addEventListener("offline", setNetworkStatus);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      byId("confirm-modal").classList.add("hidden");
      byId("sidebar").classList.remove("open");
    }
  });
}

async function initialize() {
  bindEvents();
  setNetworkStatus();
  if (!state.token) {
    showAuth();
    return;
  }
  try {
    state.user = await api("/users/me");
    showApp();
    renderProfile();
  } catch {
    clearExpiredSession();
  }
}

initialize();
