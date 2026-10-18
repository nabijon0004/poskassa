"""Sales points (stores) and their devices.

One server serves every store. Each kiosk, cash register and queue screen is a device
of a store: it is activated once with a secret key from the admin panel and sends it in
the ``X-Device-Key`` header. The server keeps only a SHA-256 hash of the key.
"""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from fastapi import Header, HTTPException

import app.main as core

DeviceKind = Literal["kiosk", "register", "queue"]
DEVICE_KINDS: tuple[str, ...] = ("kiosk", "register", "queue")
DEVICE_KIND_LABELS = {"kiosk": "Киоск", "register": "Касса", "queue": "Экран очереди"}
DEFAULT_STORE_NAME = "Paprika"
DEFAULT_DEVICES = (("register", "Касса 1"), ("kiosk", "Киоск 1"), ("queue", "Экран очереди"))
# last_seen_at is written at most this often per device.
LAST_SEEN_INTERVAL = timedelta(seconds=20)

DeviceKey = Annotated[str | None, Header(alias="X-Device-Key")]


@dataclass(frozen=True)
class Device:
    id: int
    store_id: int
    kind: str
    name: str
    store_name: str


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def new_device_key() -> str:
    return secrets.token_urlsafe(24)


# --- storage ---------------------------------------------------------------------


def initialize_store_tables(connection: sqlite3.Connection) -> int:
    """Create store tables; returns the id of the default store for migrated data."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS stores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            address TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store_id INTEGER NOT NULL REFERENCES stores (id),
            kind TEXT NOT NULL,
            name TEXT NOT NULL,
            key_hash TEXT NOT NULL UNIQUE,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            last_seen_at TEXT
        )
        """
    )
    first = connection.execute("SELECT id FROM stores ORDER BY id LIMIT 1").fetchone()
    if first is not None:
        return first["id"]
    # A new or pre-multistore database: everything so far belongs to one store.
    cursor = connection.execute(
        "INSERT INTO stores (name, created_at) VALUES (?, ?)", (DEFAULT_STORE_NAME, now_iso())
    )
    store_id = cursor.lastrowid
    for kind, name in DEFAULT_DEVICES:
        insert_device(connection, store_id, kind, name)
    return store_id


def insert_device(connection: sqlite3.Connection, store_id: int, kind: str, name: str) -> tuple[int, str]:
    key = new_device_key()
    cursor = connection.execute(
        "INSERT INTO devices (store_id, kind, name, key_hash, created_at) VALUES (?, ?, ?, ?, ?)",
        (store_id, kind, name, hash_key(key), now_iso()),
    )
    return cursor.lastrowid, key


def default_register_id(connection: sqlite3.Connection, store_id: int) -> int | None:
    row = connection.execute(
        "SELECT id FROM devices WHERE store_id = ? AND kind = 'register' ORDER BY id LIMIT 1",
        (store_id,),
    ).fetchone()
    return row["id"] if row else None


def load_device(key: str | None) -> Device | None:
    if not key:
        return None
    with closing(core.connect_db()) as connection:
        row = connection.execute(
            """
            SELECT devices.*, stores.name AS store_name FROM devices
            JOIN stores ON stores.id = devices.store_id
            WHERE devices.key_hash = ? AND devices.active = 1 AND stores.active = 1
            """,
            (hash_key(key),),
        ).fetchone()
        if row is None:
            return None
        seen = row["last_seen_at"]
        now = datetime.now(timezone.utc)
        if seen is None or datetime.fromisoformat(seen) < now - LAST_SEEN_INTERVAL:
            connection.execute(
                "UPDATE devices SET last_seen_at = ? WHERE id = ?", (now.isoformat(), row["id"])
            )
            connection.commit()
    return Device(row["id"], row["store_id"], row["kind"], row["name"], row["store_name"])


def require_device(key: str | None, *kinds: str) -> Device:
    device = load_device(key)
    if device is None:
        raise HTTPException(
            status_code=401,
            detail="Устройство не активировано. Откройте ссылку активации из админпанели.",
        )
    if kinds and device.kind not in kinds:
        raise HTTPException(status_code=403, detail="Это устройство не предназначено для этой операции.")
    return device


def store_exists(connection: sqlite3.Connection, store_id: int) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM stores WHERE id = ?", (store_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Точка не найдена.")
    return row
