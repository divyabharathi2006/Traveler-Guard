from io import BytesIO

import pytest
from PIL import Image
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from app.config import settings
from app import main as main_module
from app.main import app
from app.services.sms import SmsResult


@pytest.fixture()
def client(tmp_path):
    previous_url = settings.database_url
    settings.database_url = f"sqlite:///{(tmp_path / 'test.sqlite').as_posix()}"
    with TestClient(app) as test_client:
        yield test_client
    settings.database_url = previous_url


def register(client):
    response = client.post(
        "/api/auth/register",
        json={
            "full_name": "Avery Traveler",
            "email": "avery@example.com",
            "password": "correct-horse-battery-staple",
        },
    )
    assert response.status_code == 201
    return response.json()["access_token"]


def test_registration_login_and_protected_profile(client):
    token = register(client)
    headers = {"Authorization": f"Bearer {token}"}

    profile = client.get("/api/users/me", headers=headers)
    assert profile.status_code == 200
    assert profile.json()["email"] == "avery@example.com"
    assert client.get("/api/users/me").status_code == 401

    login = client.post(
        "/api/auth/login",
        json={"email": "AVERY@example.com", "password": "correct-horse-battery-staple"},
    )
    assert login.status_code == 200


def test_admin_login_is_disabled_without_configured_credentials(client, monkeypatch):
    monkeypatch.setattr(settings, "admin_username", "")
    monkeypatch.setattr(settings, "admin_password", "")
    response = client.post(
        "/api/admin/login",
        json={"username": "admin", "password": "not-configured"},
    )
    assert response.status_code == 503


def test_admin_vehicle_records_are_restricted_and_searchable(client, monkeypatch):
    monkeypatch.setattr(settings, "admin_username", "admin")
    monkeypatch.setattr(settings, "admin_password", "test-admin-password")
    user_headers = {"Authorization": f"Bearer {register(client)}"}
    login = client.post(
        "/api/admin/login",
        json={"username": "admin", "password": "test-admin-password"},
    )
    assert login.status_code == 200
    admin_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    assert client.get("/admin.html").status_code == 200
    assert client.get("/api/admin/vehicles", headers=user_headers).status_code == 403
    image_bytes = BytesIO()
    Image.new("RGB", (32, 32), "white").save(image_bytes, format="PNG")
    monkeypatch.setattr(
        main_module,
        "extract_text",
        lambda data: {
            "text": "KA 03 MN 1234",
            "plate_candidates": ["KA03MN1234"],
            "confidence": 95,
            "warning": "Verify extracted text.",
        },
    )
    image_file = {"file": ("plate.png", image_bytes.getvalue(), "image/png")}
    assert client.post(
        "/api/admin/ocr/scan",
        headers=user_headers,
        files=image_file,
    ).status_code == 403
    assert client.post(
        "/api/admin/ocr/scan",
        headers=admin_headers,
        files=image_file,
    ).json()["plate_candidates"] == ["KA03MN1234"]
    assert client.post(
        "/api/admin/vehicles",
        headers=user_headers,
        json={"plate_number": "KA 03 MN 1234", "owner_name": "Avery"},
    ).status_code == 403

    created = client.post(
        "/api/admin/vehicles",
        headers=admin_headers,
        json={
            "plate_number": "ka 03 mn 1234",
            "owner_name": "Avery Traveler",
            "make": "Example",
            "model": "Model X",
            "color": "Blue",
            "region": "KA",
        },
    )
    assert created.status_code == 201
    assert created.json()["plate_number"] == "KA03MN1234"
    assert client.get(
        "/api/admin/vehicles",
        headers=admin_headers,
        params={"plate_number": "KA-03-MN-1234"},
    ).json()[0]["owner_name"] == "Avery Traveler"
    assert client.post(
        "/api/admin/vehicles",
        headers=admin_headers,
        json={"plate_number": "KA03MN1234", "owner_name": "Duplicate"},
    ).status_code == 409
    assert client.delete(
        f"/api/admin/vehicles/{created.json()['id']}",
        headers=admin_headers,
    ).status_code == 204


def test_contact_crud_and_trip_lifecycle(client):
    headers = {"Authorization": f"Bearer {register(client)}"}
    contact = client.post(
        "/api/emergency-contacts",
        headers=headers,
        json={"name": "Jordan", "phone": "+15550100", "relationship": "Friend"},
    )
    assert contact.status_code == 201
    contact_id = contact.json()["id"]
    changed = client.put(
        f"/api/emergency-contacts/{contact_id}",
        headers=headers,
        json={"name": "Jordan Lee", "phone": "+15550101", "relationship": "Friend"},
    )
    assert changed.status_code == 200
    assert changed.json()["name"] == "Jordan Lee"

    trip = client.post(
        "/api/trips",
        headers=headers,
        json={"name": "City visit", "destination": "Central Station"},
    )
    assert trip.status_code == 201
    trip_id = trip.json()["id"]
    assert client.patch(f"/api/trips/{trip_id}", headers=headers, json={"status": "ACTIVE"}).status_code == 200
    assert client.patch(f"/api/trips/{trip_id}", headers=headers, json={"status": "ACTIVE"}).status_code == 409
    second_trip = client.post(
        "/api/trips",
        headers=headers,
        json={"name": "Another visit", "destination": "North Station"},
    ).json()
    assert client.patch(
        f"/api/trips/{second_trip['id']}",
        headers=headers,
        json={"status": "ACTIVE"},
    ).status_code == 409
    assert client.get(f"/api/trips/{trip_id}", headers=headers).json()["status"] == "ACTIVE"
    assert client.delete(f"/api/trips/{trip_id}", headers=headers).status_code == 204


def test_sos_is_recorded_without_claiming_notification_and_can_be_cancelled(client):
    headers = {"Authorization": f"Bearer {register(client)}"}
    event = client.post(
        "/api/emergency/sos",
        headers=headers,
        json={"event_type": "SOS", "latitude": 35.0, "longitude": 139.0},
    )
    assert event.status_code == 201
    assert event.json()["notification_status"] == "disabled"
    assert "contacts were not notified" in event.json()["message"]
    assert client.post("/api/emergency/sos", headers=headers, json={}).status_code == 409
    assert client.post("/api/emergency/cancel", headers=headers).json()["status"] == "CANCELLED"


def test_location_bounds_and_service_health(client):
    headers = {"Authorization": f"Bearer {register(client)}"}
    invalid = client.post(
        "/api/location",
        headers=headers,
        json={"latitude": 91, "longitude": 10, "captured_at": "2026-01-01T00:00:00Z"},
    )
    assert invalid.status_code == 422
    saved = client.post(
        "/api/location",
        headers=headers,
        json={"latitude": 35.0, "longitude": 139.0, "captured_at": "2026-01-01T00:00:00Z"},
    )
    assert saved.status_code == 201
    assert client.get("/api/location/latest", headers=headers).json()["latitude"] == 35.0
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["services"]["gps"] == "client-controlled"
    assert client.get("/api/checkin/status", headers=headers).json()["missed_checkin_escalation_enabled"] is False


def test_user_can_delete_account_and_cascaded_records(client):
    headers = {"Authorization": f"Bearer {register(client)}"}
    contact = client.post(
        "/api/emergency-contacts",
        headers=headers,
        json={"name": "Sam", "phone": "+15550100"},
    )
    assert contact.status_code == 201
    assert client.delete("/api/users/me", headers=headers).status_code == 204
    assert client.get("/api/users/me", headers=headers).status_code == 401


def test_private_location_share_expires_or_can_be_revoked(client):
    headers = {"Authorization": f"Bearer {register(client)}"}
    share = client.post("/api/location/shares", headers=headers)
    assert share.status_code == 201
    token = share.json()["url"].split("#token=", 1)[1]

    waiting = client.post("/api/location/shared", json={"token": token})
    assert waiting.status_code == 200
    assert waiting.json()["status"] == "waiting_for_location"

    location = client.post(
        "/api/location",
        headers=headers,
        json={"latitude": 35.0, "longitude": 139.0, "captured_at": "2026-01-01T00:00:00Z"},
    )
    assert location.status_code == 201
    shared = client.post("/api/location/shared", json={"token": token})
    assert shared.status_code == 200
    assert shared.json()["location"]["latitude"] == 35.0

    share_id = share.json()["id"]
    assert client.delete(f"/api/location/shares/{share_id}", headers=headers).status_code == 200
    assert client.post("/api/location/shared", json={"token": token}).status_code == 404


def test_production_sos_uses_mocked_twilio_and_shares_live_location(client, monkeypatch):
    headers = {"Authorization": f"Bearer {register(client)}"}
    client.post(
        "/api/emergency-contacts",
        headers=headers,
        json={"name": "Jordan", "phone": "+15550100"},
    )
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "notification_provider", "twilio")
    monkeypatch.setattr(settings, "twilio_account_sid", "AC-test")
    monkeypatch.setattr(settings, "twilio_auth_token", "test-token")
    monkeypatch.setattr(settings, "twilio_webhook_base_url", "https://travelerguard.example")
    monkeypatch.setattr(settings, "twilio_from_number", "+15550199")
    monkeypatch.setattr(settings, "public_base_url", "https://travelerguard.example")
    sent = []

    def fake_send(to_number, body, status_callback=None):
        sent.append((to_number, body, status_callback))
        return SmsResult(status="sent", provider_message_id="SM-test")

    monkeypatch.setattr(main_module, "send_sms", fake_send)
    sos = client.post(
        "/api/emergency/sos",
        headers=headers,
        json={"event_type": "SOS", "latitude": 35.0, "longitude": 139.0, "accuracy": 8},
    )
    assert sos.status_code == 201
    assert sos.json()["notification_status"] == "accepted_by_provider"
    assert len(sent) == 1
    assert sent[0][0] == "+15550100"
    assert "https://travelerguard.example/share.html#token=" in sent[0][1]
    assert "https://www.openstreetmap.org/" in sent[0][1]
    assert sent[0][2] == "https://travelerguard.example/api/twilio/sms/status"

    status_form = {"MessageSid": "SM-test", "MessageStatus": "delivered"}
    signature = RequestValidator("test-token").compute_signature(
        "https://travelerguard.example/api/twilio/sms/status",
        status_form,
    )
    delivered = client.post("/api/twilio/sms/status", data=status_form, headers={"X-Twilio-Signature": signature})
    assert delivered.status_code == 204
    history = client.get("/api/emergency/events", headers=headers)
    assert history.json()[0]["notification_status"] == "delivered"

    token = sos.json()["live_share"]["url"].split("#token=", 1)[1]
    shared = client.post("/api/location/shared", json={"token": token})
    assert shared.json()["location"]["longitude"] == 139.0


def test_inbound_twilio_sms_signature_and_delivery_status(client, monkeypatch):
    headers = {"Authorization": f"Bearer {register(client)}"}
    contact = client.post(
        "/api/emergency-contacts",
        headers=headers,
        json={"name": "Jordan", "phone": "+15550100"},
    )
    assert contact.status_code == 201
    event = client.post("/api/emergency/sos", headers=headers, json={}).json()
    monkeypatch.setattr(settings, "twilio_auth_token", "configured-token")
    incoming_form = {"From": "+15550100", "Body": "Are you okay?", "MessageSid": "SM-reply"}
    valid_signature = RequestValidator("configured-token").compute_signature(
        "http://testserver/api/twilio/sms/inbound",
        incoming_form,
    )

    rejected = client.post(
        "/api/twilio/sms/inbound",
        data=incoming_form,
        headers={"X-Twilio-Signature": "invalid"},
    )
    assert rejected.status_code == 403

    received = client.post(
        "/api/twilio/sms/inbound",
        data=incoming_form,
        headers={"X-Twilio-Signature": valid_signature},
    )
    assert received.status_code == 200
    inbox = client.get("/api/emergency/sms/inbox", headers=headers)
    assert inbox.json()[0]["body"] == "Are you okay?"
    assert inbox.json()[0]["emergency_event_id"] == event["id"]


def test_development_mode_never_sends_sms_even_with_twilio_credentials(client, monkeypatch):
    headers = {"Authorization": f"Bearer {register(client)}"}
    client.post(
        "/api/emergency-contacts",
        headers=headers,
        json={"name": "Jordan", "phone": "+15550100"},
    )
    monkeypatch.setattr(settings, "notification_provider", "twilio")
    monkeypatch.setattr(settings, "twilio_account_sid", "AC-test")
    monkeypatch.setattr(settings, "twilio_auth_token", "test-token")
    monkeypatch.setattr(settings, "twilio_from_number", "+15550199")
    sent = []
    monkeypatch.setattr(main_module, "send_sms", lambda *args, **kwargs: sent.append(args))

    response = client.post("/api/emergency/sos", headers=headers, json={})
    assert response.status_code == 201
    assert response.json()["notification_status"] == "disabled"
    assert sent == []
