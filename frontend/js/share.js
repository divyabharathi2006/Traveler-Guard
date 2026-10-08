const token = new URLSearchParams(location.hash.slice(1)).get("token");
const statusElement = document.getElementById("share-status");
const coordinateElement = document.getElementById("share-coordinate");
const fallback = document.getElementById("share-fallback");
const mapElement = document.getElementById("share-map");
let map;
let marker;
let pollTimer;

function showState(title, detail, status) {
  fallback.classList.remove("hidden");
  mapElement.classList.add("hidden");
  fallback.innerHTML = `<strong>${title}</strong><span>${detail}</span>`;
  statusElement.textContent = status;
  coordinateElement.textContent = "";
}

async function updateLocation() {
  if (!token) {
    showState("Invalid location link", "Ask the traveler to create a new link.", "This link is invalid.");
    return;
  }
  try {
    const response = await fetch("/api/location/shared", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
      cache: "no-store",
    });
    const data = await response.json();
    if (!response.ok) {
      window.clearInterval(pollTimer);
      showState("This location link is no longer active", "It may have expired or been revoked by the traveler.", "Sharing has ended.");
      return;
    }
    if (!data.location) {
      statusElement.textContent = "Waiting for the traveler to share a GPS location…";
      coordinateElement.textContent = `Link expires ${new Date(data.expires_at).toLocaleString()}.`;
      return;
    }
    const location = data.location;
    mapElement.classList.remove("hidden");
    fallback.classList.add("hidden");
    if (!window.L) {
      showState("Map is unavailable", "The location is still available as coordinates below.", "Map tiles could not be loaded.");
      coordinateElement.textContent = `${location.latitude.toFixed(5)}, ${location.longitude.toFixed(5)}`;
      return;
    }
    if (!map) {
      map = L.map(mapElement).setView([location.latitude, location.longitude], 15);
      L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19,
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">OpenStreetMap</a> contributors',
      }).addTo(map);
      marker = L.circleMarker([location.latitude, location.longitude], {
        radius: 9, color: "#fff", weight: 3, fillColor: "#17836a", fillOpacity: 1,
      }).addTo(map);
    } else {
      marker.setLatLng([location.latitude, location.longitude]);
      map.setView([location.latitude, location.longitude]);
    }
    const updated = new Date(location.captured_at);
    const isStale = Date.now() - updated.getTime() > 120000;
    statusElement.textContent = `${isStale ? "Last location update" : "Location updated"} ${updated.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}${isStale ? " · May be out of date" : ""}`;
    coordinateElement.textContent = `Accuracy ±${Math.round(location.accuracy || 0)} m · Link expires ${new Date(data.expires_at).toLocaleString()}`;
    window.setTimeout(() => map.invalidateSize(), 80);
  } catch {
    statusElement.textContent = "Connection unavailable. The page will retry.";
  }
}

if (token) {
  updateLocation();
  pollTimer = window.setInterval(updateLocation, 10000);
} else {
  showState("Invalid location link", "Ask the traveler to create a new link.", "This link is invalid.");
}
