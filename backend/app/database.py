import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .config import settings


class ConnectionAdapter:
    def __init__(self, connection):
        self._connection = connection

    def execute(self, statement, parameters=()):
        return self._connection.execute(statement.replace("?", "%s"), parameters)

    def commit(self):
        self._connection.commit()

    def rollback(self):
        self._connection.rollback()

    def close(self):
        self._connection.close()


def _is_sqlite() -> bool:
    return settings.database_url.startswith("sqlite:///")


def _database_path() -> str:
    prefix = "sqlite:///"
    if not _is_sqlite():
        raise RuntimeError(
            "DATABASE_URL is not a SQLite URL."
        )
    return settings.database_url[len(prefix) :]


@contextmanager
def connection():
    if _is_sqlite():
        database_path = _database_path()
        if database_path != ":memory:":
            Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(database_path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
    else:
        import psycopg
        from psycopg.rows import dict_row

        database_url = settings.database_url.replace("postgres://", "postgresql://", 1)
        db = ConnectionAdapter(psycopg.connect(database_url, row_factory=dict_row))
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def initialize_database() -> None:
    with connection() as db:
        schema = """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                full_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS vehicle_records (
                id TEXT PRIMARY KEY,
                plate_number TEXT NOT NULL UNIQUE,
                owner_name TEXT NOT NULL,
                make TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                color TEXT NOT NULL DEFAULT '',
                region TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_vehicle_records_plate ON vehicle_records(plate_number);
            CREATE TABLE IF NOT EXISTS emergency_contacts (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                relationship TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_contacts_user ON emergency_contacts(user_id);
            CREATE TABLE IF NOT EXISTS trips (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                destination TEXT NOT NULL,
                expected_end_time TEXT,
                status TEXT NOT NULL CHECK(status IN ('PLANNED','ACTIVE','PAUSED','COMPLETED','EMERGENCY','CANCELLED')),
                started_at TEXT,
                ended_at TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_trips_user_created ON trips(user_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS locations (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                trip_id TEXT REFERENCES trips(id) ON DELETE SET NULL,
                latitude REAL NOT NULL CHECK(latitude BETWEEN -90 AND 90),
                longitude REAL NOT NULL CHECK(longitude BETWEEN -180 AND 180),
                accuracy REAL,
                speed REAL,
                heading REAL,
                altitude REAL,
                captured_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_locations_user_time ON locations(user_id, captured_at DESC);
            CREATE TABLE IF NOT EXISTS emergency_events (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                trip_id TEXT REFERENCES trips(id) ON DELETE SET NULL,
                latitude REAL,
                longitude REAL,
                accuracy REAL,
                event_type TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('ACTIVE','CANCELLED','RESOLVED')),
                notification_status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                resolved_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_events_user_time ON emergency_events(user_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS safety_checkins (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                trip_id TEXT REFERENCES trips(id) ON DELETE SET NULL,
                created_at TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS location_shares (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                trip_id TEXT REFERENCES trips(id) ON DELETE SET NULL,
                token_hash TEXT NOT NULL UNIQUE,
                expires_at TEXT NOT NULL,
                revoked_at TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_location_shares_user ON location_shares(user_id, expires_at);
            CREATE TABLE IF NOT EXISTS sms_messages (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                emergency_event_id TEXT REFERENCES emergency_events(id) ON DELETE SET NULL,
                from_number TEXT NOT NULL,
                body TEXT NOT NULL,
                provider_message_id TEXT,
                received_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sms_messages_event ON sms_messages(emergency_event_id, received_at DESC);
            CREATE TABLE IF NOT EXISTS notification_logs (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                emergency_event_id TEXT NOT NULL REFERENCES emergency_events(id) ON DELETE CASCADE,
                contact_id TEXT REFERENCES emergency_contacts(id) ON DELETE SET NULL,
                status TEXT NOT NULL,
                provider_message_id TEXT,
                created_at TEXT NOT NULL
            );
            """
        for statement in schema.split(";"):
            if statement.strip():
                db.execute(statement)
