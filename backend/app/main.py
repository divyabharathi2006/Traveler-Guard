import logging
import hashlib
import hmac
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from secrets import token_urlsafe
from uuid import uuid4
from xml.sax.saxutils import escape as xml_escape

import anyio
import jwt
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from .ai.providers import detect_objects, extract_text, service_state, validate_image
from .config import PROJECT_ROOT, settings
from .database import connection, initialize_database
from .schemas import (
    CheckInRequest,
    AdminLoginRequest,
    ContactRequest,
    EmergencyRequest,
    LocationRequest,
    LoginRequest,
    ProfileRequest,
    RegisterRequest,
    SharedLocationRequest,
    TripRequest,
    TripStatusRequest,
    VehicleRecordRequest,
    normalize_plate_number,
)
from .security import create_access_token, current_admin, current_user, hash_password, verify_password
from .services.sms import send_sms, twilio_is_configured

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("travelerguard")


@asynccontextmanager
async def lifespan(application):
    if settings.app_env.lower() == "production":
        if len(settings.secret_key) < 32 or settings.secret_key.startswith("local-") or "replace-this" in settings.secret_key:
            raise RuntimeError("Set a unique SECRET_KEY of at least 32 characters before starting in production.")
        if not settings.admin_username or len(settings.admin_password) < 12:
            raise RuntimeError("Set ADMIN_USERNAME and an ADMIN_PASSWORD of at least 12 characters before starting in production.")
    initialize_database()
    yield


app = FastAPI(title="TravelerGuard AI", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(self), geolocation=(self)"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(HTTPException)
async def http_error_handler(request, exc: HTTPException):
    detail = exc.detail
    if isinstance(detail, dict):
        error = detail
    else:
        error = {"code": "REQUEST_FAILED", "message": str(detail)}
    return JSONResponse(
        status_code=exc.status_code,
        content={"success": False, "error": error},
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "success": False,
            "error": {"code": "VALIDATION_ERROR", "message": "Check the submitted fields."},
        },
    )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def public_app_url(request: Request) -> str:
    return settings.public_base_url or str(request.base_url).rstrip("/")


def make_share_token() -> str:
    return token_urlsafe(32)


def sha256_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_location_share_record(db, user_id: str, trip_id: str | None, request: Request) -> dict:
    token = make_share_token()
    created = datetime.now(timezone.utc)
    expires = created + timedelta(minutes=settings.shared_location_ttl_minutes)
    share_id = str(uuid4())
    db.execute(
        "INSERT INTO location_shares(id,user_id,trip_id,token_hash,expires_at,created_at) VALUES(?,?,?,?,?,?)",
        (share_id, user_id, trip_id, sha256_token(token), expires.isoformat(), created.isoformat()),
    )
    return {
        "id": share_id,
        "url": f"{public_app_url(request)}/share.html#token={token}",
        "expires_at": expires.isoformat(),
    }


def twilio_signature_valid(request: Request, form, signature: str | None) -> bool:
    if not settings.twilio_auth_token or not signature:
        return False
    try:
        from twilio.request_validator import RequestValidator
    except ImportError:
        logger.error("Twilio webhook validation unavailable: install the Twilio SDK.")
        return False
    url = str(request.url)
    if settings.twilio_webhook_base_url:
        url = f"{settings.twilio_webhook_base_url}{request.url.path}"
        if request.url.query:
            url = f"{url}?{request.url.query}"
    return RequestValidator(settings.twilio_auth_token).validate(url, form, signature)


def owned_trip(db: sqlite3.Connection, trip_id: str | None, user_id: str):
    if not trip_id:
        return
    row = db.execute("SELECT id FROM trips WHERE id = ? AND user_id = ?", (trip_id, user_id)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "TRIP_NOT_FOUND", "message": "Trip not found."})


@app.get("/health")
def health():
    try:
        with connection() as db:
            db.execute("SELECT 1")
        database_status = "healthy"
    except Exception:
        logger.exception("Database health check failed")
        database_status = "unavailable"
    sms_status = (
        "configured"
        if settings.app_env.lower() == "production" and twilio_is_configured()
        else "disabled"
        if settings.app_env.lower() != "production"
        else "unavailable"
    )
    services = {"database": database_status, "gps": "client-controlled", **service_state(), "notifications": sms_status}
    return {
        "status": "healthy" if database_status == "healthy" else "degraded",
        "services": services,
    }


@app.get("/api/health")
def api_health():
    return health()


@app.post("/api/auth/register", status_code=201)
def register(payload: RegisterRequest):
    user_id = str(uuid4())
    created_at = now_iso()
    try:
        with connection() as db:
            db.execute(
                "INSERT INTO users(id,email,full_name,password_hash,created_at) VALUES(?,?,?,?,?)",
                (user_id, payload.email.lower(), payload.full_name.strip(), hash_password(payload.password), created_at),
            )
    except Exception as exc:
        if isinstance(exc, sqlite3.IntegrityError) or getattr(exc, "sqlstate", None) == "23505":
            raise HTTPException(status_code=409, detail={"code": "EMAIL_EXISTS", "message": "An account with this email already exists."}) from exc
        raise
    return {"access_token": create_access_token(user_id), "token_type": "bearer"}


@app.post("/api/auth/login")
def login(payload: LoginRequest):
    with connection() as db:
        row = db.execute("SELECT id, password_hash FROM users WHERE email = ?", (payload.email.lower(),)).fetchone()
    if row is None or not verify_password(payload.password, row["password_hash"]):
        logger.warning("Authentication failed")
        raise HTTPException(status_code=401, detail={"code": "INVALID_CREDENTIALS", "message": "Email or password is incorrect."})
    return {"access_token": create_access_token(row["id"]), "token_type": "bearer"}


@app.post("/api/admin/login")
def admin_login(payload: AdminLoginRequest):
    if not settings.admin_username or not settings.admin_password:
        raise HTTPException(
            status_code=503,
            detail={"code": "ADMIN_LOGIN_UNCONFIGURED", "message": "Administrator login is not configured. Set ADMIN_USERNAME and ADMIN_PASSWORD."},
        )
    username_matches = hmac.compare_digest(payload.username.encode(), settings.admin_username.encode())
    password_matches = hmac.compare_digest(payload.password.encode(), settings.admin_password.encode())
    if not username_matches or not password_matches:
        logger.warning("Administrator authentication failed")
        raise HTTPException(status_code=401, detail={"code": "INVALID_ADMIN_CREDENTIALS", "message": "Username or password is incorrect."})
    return {
        "access_token": create_access_token(settings.admin_username, role="admin"),
        "token_type": "bearer",
    }


@app.get("/api/admin/vehicles")
def list_vehicle_records(plate_number: str | None = None, admin=Depends(current_admin)):
    with connection() as db:
        if plate_number is None:
            rows = db.execute(
                "SELECT * FROM vehicle_records ORDER BY created_at DESC LIMIT 200"
            ).fetchall()
        else:
            try:
                normalized_plate = normalize_plate_number(plate_number)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail={"code": "INVALID_PLATE", "message": str(exc)}) from exc
            rows = db.execute(
                "SELECT * FROM vehicle_records WHERE plate_number = ?",
                (normalized_plate,),
            ).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/admin/vehicles", status_code=201)
def add_vehicle_record(payload: VehicleRecordRequest, admin=Depends(current_admin)):
    record_id = str(uuid4())
    created_at = now_iso()
    values = payload.model_dump()
    try:
        with connection() as db:
            db.execute(
                "INSERT INTO vehicle_records(id,plate_number,owner_name,make,model,color,region,notes,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    record_id,
                    values["plate_number"],
                    values["owner_name"],
                    values["make"],
                    values["model"],
                    values["color"],
                    values["region"],
                    values["notes"],
                    created_at,
                ),
            )
    except Exception as exc:
        if isinstance(exc, sqlite3.IntegrityError) or getattr(exc, "sqlstate", None) == "23505":
            raise HTTPException(status_code=409, detail={"code": "PLATE_EXISTS", "message": "A vehicle record with this registration number already exists."}) from exc
        raise
    return {"id": record_id, **values, "created_at": created_at}


@app.delete("/api/admin/vehicles/{record_id}", status_code=204)
def delete_vehicle_record(record_id: str, admin=Depends(current_admin)):
    with connection() as db:
        result = db.execute("DELETE FROM vehicle_records WHERE id=?", (record_id,))
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail={"code": "VEHICLE_NOT_FOUND", "message": "Vehicle record not found."})


@app.post("/api/admin/ocr/scan")
async def admin_ocr_scan(file: UploadFile = File(...), admin=Depends(current_admin)):
    data = await read_upload(file)
    try:
        return await run_in_threadpool(extract_text, data)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail={"code": "OCR_UNAVAILABLE", "message": str(exc)}) from exc
    except Exception as exc:
        logger.exception("Administrator OCR processing failed")
        raise HTTPException(status_code=503, detail={"code": "OCR_FAILED", "message": "Text extraction could not be completed."}) from exc


@app.get("/api/users/me")
def get_profile(user=Depends(current_user)):
    return user


@app.put("/api/users/me")
def update_profile(payload: ProfileRequest, user=Depends(current_user)):
    full_name = payload.full_name.strip()
    if not full_name:
        raise HTTPException(status_code=422, detail={"code": "INVALID_NAME", "message": "Name must contain 1 to 100 characters."})
    with connection() as db:
        db.execute("UPDATE users SET full_name = ? WHERE id = ?", (full_name, user["id"]))
    return {**user, "full_name": full_name}


@app.delete("/api/users/me", status_code=204)
def delete_account(user=Depends(current_user)):
    with connection() as db:
        db.execute("DELETE FROM users WHERE id=?", (user["id"],))


@app.get("/api/emergency-contacts")
def get_contacts(user=Depends(current_user)):
    with connection() as db:
        rows = db.execute(
            "SELECT id,name,phone,relationship,email,created_at FROM emergency_contacts WHERE user_id=? ORDER BY created_at",
            (user["id"],),
        ).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/emergency-contacts", status_code=201)
def create_contact(payload: ContactRequest, user=Depends(current_user)):
    contact_id = str(uuid4())
    with connection() as db:
        db.execute(
            "INSERT INTO emergency_contacts(id,user_id,name,phone,relationship,email,created_at) VALUES(?,?,?,?,?,?,?)",
            (contact_id, user["id"], payload.name.strip(), payload.phone.strip(), payload.relationship.strip(), payload.email.strip(), now_iso()),
        )
    return {"id": contact_id, **payload.model_dump()}


@app.put("/api/emergency-contacts/{contact_id}")
def update_contact(contact_id: str, payload: ContactRequest, user=Depends(current_user)):
    with connection() as db:
        result = db.execute(
            "UPDATE emergency_contacts SET name=?,phone=?,relationship=?,email=? WHERE id=? AND user_id=?",
            (payload.name.strip(), payload.phone.strip(), payload.relationship.strip(), payload.email.strip(), contact_id, user["id"]),
        )
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail={"code": "CONTACT_NOT_FOUND", "message": "Emergency contact not found."})
    return {"id": contact_id, **payload.model_dump()}


@app.delete("/api/emergency-contacts/{contact_id}", status_code=204)
def delete_contact(contact_id: str, user=Depends(current_user)):
    with connection() as db:
        result = db.execute("DELETE FROM emergency_contacts WHERE id=? AND user_id=?", (contact_id, user["id"]))
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail={"code": "CONTACT_NOT_FOUND", "message": "Emergency contact not found."})


@app.get("/api/trips")
def get_trips(user=Depends(current_user)):
    with connection() as db:
        rows = db.execute("SELECT * FROM trips WHERE user_id=? ORDER BY created_at DESC", (user["id"],)).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/trips", status_code=201)
def create_trip(payload: TripRequest, user=Depends(current_user)):
    trip_id = str(uuid4())
    created_at = now_iso()
    with connection() as db:
        db.execute(
            "INSERT INTO trips(id,user_id,name,destination,expected_end_time,status,created_at) VALUES(?,?,?,?,?,'PLANNED',?)",
            (trip_id, user["id"], payload.name.strip(), payload.destination.strip(), payload.expected_end_time.isoformat() if payload.expected_end_time else None, created_at),
        )
    trip = {"id": trip_id, **payload.model_dump(mode="json"), "status": "PLANNED", "created_at": created_at}
    broadcast_user(user["id"], {"type": "trip_update", "trip": trip})
    return trip


@app.patch("/api/trips/{trip_id}")
def update_trip(trip_id: str, payload: TripStatusRequest, user=Depends(current_user)):
    timestamp = now_iso()
    with connection() as db:
        trip = db.execute("SELECT id,status FROM trips WHERE id=? AND user_id=?", (trip_id, user["id"])).fetchone()
        if trip is None:
            raise HTTPException(status_code=404, detail={"code": "TRIP_NOT_FOUND", "message": "Trip not found."})
        allowed_transitions = {
            "PLANNED": {"ACTIVE", "CANCELLED"},
            "ACTIVE": {"PAUSED", "COMPLETED", "CANCELLED"},
            "PAUSED": {"ACTIVE", "COMPLETED", "CANCELLED"},
            "COMPLETED": set(),
            "CANCELLED": set(),
        }
        if payload.status not in allowed_transitions[trip["status"]]:
            raise HTTPException(status_code=409, detail={"code": "INVALID_TRIP_TRANSITION", "message": f"A trip cannot move from {trip['status']} to {payload.status}."})
        if payload.status == "ACTIVE":
            other_active = db.execute(
                "SELECT id FROM trips WHERE user_id=? AND status='ACTIVE' AND id<>? LIMIT 1",
                (user["id"], trip_id),
            ).fetchone()
            if other_active:
                raise HTTPException(status_code=409, detail={"code": "ACTIVE_TRIP_EXISTS", "message": "End or pause the active trip before starting another."})
        started_at = timestamp if payload.status == "ACTIVE" and trip["status"] in {"PLANNED", "PAUSED"} else None
        ended_at = timestamp if payload.status in {"COMPLETED", "CANCELLED"} else None
        if started_at:
            db.execute("UPDATE trips SET status=?,started_at=COALESCE(started_at,?) WHERE id=?", (payload.status, started_at, trip_id))
        elif ended_at:
            db.execute("UPDATE trips SET status=?,ended_at=? WHERE id=?", (payload.status, ended_at, trip_id))
        else:
            db.execute("UPDATE trips SET status=? WHERE id=?", (payload.status, trip_id))
    updated = {"id": trip_id, "status": payload.status}
    broadcast_user(user["id"], {"type": "trip_update", "trip": updated})
    return updated


@app.delete("/api/trips/{trip_id}", status_code=204)
def delete_trip(trip_id: str, user=Depends(current_user)):
    with connection() as db:
        result = db.execute("DELETE FROM trips WHERE id=? AND user_id=?", (trip_id, user["id"]))
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail={"code": "TRIP_NOT_FOUND", "message": "Trip not found."})


@app.get("/api/trips/{trip_id}")
def get_trip(trip_id: str, user=Depends(current_user)):
    with connection() as db:
        row = db.execute("SELECT * FROM trips WHERE id=? AND user_id=?", (trip_id, user["id"])).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "TRIP_NOT_FOUND", "message": "Trip not found."})
    return dict(row)


@app.post("/api/location", status_code=201)
def save_location(payload: LocationRequest, user=Depends(current_user)):
    location_id = str(uuid4())
    with connection() as db:
        owned_trip(db, payload.trip_id, user["id"])
        db.execute(
            """INSERT INTO locations(id,user_id,trip_id,latitude,longitude,accuracy,speed,heading,altitude,captured_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (location_id, user["id"], payload.trip_id, payload.latitude, payload.longitude, payload.accuracy, payload.speed, payload.heading, payload.altitude, payload.captured_at.isoformat()),
        )
    location = {"id": location_id, **payload.model_dump(mode="json")}
    broadcast_user(user["id"], {"type": "location_update", "location": location})
    return location


@app.get("/api/location/latest")
def latest_location(user=Depends(current_user)):
    with connection() as db:
        row = db.execute(
            "SELECT id,trip_id,latitude,longitude,accuracy,speed,heading,altitude,captured_at FROM locations WHERE user_id=? ORDER BY captured_at DESC LIMIT 1",
            (user["id"],),
        ).fetchone()
    return dict(row) if row else None


@app.get("/api/location/shares")
def get_location_shares(user=Depends(current_user)):
    with connection() as db:
        rows = db.execute(
            """SELECT id,trip_id,expires_at,created_at FROM location_shares
               WHERE user_id=? AND revoked_at IS NULL AND expires_at>?
               ORDER BY created_at DESC""",
            (user["id"], now_iso()),
        ).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/location/shares", status_code=201)
def create_location_share(request: Request, user=Depends(current_user)):
    active_trip_id = None
    with connection() as db:
        active_trip = db.execute(
            "SELECT id FROM trips WHERE user_id=? AND status='ACTIVE' ORDER BY created_at DESC LIMIT 1",
            (user["id"],),
        ).fetchone()
        if active_trip:
            active_trip_id = active_trip["id"]
        share = create_location_share_record(db, user["id"], active_trip_id, request)
    return {**share, "message": "Anyone with this private link can view your latest shared location until it expires or you revoke it."}


@app.delete("/api/location/shares/{share_id}")
def revoke_location_share(share_id: str, user=Depends(current_user)):
    with connection() as db:
        result = db.execute(
            "UPDATE location_shares SET revoked_at=? WHERE id=? AND user_id=? AND revoked_at IS NULL",
            (now_iso(), share_id, user["id"]),
        )
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail={"code": "SHARE_NOT_FOUND", "message": "Active location share not found."})
    return {"id": share_id, "status": "revoked"}


@app.post("/api/location/shared")
def read_shared_location(payload: SharedLocationRequest):
    with connection() as db:
        share = db.execute(
            """SELECT user_id,trip_id,expires_at FROM location_shares
               WHERE token_hash=? AND revoked_at IS NULL AND expires_at>?""",
            (sha256_token(payload.token), now_iso()),
        ).fetchone()
        if share is None:
            raise HTTPException(status_code=404, detail={"code": "SHARE_NOT_FOUND", "message": "This location link is invalid or expired."})
        query = "SELECT latitude,longitude,accuracy,captured_at FROM locations WHERE user_id=?"
        parameters = [share["user_id"]]
        if share["trip_id"]:
            query += " AND trip_id=?"
            parameters.append(share["trip_id"])
        query += " ORDER BY captured_at DESC LIMIT 1"
        location = db.execute(query, tuple(parameters)).fetchone()
    if location is None:
        return {"status": "waiting_for_location", "location": None, "expires_at": share["expires_at"]}
    return {"status": "available", "location": dict(location), "expires_at": share["expires_at"]}


@app.post("/api/emergency/sos", status_code=201)
def activate_sos(payload: EmergencyRequest, request: Request, user=Depends(current_user)):
    event_id = str(uuid4())
    share = None
    with connection() as db:
        owned_trip(db, payload.trip_id, user["id"])
        active = db.execute(
            "SELECT id FROM emergency_events WHERE user_id=? AND status='ACTIVE' LIMIT 1", (user["id"],)
        ).fetchone()
        if active:
            raise HTTPException(status_code=409, detail={"code": "SOS_ALREADY_ACTIVE", "message": "An emergency session is already active."})
        db.execute(
            """INSERT INTO emergency_events(id,user_id,trip_id,latitude,longitude,accuracy,event_type,status,notification_status,created_at)
               VALUES(?,?,?,?,?,?,?,'ACTIVE','pending',?)""",
            (event_id, user["id"], payload.trip_id, payload.latitude, payload.longitude, payload.accuracy, payload.event_type, now_iso()),
        )
        contacts = db.execute(
            "SELECT id,name,phone FROM emergency_contacts WHERE user_id=? ORDER BY created_at",
            (user["id"],),
        ).fetchall()
        if payload.latitude is not None and payload.longitude is not None:
            location_id = str(uuid4())
            captured_at = now_iso()
            db.execute(
                """INSERT INTO locations(id,user_id,trip_id,latitude,longitude,accuracy,captured_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (location_id, user["id"], payload.trip_id, payload.latitude, payload.longitude, payload.accuracy, captured_at),
            )
            if contacts and settings.app_env.lower() == "production" and twilio_is_configured():
                share = create_location_share_record(db, user["id"], payload.trip_id, request)

    results = []
    if settings.app_env.lower() == "production" and twilio_is_configured():
        if not contacts:
            notification_status = "no_contacts"
        else:
            share_line = f"\nLive location: {share['url']}" if share else ""
            coordinates = (
                f"\nLast known location: https://www.openstreetmap.org/?mlat={payload.latitude}&mlon={payload.longitude}"
                if payload.latitude is not None and payload.longitude is not None
                else "\nThe device did not provide a location."
            )
            body = (
                f"TravelerGuard emergency alert for {user['full_name']}. "
                f"Event {event_id}.{coordinates}{share_line}\n"
                "The app cannot contact local emergency services. Please contact the traveler and local services if needed."
            )
            for contact in contacts:
                status_base = settings.twilio_webhook_base_url or settings.public_base_url
                callback_url = f"{status_base}/api/twilio/sms/status" if status_base else None
                result = deliver_sms(contact["phone"], body, callback_url)
                results.append((contact, result))
            notification_status = (
                "accepted_by_provider"
                if results and all(result.status == "accepted_by_provider" for _, result in results)
                else "partially_failed"
                if any(result.status == "accepted_by_provider" for _, result in results)
                else "failed"
            )
    else:
        notification_status = "disabled" if settings.app_env.lower() != "production" else "not_configured"

    with connection() as db:
        db.execute("UPDATE emergency_events SET notification_status=? WHERE id=?", (notification_status, event_id))
        for contact, result in results:
            db.execute(
                """INSERT INTO notification_logs(id,user_id,emergency_event_id,contact_id,status,provider_message_id,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (str(uuid4()), user["id"], event_id, contact["id"], result.status, result.provider_message_id, now_iso()),
            )
    logger.warning("Emergency event activated event_id=%s user_id=%s", event_id, user["id"])
    event = {"id": event_id, "status": "ACTIVE", "notification_status": notification_status, "event_type": payload.event_type}
    broadcast_user(user["id"], {"type": "emergency_activated", "event": event})
    response = {
        **event,
        "message": emergency_message(notification_status, len(results), len(contacts)),
    }
    if share:
        response["live_share"] = share
    return response


def deliver_sms(to_number: str, body: str, status_callback: str | None = None):
    from .services.sms import SmsResult

    try:
        result = send_sms(to_number, body, status_callback)
        if result.status == "sent":
            return SmsResult(status="accepted_by_provider", provider_message_id=result.provider_message_id)
        return result
    except Exception:
        logger.exception("Emergency SMS provider failed")
        return SmsResult(status="failed", error="SMS provider request failed.")


def emergency_message(notification_status: str, sent_count: int, contact_count: int) -> str:
    if notification_status == "accepted_by_provider":
        return (
            f"Emergency mode is active. Twilio accepted {sent_count} message(s) for delivery; "
            "this does not confirm delivery. Contact local emergency services directly."
        )
    if notification_status == "partially_failed":
        return (
            f"Emergency mode is active. Twilio accepted messages for {sent_count} of {contact_count} contacts; "
            "delivery is not confirmed. Contact local emergency services directly."
        )
    if notification_status == "failed":
        return "Emergency mode is active, but SMS could not be accepted by the provider. Contact local emergency services directly."
    if notification_status == "no_contacts":
        return "Emergency mode is active, but no emergency contacts are configured. Contact local emergency services directly."
    if notification_status == "not_configured":
        return "Emergency mode is active. No SMS provider is configured, so contacts were not notified. Contact local emergency services directly."
    return "Emergency mode is active. SMS is disabled in development/test mode; contacts were not notified. Contact local emergency services directly."


@app.post("/api/emergency/cancel")
def cancel_sos(user=Depends(current_user)):
    timestamp = now_iso()
    with connection() as db:
        row = db.execute(
            "SELECT id FROM emergency_events WHERE user_id=? AND status='ACTIVE' ORDER BY created_at DESC LIMIT 1",
            (user["id"],),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=409, detail={"code": "NO_ACTIVE_SOS", "message": "There is no active emergency session to cancel."})
        db.execute("UPDATE emergency_events SET status='CANCELLED',resolved_at=? WHERE id=?", (timestamp, row["id"]))
    broadcast_user(user["id"], {"type": "emergency_cancelled", "event_id": row["id"]})
    return {"id": row["id"], "status": "CANCELLED"}


@app.get("/api/emergency/events")
def emergency_history(user=Depends(current_user)):
    with connection() as db:
        rows = db.execute(
            "SELECT * FROM emergency_events WHERE user_id=? ORDER BY created_at DESC LIMIT 100", (user["id"],)
        ).fetchall()
    return [dict(row) for row in rows]


@app.get("/api/emergency/sms/inbox")
def emergency_sms_inbox(user=Depends(current_user)):
    with connection() as db:
        rows = db.execute(
            """SELECT id,emergency_event_id,from_number,body,received_at FROM sms_messages
               WHERE user_id=? ORDER BY received_at DESC LIMIT 100""",
            (user["id"],),
        ).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/twilio/sms/inbound")
async def receive_twilio_sms(request: Request):
    if not settings.twilio_auth_token:
        raise HTTPException(status_code=503, detail={"code": "SMS_WEBHOOK_UNAVAILABLE", "message": "Twilio webhook validation is not configured."})
    form = await request.form()
    signature = request.headers.get("X-Twilio-Signature")
    if not twilio_signature_valid(request, form, signature):
        raise HTTPException(status_code=403, detail={"code": "INVALID_WEBHOOK_SIGNATURE", "message": "The Twilio webhook signature could not be verified."})
    from_number = str(form.get("From", ""))[:40]
    body = str(form.get("Body", ""))[:1600]
    message_sid = str(form.get("MessageSid", ""))[:64]
    if from_number and body:
        with connection() as db:
            match = db.execute(
                """SELECT contacts.user_id,events.id AS event_id
                   FROM emergency_contacts AS contacts
                   JOIN emergency_events AS events ON events.user_id=contacts.user_id
                   WHERE contacts.phone=? AND events.status='ACTIVE'
                   ORDER BY events.created_at DESC LIMIT 1""",
                (from_number,),
            ).fetchone()
            if match:
                db.execute(
                    """INSERT INTO sms_messages(id,user_id,emergency_event_id,from_number,body,provider_message_id,received_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (str(uuid4()), match["user_id"], match["event_id"], from_number, body, message_sid or None, now_iso()),
                )
                await manager.send_user(match["user_id"], {"type": "sms_received", "event_id": match["event_id"]})
    return Response(
        content='<?xml version="1.0" encoding="UTF-8"?><Response><Message>Message received. If someone may be in immediate danger, contact them and local emergency services directly.</Message></Response>',
        media_type="application/xml",
    )


@app.post("/api/twilio/sms/status")
async def receive_twilio_status(request: Request):
    if not settings.twilio_auth_token:
        raise HTTPException(status_code=503, detail={"code": "SMS_WEBHOOK_UNAVAILABLE", "message": "Twilio webhook validation is not configured."})
    form = await request.form()
    signature = request.headers.get("X-Twilio-Signature")
    if not twilio_signature_valid(request, form, signature):
        raise HTTPException(status_code=403, detail={"code": "INVALID_WEBHOOK_SIGNATURE", "message": "The Twilio webhook signature could not be verified."})
    message_sid = str(form.get("MessageSid", ""))[:64]
    provider_status = str(form.get("MessageStatus", ""))[:40]
    if not message_sid or provider_status not in {"accepted", "queued", "sending", "sent", "delivered", "undelivered", "failed", "canceled"}:
        raise HTTPException(status_code=400, detail={"code": "INVALID_SMS_STATUS", "message": "The Twilio message status payload is incomplete or invalid."})
    with connection() as db:
        row = db.execute(
            "SELECT user_id,emergency_event_id FROM notification_logs WHERE provider_message_id=? LIMIT 1",
            (message_sid,),
        ).fetchone()
        if row:
            db.execute("UPDATE notification_logs SET status=? WHERE provider_message_id=?", (provider_status, message_sid))
            statuses = db.execute(
                "SELECT status FROM notification_logs WHERE emergency_event_id=?",
                (row["emergency_event_id"],),
            ).fetchall()
            current_statuses = [item["status"] for item in statuses]
            delivered = sum(status == "delivered" for status in current_statuses)
            failed = sum(status in {"failed", "undelivered", "canceled"} for status in current_statuses)
            if current_statuses and delivered == len(current_statuses):
                aggregate_status = "delivered"
            elif failed and delivered:
                aggregate_status = "partially_failed"
            elif failed == len(current_statuses):
                aggregate_status = "failed"
            else:
                aggregate_status = "accepted_by_provider"
            db.execute(
                "UPDATE emergency_events SET notification_status=? WHERE id=?",
                (aggregate_status, row["emergency_event_id"]),
            )
            await manager.send_user(row["user_id"], {"type": "sms_status", "event_id": row["emergency_event_id"], "status": provider_status})
    return Response(status_code=204)


@app.get("/api/checkin/status")
def checkin_status(user=Depends(current_user)):
    with connection() as db:
        latest = db.execute(
            "SELECT created_at FROM safety_checkins WHERE user_id=? ORDER BY created_at DESC LIMIT 1",
            (user["id"],),
        ).fetchone()
    return {
        "last_checkin": latest["created_at"] if latest else None,
        "next_due_at": None,
        "missed_checkin_escalation_enabled": False,
    }


@app.get("/api/safety/status")
def safety_status(user=Depends(current_user)):
    with connection() as db:
        active_trip = db.execute(
            "SELECT id,name,destination,status FROM trips WHERE user_id=? AND status IN ('ACTIVE','PAUSED') ORDER BY created_at DESC LIMIT 1",
            (user["id"],),
        ).fetchone()
        active_event = db.execute(
            "SELECT id,status,created_at FROM emergency_events WHERE user_id=? AND status='ACTIVE' LIMIT 1",
            (user["id"],),
        ).fetchone()
    return {
        "active_trip": dict(active_trip) if active_trip else None,
        "active_emergency": dict(active_event) if active_event else None,
        "missed_checkin_escalation_enabled": False,
        "notifications": "disabled",
    }


@app.post("/api/checkin", status_code=201)
def checkin(payload: CheckInRequest, user=Depends(current_user)):
    checkin_id = str(uuid4())
    with connection() as db:
        owned_trip(db, payload.trip_id, user["id"])
        db.execute(
            "INSERT INTO safety_checkins(id,user_id,trip_id,created_at,note) VALUES(?,?,?,?,?)",
            (checkin_id, user["id"], payload.trip_id, now_iso(), payload.note.strip()),
        )
    return {"id": checkin_id, "created_at": now_iso(), "status": "recorded"}


async def read_upload(file: UploadFile) -> bytes:
    if file.content_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise HTTPException(status_code=415, detail={"code": "UNSUPPORTED_IMAGE", "message": "Upload a JPEG, PNG, or WEBP image."})
    data = await file.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail={"code": "IMAGE_TOO_LARGE", "message": "Image must be 5 MB or smaller."})
    try:
        validate_image(data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "INVALID_IMAGE", "message": str(exc)}) from exc
    return data


@app.post("/api/ai/detect")
async def ai_detect(file: UploadFile = File(...), user=Depends(current_user)):
    data = await read_upload(file)
    try:
        detections = await run_in_threadpool(detect_objects, data)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail={"code": "MODEL_UNAVAILABLE", "message": str(exc)}) from exc
    except Exception as exc:
        logger.exception("Object detection failed")
        raise HTTPException(status_code=503, detail={"code": "DETECTION_FAILED", "message": "Object detection could not be completed."}) from exc
    return {"detections": detections, "notice": "AI observations can be inaccurate and are not a safety assessment."}


@app.post("/api/ocr/scan")
async def ocr_scan(file: UploadFile = File(...), user=Depends(current_user)):
    data = await read_upload(file)
    try:
        return await run_in_threadpool(extract_text, data)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail={"code": "OCR_UNAVAILABLE", "message": str(exc)}) from exc
    except Exception as exc:
        logger.exception("OCR processing failed")
        raise HTTPException(status_code=503, detail={"code": "OCR_FAILED", "message": "Text extraction could not be completed."}) from exc


class ConnectionManager:
    def __init__(self):
        self.connections: dict[str, set[WebSocket]] = {}

    async def connect(self, user_id: str, websocket: WebSocket):
        await websocket.accept()
        self.connections.setdefault(user_id, set()).add(websocket)

    def disconnect(self, user_id: str, websocket: WebSocket):
        self.connections.get(user_id, set()).discard(websocket)

    async def send_user(self, user_id: str, message: dict):
        for websocket in tuple(self.connections.get(user_id, set())):
            try:
                await websocket.send_json(message)
            except Exception:
                self.disconnect(user_id, websocket)


manager = ConnectionManager()


def broadcast_user(user_id: str, event: dict):
    anyio.from_thread.run(manager.send_user, user_id, event)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    token = websocket.query_params.get("token", "")
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
        user_id = payload["sub"]
    except (jwt.InvalidTokenError, KeyError):
        await websocket.close(code=1008)
        return
    await manager.connect(user_id, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(user_id, websocket)


frontend = PROJECT_ROOT / "frontend"
if frontend.exists():
    app.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")
