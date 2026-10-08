import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")


@dataclass
class Settings:
    app_env: str = os.getenv("APP_ENV", "development")
    secret_key: str = os.getenv("SECRET_KEY", "local-development-key-change-before-deploy")
    admin_username: str = os.getenv("ADMIN_USERNAME", "")
    admin_password: str = os.getenv("ADMIN_PASSWORD", "")
    database_url: str = os.getenv("DATABASE_URL") or f"sqlite:///{(PROJECT_ROOT / 'travelerguard.db').as_posix()}"
    token_expire_minutes: int = int(os.getenv("TOKEN_EXPIRE_MINUTES", "60"))
    ocr_language: str = os.getenv("OCR_LANGUAGE", "eng")
    tesseract_cmd: str = os.getenv("TESSERACT_CMD", "")
    cors_origins: tuple[str, ...] = tuple(
        origin.strip()
        for origin in os.getenv(
            "CORS_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000"
        ).split(",")
        if origin.strip()
    )
    yolo_model_path: str = os.getenv("YOLO_MODEL_PATH", "")
    notification_provider: str = os.getenv("NOTIFICATION_PROVIDER", "disabled").lower()
    twilio_account_sid: str = os.getenv("TWILIO_ACCOUNT_SID", "")
    twilio_auth_token: str = os.getenv("TWILIO_AUTH_TOKEN", "")
    twilio_from_number: str = os.getenv("TWILIO_FROM_NUMBER", "")
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    twilio_webhook_base_url: str = os.getenv("TWILIO_WEBHOOK_BASE_URL", "").rstrip("/")
    shared_location_ttl_minutes: int = 240
    max_upload_bytes: int = 5 * 1024 * 1024


settings = Settings()
