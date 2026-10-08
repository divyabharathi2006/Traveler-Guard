# TravelerGuard AI

**Intelligent Traveler Safety & Emergency Platform**

TravelerGuard is a mobile-first travel safety workspace for planning a trip, intentionally sharing device location, recording check-ins and emergency events, and optionally scanning images for objects or text. It is a support tool—not a replacement for local emergency services. GPS, maps, AI, internet access, and notification systems may be unavailable or inaccurate.

## Design direction

The interface uses a calm, high-readability palette, clear status language, spacious cards, visible focus states, reduced-motion support, and a confirmation step for emergency mode. The visual direction is informed by [Material Design color guidance](https://m3.material.io/styles/color/overview) and the [W3C WCAG 2.2 quick reference](https://www.w3.org/WAI/WCAG22/quickref/). Maps use [Leaflet](https://leafletjs.com/reference.html) with OpenStreetMap tiles and visible attribution. Map tile loading requires an internet connection.

## Features

- Registration, Argon2id password hashing, expiring JWTs, and protected account endpoints.
- Responsive dashboard with a live map that does not display fabricated coordinates.
- Explicit browser GPS permission and start/stop location sharing; location sync is limited to once per 30 seconds.
- Trip creation, status changes, history, and deletion.
- Emergency contact create/read/update/delete with E.164 phone validation.
- Press-and-hold SOS followed by a confirmation dialog, event history, duplicate activation guard, and cancellation.
- Optional production-only Twilio SMS alerts to saved emergency contacts; provider delivery callbacks update the recorded status.
- Signature-validated incoming Twilio SMS replies, associated with active emergency events and visible in the app inbox.
- Expiring, revocable live-location links that display the latest location only while the browser is sharing GPS.
- Check-in recording without automatic emergency escalation.
- Camera and image upload controls with MIME, file-size, image-format, and image-dimension checks.
- Automatic local OCR for uploaded or captured images, including heuristic possible number-plate text; optional YOLO detection returns an explicit error when unavailable.
- REST API, authenticated user WebSocket events, health endpoint, structured error responses, and security headers.
- SQLite for local development; PostgreSQL adapter for Docker/production deployments.

## Architecture

```text
backend/app/
  main.py                 FastAPI routes, validation, health, WebSocket
  config.py               Environment configuration
  database.py             SQLite/PostgreSQL connections and schema
  security.py             Argon2 password hashing and JWT auth
  schemas.py              Pydantic input validation
  ai/providers.py         Optional YOLO/OCR and image validation
frontend/
  index.html              Accessible single-page app views
  css/styles.css          Responsive theme and component styles
  js/app.js               API integration, GPS, map, camera, and interactions
```

The app serves the frontend and API from the same FastAPI origin. The maps provider is client-side Leaflet/OpenStreetMap and requires no key. API secrets are read from environment variables; never commit a real `.env`.

## Start locally

Requirements: Python 3.11+.

```powershell
cd backend
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open `http://localhost:8000`. Create an account from the sign-in screen. The default local database is `travelerguard.db` in the project root. To use the one-command launcher, from the project root run `py run.py`.

For PostgreSQL, set `DATABASE_URL` to a `postgresql://` connection string and ensure PostgreSQL is reachable. The Docker Compose configuration starts PostgreSQL and the app together.

## Environment variables

Copy `.env.example` to `.env` for local configuration and replace all example secrets:

| Variable | Purpose |
|---|---|
| `APP_ENV` | `development` or `production`; production requires an explicit `SECRET_KEY`. |
| `SECRET_KEY` | Secret used to sign JWTs. Use a unique, high-entropy secret. |
| `DATABASE_URL` | Leave empty for SQLite at the project root; set a `postgresql://` URL for PostgreSQL. |
| `CORS_ORIGINS` | Comma-separated allowed origins. Keep this restricted in production. |
| `TOKEN_EXPIRE_MINUTES` | JWT lifetime in minutes (default 60). |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | Credentials for the separate, administrator-only vehicle-record portal. Keep the password in an untracked `.env` or secret manager; production requires at least 12 characters. |
| `TESSERACT_CMD` | Optional path to `tesseract.exe` when it is not available on `PATH` or a standard Windows install path. |
| `YOLO_MODEL_PATH` | Path to a local YOLO model; empty means object detection is unavailable. |
| `OCR_LANGUAGE` | Tesseract language code (default `eng`); configure installed language data as needed. |
| `NOTIFICATION_PROVIDER` | Set `twilio` to configure Twilio; messages are still blocked unless `APP_ENV=production`. |
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` | Twilio credentials. Store real values only in an untracked `.env` or a secret manager. |
| `TWILIO_FROM_NUMBER` | Twilio-enabled sender number in E.164 format. |
| `PUBLIC_BASE_URL` | Public HTTPS URL used when generating live location share links and SMS links. |
| `TWILIO_WEBHOOK_BASE_URL` | Public HTTPS origin Twilio uses for inbound SMS and message status webhooks. |

## Twilio SMS and live location setup

Install the current requirements, provide the Twilio credentials and a Twilio-enabled E.164 sender in `.env`, set `NOTIFICATION_PROVIDER=twilio`, and deploy the app behind HTTPS. **Real emergency SMS is intentionally sent only when `APP_ENV=production`.** Test and development mode do not send messages even if credentials are present.

Configure the Twilio number's incoming-message webhook as:

```text
https://your-public-host.example/api/twilio/sms/inbound
```

Use HTTP POST and set `TWILIO_WEBHOOK_BASE_URL=https://your-public-host.example` so request signatures can be verified behind a proxy. Configure a status callback at `/api/twilio/sms/status` on the same host. Twilio webhooks are rejected unless their `X-Twilio-Signature` validates. Incoming replies from saved emergency-contact phone numbers are recorded only when that account has an active emergency event.

An authenticated user can create a private live-location link from **Live map**. The browser asks for permission and begins sending GPS updates at most once every 30 seconds. The link is a bearer secret: anyone with it can view the latest shared position until it expires (four hours) or is revoked. Stopping sharing revokes the active links when online. Sharing relies on the user's device, internet, browser, and GPS; it does not guarantee availability. `PUBLIC_BASE_URL` must be the actual public HTTPS site URL if recipients need to open the link.

## OCR and optional AI setup

OCR's Python wrapper is installed with the base requirements. Install the Tesseract executable and English language data on the host (for example, `winget install --id UB-Mannheim.TesseractOCR --exact` on Windows); the app checks the standard Windows installation paths, or set `TESSERACT_CMD` to the executable path. The Docker image installs Tesseract automatically. Check `/health` and confirm `services.ocr` is `healthy`. Install `backend/requirements-ai.txt` and provide a trusted local model via `YOLO_MODEL_PATH` only to enable optional YOLO object detection. OCR output and possible plate text are unverified assistive guesses. Images are processed in memory and are not persisted by the app. The app does not perform online vehicle-registration or owner lookups.

## Administrator vehicle records

Configure `ADMIN_USERNAME` and `ADMIN_PASSWORD` in an untracked `.env` file, then open `/admin.html`. The administrator-only portal can add, search, and delete vehicle records (registration/plate, owner name, make, model, color, region, and notes). It stores manually entered records in the app database; it does not query an online registry. The admin token is kept in session storage and admin credentials are never shipped as source-code defaults. Use a strong password, especially before deployment; production startup rejects passwords shorter than 12 characters.

## Docker

```powershell
docker compose up --build
```

Visit `http://localhost:8000`. The compose file's default database password and app signing key are for local development only. Before any network deployment, set strong `POSTGRES_PASSWORD` and `SECRET_KEY` environment values, set `APP_ENV=production`, configure TLS and trusted origins, and review the deployment security requirements.

## Tests

```powershell
cd backend
pip install -r requirements-dev.txt
pytest
```

Tests cover authentication, authorization, contact and trip operations, SOS duplicate protection/cancellation, location validation, and health reporting. Tests do not send emergency messages.

## Safety and privacy limits

- Location is not collected until the user starts browser location sharing. Stop sharing to end GPS tracking.
- Email, phone calls, automatic emergency escalation, and automatic missed-check-in notifications are not implemented. SMS attempts require production-mode Twilio configuration and still may fail or be delayed; provider acceptance is not final carrier delivery.
- Incoming SMS webhook delivery requires a public HTTPS origin and correctly configured Twilio URL/signature token. Only replies from saved contacts during an active event are associated with an account.
- Live-location URLs are bearer secrets. They expire after four hours and are revoked when the user stops sharing (while online), signs out (while online), deletes the account, or revokes them manually. A recipient can retain or forward a link while it is active.
- No fixed emergency number is hard-coded; use the correct local emergency service number for your region.
- Check-ins do not trigger an automatic alert if missed.
- Nearby emergency services, geocoding, and routing are not implemented. Do not infer or display unverified emergency-service listings.
- No model is bundled. AI scanning reports unavailable until a local model/tool is configured.
- The backend currently creates its tables at startup; production deployments should replace startup schema creation with a reviewed migration workflow and configure backups, monitoring, rate limiting, retention, and account deletion before launch.
- JWTs are stored in browser local storage for this starter. Production deployments should assess an HttpOnly secure-cookie strategy, rate limiting, and CSP before handling sensitive user accounts.

## Troubleshooting

- **Cannot register/sign in:** Check the server output and `SECRET_KEY`; the API is at `/api/health`.
- **Map blank:** Map tiles need network access. GPS markers are shown only after the user enables location sharing.
- **Location unavailable:** Use HTTPS (or localhost), grant browser permission, and check device GPS settings.
- **Camera unavailable:** Use HTTPS (or localhost), grant camera permission, or choose an image upload.
- **OCR unavailable:** Install the base requirements and the Tesseract executable with English language data, then verify `services.ocr` at `/health`.
- **Object detection unavailable:** Install `backend/requirements-ai.txt`, configure a local model, and verify `/health`; the app will not substitute fake outputs.
- **Emergency contact was not notified:** This is expected. Notification delivery is disabled until an actual provider is configured and verified.
