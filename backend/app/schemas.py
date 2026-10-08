from datetime import datetime
import re
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, TypeAdapter, field_validator, model_validator

email_adapter = TypeAdapter(EmailStr)


class RegisterRequest(BaseModel):
    full_name: str = Field(min_length=1, max_length=100)
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class AdminLoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=128)


def normalize_plate_number(value: str) -> str:
    normalized = re.sub(r"[^A-Z0-9]", "", value.upper())
    if not re.fullmatch(r"[A-Z0-9]{2,12}", normalized):
        raise ValueError("Enter a registration number containing 2 to 12 letters or digits.")
    return normalized


class VehicleRecordRequest(BaseModel):
    plate_number: str = Field(min_length=2, max_length=24)
    owner_name: str = Field(min_length=1, max_length=100)
    make: str = Field(default="", max_length=60)
    model: str = Field(default="", max_length=60)
    color: str = Field(default="", max_length=40)
    region: str = Field(default="", max_length=80)
    notes: str = Field(default="", max_length=500)

    @field_validator("plate_number")
    @classmethod
    def validate_plate_number(cls, value: str) -> str:
        return normalize_plate_number(value)

    @field_validator("owner_name", "make", "model", "color", "region", "notes")
    @classmethod
    def trim_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("owner_name")
    @classmethod
    def require_owner_name(cls, value: str) -> str:
        if not value:
            raise ValueError("Owner name is required.")
        return value


class ContactRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    phone: str = Field(min_length=3, max_length=30)
    relationship: str = Field(default="", max_length=60)
    email: str = Field(default="", max_length=254)

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str) -> str:
        normalized = re.sub(r"[\s().-]", "", value)
        if not re.fullmatch(r"\+[1-9]\d{7,14}", normalized):
            raise ValueError("Enter a phone number in international format, such as +14155550100.")
        return normalized

    @field_validator("email")
    @classmethod
    def validate_optional_email(cls, value: str) -> str:
        value = value.strip()
        if value:
            email_adapter.validate_python(value)
        return value


class ProfileRequest(BaseModel):
    full_name: str = Field(min_length=1, max_length=100)


class TripRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    destination: str = Field(min_length=1, max_length=200)
    expected_end_time: datetime | None = None


class TripStatusRequest(BaseModel):
    status: Literal["ACTIVE", "PAUSED", "COMPLETED", "CANCELLED"]


class LocationRequest(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy: float | None = Field(default=None, ge=0)
    speed: float | None = Field(default=None, ge=0)
    heading: float | None = Field(default=None, ge=0, le=360)
    altitude: float | None = None
    captured_at: datetime
    trip_id: str | None = None


class EmergencyRequest(BaseModel):
    event_type: Literal["SOS", "MANUAL_EMERGENCY", "TRIP_TIMEOUT", "DEVICE_ERROR"] = "SOS"
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    accuracy: float | None = Field(default=None, ge=0)
    trip_id: str | None = None

    @model_validator(mode="after")
    def validate_coordinate_pair(self):
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("Latitude and longitude must be provided together.")
        return self


class SharedLocationRequest(BaseModel):
    token: str = Field(min_length=32, max_length=128)


class CheckInRequest(BaseModel):
    trip_id: str | None = None
    note: str = Field(default="", max_length=500)
