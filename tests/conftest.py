import base64
import hashlib
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import main  # noqa: I001  (app.main loads the other modules)
from app import admin, pos, queue_screen, stores

# The owner's admin session and the devices of store 1 used by the tests.
ADMIN_TOKEN = "test-staff-secret"
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
KIOSK_KEY = "kiosk-key-store-1"
REGISTER_KEY = "register-key-store-1"
QUEUE_KEY = "queue-key-store-1"
REGISTER = {"X-Device-Key": REGISTER_KEY}


@pytest.fixture(autouse=True)
def no_queue_server(monkeypatch):
    """Tests call the queue screen app directly instead of opening its port."""
    monkeypatch.setattr(queue_screen, "QUEUE_PORT", 0)


def add_device(connection, store_id, kind, name, key):
    connection.execute(
        "INSERT INTO devices (store_id, kind, name, key_hash, created_at) VALUES (?, ?, ?, ?, ?)",
        (store_id, kind, name, stores.hash_key(key), stores.now_iso()),
    )


def add_admin_session(connection, user_id, token):
    connection.execute(
        "INSERT INTO admin_sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
        (
            hashlib.sha256(token.encode()).hexdigest(),
            user_id,
            (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        ),
    )


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "orders.sqlite3")
    monkeypatch.setattr(main, "IMAGE_UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(main, "PRINTER_HOST", "")
    monkeypatch.setenv("KIOSK_OWNER_LOGIN", "owner")
    monkeypatch.setenv("KIOSK_OWNER_PASSWORD", "owner-password")
    pos._sessions.clear()
    pos._failed_logins.clear()
    admin._failed_logins.clear()
    with TestClient(main.app, headers={"X-Device-Key": KIOSK_KEY}) as test_client:
        with closing(main.connect_db()) as connection:
            owner_id = connection.execute("SELECT id FROM admin_users WHERE login = 'owner'").fetchone()["id"]
            add_admin_session(connection, owner_id, ADMIN_TOKEN)
            add_device(connection, 1, "kiosk", "Тестовый киоск", KIOSK_KEY)
            add_device(connection, 1, "register", "Тестовая касса", REGISTER_KEY)
            add_device(connection, 1, "queue", "Тестовый экран", QUEUE_KEY)
            connection.commit()
        yield test_client


def take_print_jobs(client, key=REGISTER_KEY, result="printed"):
    """Act as the register program: take the store's print jobs and report the result."""
    headers = {"X-Device-Key": key}
    response = client.get("/api/agent/print-jobs", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    for job in body["jobs"]:
        reported = client.post(f"/api/agent/print-jobs/{job['id']}", headers=headers, json={"status": result})
        assert reported.status_code == 200, reported.text
    return body["printer"], [base64.b64decode(job["data"]) for job in body["jobs"]]
