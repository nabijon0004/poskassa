"""Admin panel API (/admin): stores, devices, staff, products, printers and reports.

Admins log in with a login and password. The owner manages every store and the
managers; a manager sees only the stores the owner gave them access to.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import secrets
import sqlite3
import time as time_module
import uuid
from collections import deque
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

import app.main as core
from app import pos, printing, queue_screen, stores
from app.main import Product, ProductCreate, ProductUpdate

router = APIRouter()

SESSION_TTL = timedelta(hours=12)
LOGIN_WINDOW_SECONDS = 5 * 60
LOGIN_MAX_FAILURES = 10
LOGIN_PATTERN = r"^[A-Za-z0-9_.@-]{3,40}$"
_failed_logins: deque[float] = deque()

Authorization = Annotated[str | None, Header()]


# --- storage ---------------------------------------------------------------------


def initialize_admin_tables(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            login TEXT NOT NULL UNIQUE COLLATE NOCASE,
            name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_user_stores (
            user_id INTEGER NOT NULL,
            store_id INTEGER NOT NULL,
            PRIMARY KEY (user_id, store_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_sessions (
            token_hash TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            expires_at TEXT NOT NULL
        )
        """
    )


def now() -> datetime:
    return datetime.now(timezone.utc)


def create_user(
    connection: sqlite3.Connection, login: str, name: str, password: str, role: str
) -> int:
    if connection.execute("SELECT 1 FROM admin_users WHERE login = ?", (login,)).fetchone():
        raise HTTPException(status_code=409, detail="Такой логин уже занят.")
    cursor = connection.execute(
        "INSERT INTO admin_users (login, name, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
        (login, name, pos.hash_pin(password), role, now().isoformat()),
    )
    return cursor.lastrowid


def create_owner_from_environment() -> None:
    """The first owner comes from KIOSK_OWNER_LOGIN / KIOSK_OWNER_PASSWORD."""
    login = os.getenv("KIOSK_OWNER_LOGIN", "").strip()
    password = os.getenv("KIOSK_OWNER_PASSWORD", "")
    with closing(core.connect_db()) as connection:
        if connection.execute("SELECT 1 FROM admin_users WHERE role = 'owner' AND active = 1").fetchone():
            return
        if not login or len(password) < 8:
            core.logger.warning(
                "No admin panel owner yet: set KIOSK_OWNER_LOGIN and KIOSK_OWNER_PASSWORD (8+ characters)"
            )
            return
        create_user(connection, login, "Владелец", password, "owner")
        connection.commit()
    core.logger.info("Created the admin panel owner %s", login)


# --- auth ------------------------------------------------------------------------


@dataclass(frozen=True)
class AdminUser:
    id: int
    login: str
    name: str
    role: str
    # None: every store (the owner).
    store_ids: frozenset[int] | None

    @property
    def is_owner(self) -> bool:
        return self.role == "owner"

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "login": self.login, "name": self.name, "role": self.role}


def session_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def require_user(authorization: str | None) -> AdminUser:
    token = (authorization or "").removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Войдите в админпанель.")
    with closing(core.connect_db()) as connection:
        row = connection.execute(
            """
            SELECT admin_users.* FROM admin_sessions
            JOIN admin_users ON admin_users.id = admin_sessions.user_id
            WHERE admin_sessions.token_hash = ? AND admin_sessions.expires_at > ? AND admin_users.active = 1
            """,
            (session_hash(token), now().isoformat()),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=401, detail="Сессия истекла. Войдите снова.")
        store_ids = None
        if row["role"] != "owner":
            store_ids = frozenset(
                store["store_id"]
                for store in connection.execute(
                    "SELECT store_id FROM admin_user_stores WHERE user_id = ?", (row["id"],)
                )
            )
    return AdminUser(row["id"], row["login"], row["name"], row["role"], store_ids)


def require_owner(authorization: str | None) -> AdminUser:
    user = require_user(authorization)
    if not user.is_owner:
        raise HTTPException(status_code=403, detail="Это может только владелец.")
    return user


def require_store(authorization: str | None, store_id: int) -> AdminUser:
    user = require_user(authorization)
    with closing(core.connect_db()) as connection:
        stores.store_exists(connection, store_id)
    if user.store_ids is not None and store_id not in user.store_ids:
        raise HTTPException(status_code=403, detail="Нет доступа к этой точке.")
    return user


def visible_store_ids(connection: sqlite3.Connection, user: AdminUser) -> list[int]:
    ids = [row["id"] for row in connection.execute("SELECT id FROM stores ORDER BY id")]
    return ids if user.store_ids is None else [store_id for store_id in ids if store_id in user.store_ids]


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    login: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=1, max_length=200)


@router.post("/api/admin/login")
def login(payload: LoginInput) -> dict[str, Any]:
    current = time_module.time()
    while _failed_logins and _failed_logins[0] < current - LOGIN_WINDOW_SECONDS:
        _failed_logins.popleft()
    if len(_failed_logins) >= LOGIN_MAX_FAILURES:
        raise HTTPException(status_code=429, detail="Слишком много попыток. Подождите несколько минут.")
    with closing(core.connect_db()) as connection:
        row = connection.execute(
            "SELECT * FROM admin_users WHERE login = ? AND active = 1", (payload.login.strip(),)
        ).fetchone()
        if row is None or not pos.pin_matches(payload.password, row["password_hash"]):
            _failed_logins.append(current)
            raise HTTPException(status_code=401, detail="Неверный логин или пароль.")
        token = secrets.token_urlsafe(32)
        connection.execute("DELETE FROM admin_sessions WHERE expires_at < ?", (now().isoformat(),))
        connection.execute(
            "INSERT INTO admin_sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
            (session_hash(token), row["id"], (now() + SESSION_TTL).isoformat()),
        )
        connection.commit()
    return {"token": token, **me(f"Bearer {token}")}


@router.post("/api/admin/logout")
def logout(authorization: Authorization = None) -> dict[str, bool]:
    token = (authorization or "").removeprefix("Bearer ").strip()
    with closing(core.connect_db()) as connection:
        connection.execute("DELETE FROM admin_sessions WHERE token_hash = ?", (session_hash(token),))
        connection.commit()
    return {"ok": True}


def public_store(row: sqlite3.Row) -> dict[str, Any]:
    return {"id": row["id"], "name": row["name"], "address": row["address"], "active": bool(row["active"])}


@router.get("/api/admin/me")
def me(authorization: Authorization = None) -> dict[str, Any]:
    user = require_user(authorization)
    with closing(core.connect_db()) as connection:
        ids = visible_store_ids(connection, user)
        rows = connection.execute(
            f"SELECT * FROM stores WHERE id IN ({','.join('?' * len(ids))}) ORDER BY id", ids
        ).fetchall() if ids else []
    return {
        "user": user.public(),
        "stores": [public_store(row) for row in rows],
        # Where queue screens open their page (a separate port, or a public address behind a proxy).
        "queue_url": queue_screen.QUEUE_PUBLIC_URL,
        "queue_port": queue_screen.QUEUE_PORT,
    }


# --- admins (owner) ------------------------------------------------------------------


class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    login: str = Field(pattern=LOGIN_PATTERN)
    name: str = Field(min_length=1, max_length=60)
    password: str = Field(min_length=8, max_length=200)
    role: Literal["owner", "manager"] = "manager"
    store_ids: list[int] = Field(default_factory=list, max_length=500)


class UserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=60)
    password: str | None = Field(default=None, min_length=8, max_length=200)
    active: bool | None = None
    store_ids: list[int] | None = Field(default=None, max_length=500)


def public_user(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    store_ids = [
        store["store_id"]
        for store in connection.execute(
            "SELECT store_id FROM admin_user_stores WHERE user_id = ? ORDER BY store_id", (row["id"],)
        )
    ]
    return {
        "id": row["id"],
        "login": row["login"],
        "name": row["name"],
        "role": row["role"],
        "active": bool(row["active"]),
        "store_ids": store_ids,
    }


def set_user_stores(connection: sqlite3.Connection, user_id: int, store_ids: list[int]) -> None:
    for store_id in set(store_ids):
        stores.store_exists(connection, store_id)
    connection.execute("DELETE FROM admin_user_stores WHERE user_id = ?", (user_id,))
    connection.executemany(
        "INSERT INTO admin_user_stores (user_id, store_id) VALUES (?, ?)",
        [(user_id, store_id) for store_id in sorted(set(store_ids))],
    )


@router.get("/api/admin/users")
def list_users(authorization: Authorization = None) -> list[dict[str, Any]]:
    require_owner(authorization)
    with closing(core.connect_db()) as connection:
        rows = connection.execute("SELECT * FROM admin_users ORDER BY role DESC, name").fetchall()
        return [public_user(connection, row) for row in rows]


@router.post("/api/admin/users", status_code=201)
def add_user(payload: UserCreate, authorization: Authorization = None) -> dict[str, Any]:
    require_owner(authorization)
    with closing(core.connect_db()) as connection:
        user_id = create_user(connection, payload.login, " ".join(payload.name.split()), payload.password, payload.role)
        set_user_stores(connection, user_id, payload.store_ids if payload.role == "manager" else [])
        connection.commit()
        return public_user(connection, connection.execute("SELECT * FROM admin_users WHERE id = ?", (user_id,)).fetchone())


@router.put("/api/admin/users/{user_id}")
def update_user(user_id: int, payload: UserUpdate, authorization: Authorization = None) -> dict[str, Any]:
    owner = require_owner(authorization)
    with closing(core.connect_db()) as connection:
        row = connection.execute("SELECT * FROM admin_users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Пользователь не найден.")
        if payload.active is False and user_id == owner.id:
            raise HTTPException(status_code=409, detail="Нельзя отключить самого себя.")
        updates: dict[str, Any] = {}
        if payload.name is not None:
            updates["name"] = " ".join(payload.name.split())
        if payload.password is not None:
            updates["password_hash"] = pos.hash_pin(payload.password)
        if payload.active is not None:
            updates["active"] = int(payload.active)
        if updates:
            assignments = ", ".join(f"{name} = ?" for name in updates)
            connection.execute(f"UPDATE admin_users SET {assignments} WHERE id = ?", (*updates.values(), user_id))
        if payload.store_ids is not None and row["role"] == "manager":
            set_user_stores(connection, user_id, payload.store_ids)
        if payload.active is False or payload.password is not None:
            connection.execute("DELETE FROM admin_sessions WHERE user_id = ?", (user_id,))
        connection.commit()
        return public_user(connection, connection.execute("SELECT * FROM admin_users WHERE id = ?", (user_id,)).fetchone())


# --- stores ----------------------------------------------------------------------


class StoreInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    address: str = Field(default="", max_length=200)

    @field_validator("name", "address")
    @classmethod
    def single_line(cls, value: str) -> str:
        return " ".join(value.split())


class StoreUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=80)
    address: str | None = Field(default=None, max_length=200)
    active: bool | None = None


def today_summary(connection: sqlite3.Connection, store_id: int) -> dict[str, Any]:
    _, _, start_utc, end_utc = core.report_period_bounds(None, None)
    row = connection.execute(
        """
        SELECT COUNT(*) AS orders, COALESCE(SUM(total), 0) AS amount,
               COALESCE(SUM(CASE WHEN payment_status = 'unpaid' THEN 1 ELSE 0 END), 0) AS unpaid
        FROM orders WHERE store_id = ? AND created_at >= ? AND created_at < ?
          AND payment_status NOT IN ('cancelled', 'refunded')
        """,
        (store_id, start_utc, end_utc),
    ).fetchone()
    open_shifts = connection.execute(
        "SELECT COUNT(*) FROM shifts WHERE store_id = ? AND closed_at IS NULL", (store_id,)
    ).fetchone()[0]
    return {
        "orders": row["orders"],
        "amount": pos.money(row["amount"]),
        "unpaid": row["unpaid"],
        "open_shifts": open_shifts,
    }


@router.get("/api/admin/stores")
def list_stores(authorization: Authorization = None) -> list[dict[str, Any]]:
    user = require_user(authorization)
    with closing(core.connect_db()) as connection:
        result = []
        for store_id in visible_store_ids(connection, user):
            row = stores.store_exists(connection, store_id)
            result.append({**public_store(row), "today": today_summary(connection, store_id)})
    return result


@router.post("/api/admin/stores", status_code=201)
def create_store(payload: StoreInput, authorization: Authorization = None) -> dict[str, Any]:
    require_owner(authorization)
    if not payload.name:
        raise HTTPException(status_code=422, detail="Укажите название точки.")
    with closing(core.connect_db()) as connection:
        cursor = connection.execute(
            "INSERT INTO stores (name, address, created_at) VALUES (?, ?, ?)",
            (payload.name, payload.address, now().isoformat()),
        )
        connection.commit()
        return public_store(stores.store_exists(connection, cursor.lastrowid))


@router.put("/api/admin/stores/{store_id}")
def update_store(store_id: int, payload: StoreUpdate, authorization: Authorization = None) -> dict[str, Any]:
    require_owner(authorization)
    with closing(core.connect_db()) as connection:
        stores.store_exists(connection, store_id)
        updates: dict[str, Any] = {}
        if payload.name is not None:
            updates["name"] = " ".join(payload.name.split())
        if payload.address is not None:
            updates["address"] = " ".join(payload.address.split())
        if payload.active is not None:
            updates["active"] = int(payload.active)
        if updates:
            assignments = ", ".join(f"{name} = ?" for name in updates)
            connection.execute(f"UPDATE stores SET {assignments} WHERE id = ?", (*updates.values(), store_id))
            connection.commit()
        return public_store(stores.store_exists(connection, store_id))


# --- devices ---------------------------------------------------------------------


class DeviceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: stores.DeviceKind
    name: str = Field(min_length=1, max_length=60)


class DeviceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=60)
    active: bool | None = None


def public_device(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "store_id": row["store_id"],
        "kind": row["kind"],
        "name": row["name"],
        "active": bool(row["active"]),
        "last_seen_at": row["last_seen_at"],
    }


def load_store_device(connection: sqlite3.Connection, store_id: int, device_id: int) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM devices WHERE id = ? AND store_id = ?", (device_id, store_id)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Устройство не найдено.")
    return row


@router.get("/api/admin/stores/{store_id}/devices")
def list_devices(store_id: int, authorization: Authorization = None) -> list[dict[str, Any]]:
    require_store(authorization, store_id)
    with closing(core.connect_db()) as connection:
        rows = connection.execute(
            "SELECT * FROM devices WHERE store_id = ? ORDER BY active DESC, kind, name", (store_id,)
        ).fetchall()
    return [public_device(row) for row in rows]


@router.post("/api/admin/stores/{store_id}/devices", status_code=201)
def create_device(store_id: int, payload: DeviceCreate, authorization: Authorization = None) -> dict[str, Any]:
    require_store(authorization, store_id)
    with closing(core.connect_db()) as connection:
        device_id, key = stores.insert_device(connection, store_id, payload.kind, " ".join(payload.name.split()))
        connection.commit()
        row = load_store_device(connection, store_id, device_id)
    return {**public_device(row), "key": key}


@router.put("/api/admin/stores/{store_id}/devices/{device_id}")
def update_device(
    store_id: int, device_id: int, payload: DeviceUpdate, authorization: Authorization = None
) -> dict[str, Any]:
    require_store(authorization, store_id)
    with closing(core.connect_db()) as connection:
        load_store_device(connection, store_id, device_id)
        if payload.name is not None:
            connection.execute("UPDATE devices SET name = ? WHERE id = ?", (" ".join(payload.name.split()), device_id))
        if payload.active is not None:
            connection.execute("UPDATE devices SET active = ? WHERE id = ?", (int(payload.active), device_id))
        connection.commit()
        return public_device(load_store_device(connection, store_id, device_id))


@router.post("/api/admin/stores/{store_id}/devices/{device_id}/key")
def renew_device_key(store_id: int, device_id: int, authorization: Authorization = None) -> dict[str, Any]:
    """A new key; the old one stops working, so the device has to be activated again."""
    require_store(authorization, store_id)
    key = stores.new_device_key()
    with closing(core.connect_db()) as connection:
        load_store_device(connection, store_id, device_id)
        connection.execute("UPDATE devices SET key_hash = ? WHERE id = ?", (stores.hash_key(key), device_id))
        connection.commit()
        row = load_store_device(connection, store_id, device_id)
    return {**public_device(row), "key": key}


# --- cashiers --------------------------------------------------------------------


def load_store_cashier(connection: sqlite3.Connection, store_id: int, cashier_id: int) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM cashiers WHERE id = ? AND store_id = ?", (cashier_id, store_id)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Кассир не найден.")
    return row


@router.get("/api/admin/stores/{store_id}/cashiers")
def list_cashiers(store_id: int, authorization: Authorization = None) -> list[dict[str, Any]]:
    require_store(authorization, store_id)
    with closing(core.connect_db()) as connection:
        rows = connection.execute(
            "SELECT * FROM cashiers WHERE store_id = ? ORDER BY active DESC, name", (store_id,)
        ).fetchall()
    return [pos.public_cashier(row) for row in rows]


@router.post("/api/admin/stores/{store_id}/cashiers", status_code=201)
def create_cashier(store_id: int, payload: pos.CashierCreate, authorization: Authorization = None) -> dict[str, Any]:
    require_store(authorization, store_id)
    with closing(core.connect_db()) as connection:
        if pos.find_cashier_by_pin(connection, store_id, payload.pin):
            raise HTTPException(status_code=409, detail="Такой PIN-код уже используется в этой точке.")
        cursor = connection.execute(
            """
            INSERT INTO cashiers (name, pin_hash, active, created_at, can_view_revenue, store_id)
            VALUES (?, ?, 1, ?, ?, ?)
            """,
            (
                payload.name, pos.hash_pin(payload.pin), now().isoformat(),
                int(payload.can_view_revenue), store_id,
            ),
        )
        connection.commit()
        return pos.public_cashier(load_store_cashier(connection, store_id, cursor.lastrowid))


@router.put("/api/admin/stores/{store_id}/cashiers/{cashier_id}")
def update_cashier(
    store_id: int, cashier_id: int, payload: pos.CashierUpdate, authorization: Authorization = None
) -> dict[str, Any]:
    require_store(authorization, store_id)
    with closing(core.connect_db()) as connection:
        load_store_cashier(connection, store_id, cashier_id)
        updates: dict[str, Any] = {}
        if payload.name is not None:
            updates["name"] = " ".join(payload.name.split())
        if payload.pin is not None:
            if pos.find_cashier_by_pin(connection, store_id, payload.pin, exclude_id=cashier_id):
                raise HTTPException(status_code=409, detail="Такой PIN-код уже используется в этой точке.")
            updates["pin_hash"] = pos.hash_pin(payload.pin)
        if payload.active is not None:
            updates["active"] = int(payload.active)
        if payload.can_view_revenue is not None:
            updates["can_view_revenue"] = int(payload.can_view_revenue)
        if updates:
            assignments = ", ".join(f"{name} = ?" for name in updates)
            connection.execute(f"UPDATE cashiers SET {assignments} WHERE id = ?", (*updates.values(), cashier_id))
            connection.commit()
        row = load_store_cashier(connection, store_id, cashier_id)
    if payload.active is False or payload.pin is not None:
        pos.end_cashier_sessions(cashier_id)
    return pos.public_cashier(row)


# --- products --------------------------------------------------------------------


def validate_product_category(catalog: core.Catalog, category_id: str) -> core.Category:
    category = catalog.category_by_id.get(category_id)
    if category is None:
        raise HTTPException(status_code=422, detail="Выберите существующую категорию.")
    return category


def persist_product(store_id: int, product: Product, *, is_custom: bool) -> None:
    try:
        core.save_product_setting(store_id, product, is_custom=is_custom)
    except sqlite3.Error as exc:
        core.logger.exception("Could not save product %s", product.id)
        raise HTTPException(status_code=500, detail="Не удалось сохранить товар.") from exc


def is_custom_product(store_id: int, product_id: str) -> bool:
    with closing(core.connect_db()) as connection:
        row = connection.execute(
            "SELECT is_custom FROM product_settings WHERE store_id = ? AND product_id = ?",
            (store_id, product_id),
        ).fetchone()
    return bool(row["is_custom"]) if row else False


def store_product(store_id: int, product_id: str) -> Product:
    product = core.catalog_for(store_id).product_by_id.get(product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Товар не найден.")
    return product


@router.get("/api/admin/stores/{store_id}/products")
def admin_products(store_id: int, authorization: Authorization = None) -> dict[str, Any]:
    require_store(authorization, store_id)
    catalog = core.catalog_for(store_id)
    return {
        "categories": [category.model_dump() for category in catalog.categories],
        "products": [product.model_dump() for product in catalog.products],
    }


@router.post("/api/admin/stores/{store_id}/products", status_code=201)
def create_product(store_id: int, payload: ProductCreate, authorization: Authorization = None) -> Product:
    require_store(authorization, store_id)
    category = validate_product_category(core.catalog_for(store_id), payload.category_id)
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Укажите название товара.")
    product = Product(
        id=str(uuid.uuid4()),
        name=name,
        category_id=payload.category_id,
        description=category.name,
        price=float(payload.price),
        available=payload.available,
        image=core._image_for_product(name, category.name),
        unit=payload.unit,
        ingredients=(
            payload.ingredients.strip() if payload.ingredients is not None else core.default_ingredients(name)
        ),
        priority=payload.priority,
    )
    persist_product(store_id, product, is_custom=True)
    return product


@router.put("/api/admin/stores/{store_id}/products/{product_id}")
def update_product(
    store_id: int, product_id: str, payload: ProductUpdate, authorization: Authorization = None
) -> Product:
    require_store(authorization, store_id)
    existing = store_product(store_id, product_id)
    category = validate_product_category(core.catalog_for(store_id), payload.category_id)
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Укажите название товара.")
    product = existing.model_copy(
        update={
            "name": name,
            "category_id": payload.category_id,
            "description": category.name,
            "price": float(payload.price),
            "available": payload.available,
            **({"ingredients": payload.ingredients.strip()} if payload.ingredients is not None else {}),
            **({"priority": payload.priority} if "priority" in payload.model_fields_set else {}),
        }
    )
    persist_product(store_id, product, is_custom=is_custom_product(store_id, product_id))
    return store_product(store_id, product_id)


@router.post("/api/admin/stores/{store_id}/products/{product_id}/image")
async def upload_product_image(
    store_id: int,
    product_id: str,
    authorization: Authorization = None,
    image: UploadFile = File(...),
) -> Product:
    require_store(authorization, store_id)
    existing = store_product(store_id, product_id)
    content = await image.read(core.MAX_PRODUCT_IMAGE_SIZE + 1)
    if len(content) > core.MAX_PRODUCT_IMAGE_SIZE:
        raise HTTPException(status_code=413, detail="Размер изображения не должен превышать 5 МБ.")
    image_types = (
        (content.startswith(b"\xff\xd8\xff"), "jpg"),
        (content.startswith(b"\x89PNG\r\n\x1a\n"), "png"),
        (len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP", "webp"),
    )
    extension = next((extension for is_valid, extension in image_types if is_valid), None)
    if extension is None:
        raise HTTPException(status_code=415, detail="Поддерживаются изображения JPEG, PNG и WebP.")
    core.IMAGE_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    image_path = core.IMAGE_UPLOAD_DIR / f"{uuid.uuid4().hex}.{extension}"
    try:
        image_path.write_bytes(content)
    except OSError as exc:
        core.logger.exception("Could not save uploaded product image")
        raise HTTPException(status_code=500, detail="Не удалось сохранить изображение.") from exc

    product = existing.model_copy(update={"image": f"/static/images/uploads/{image_path.name}"})
    try:
        persist_product(store_id, product, is_custom=is_custom_product(store_id, product_id))
    except HTTPException:
        image_path.unlink(missing_ok=True)
        raise
    # An uploaded photo may be shared with another store's copy of the product; keep it.
    return store_product(store_id, product_id)


# --- printer ---------------------------------------------------------------------


class PrintInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The unique internal reference (P-00051), not the repeating short number.
    order_ref: str = Field(pattern=r"^[A-Z0-9-]{1,40}$")


@router.get("/api/admin/stores/{store_id}/printer")
def get_printer_settings(store_id: int, authorization: Authorization = None) -> core.PrinterSettings:
    require_store(authorization, store_id)
    return core.load_printer_settings(store_id)


@router.put("/api/admin/stores/{store_id}/printer")
def update_printer_settings(
    store_id: int, payload: core.PrinterSettings, authorization: Authorization = None
) -> core.PrinterSettings:
    require_store(authorization, store_id)
    try:
        core.save_printer_settings(store_id, payload)
    except sqlite3.Error as exc:
        core.logger.exception("Could not save printer settings")
        raise HTTPException(status_code=500, detail="Не удалось сохранить настройки.") from exc
    return payload


@router.post("/api/admin/stores/{store_id}/printer/preview")
def preview_receipt(
    store_id: int, payload: core.PrinterSettings, authorization: Authorization = None
) -> dict[str, Any]:
    require_store(authorization, store_id)
    return {"chars_per_line": payload.chars_per_line, "lines": core.build_receipt(core.sample_order(), payload)}


@router.get("/api/admin/stores/{store_id}/printer/status")
def printer_status(store_id: int, authorization: Authorization = None) -> dict[str, Any]:
    """Whether the register program prints for this store, and the latest print jobs."""
    require_store(authorization, store_id)
    with closing(core.connect_db()) as connection:
        agents = printing.agents_status(connection, store_id)
        jobs = printing.recent_jobs(connection, store_id)
    settings = core.load_printer_settings(store_id)
    return {"configured": bool(settings.host), "agents": agents, "jobs": jobs}


@router.post("/api/admin/stores/{store_id}/printer/test")
def print_test_receipt(
    store_id: int, payload: core.PrinterSettings, authorization: Authorization = None
) -> dict[str, str]:
    require_store(authorization, store_id)
    try:
        core.OrderPrinter.print_order(core.sample_order(), store_id, payload)
    except core.PrinterError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"message": "Пробный чек поставлен в очередь печати точки."}


@router.post("/api/admin/stores/{store_id}/print")
def reprint_order(store_id: int, payload: PrintInput, authorization: Authorization = None) -> dict[str, Any]:
    require_store(authorization, store_id)
    with closing(core.connect_db()) as connection:
        row = connection.execute(
            "SELECT * FROM orders WHERE order_number = ? AND store_id = ?", (payload.order_ref, store_id)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Заказ не найден.")
    try:
        core.OrderPrinter.print_order(core.public_order(row), store_id)
    except core.PrinterError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    core.update_order_print_status(row["order_number"], "queued")
    return {
        "order_number": core.display_order_number(row["store_seq"]),
        "order_ref": row["order_number"],
        "print_status": "queued",
        "comment_printed": bool(row["comment"]),
    }


@router.get("/api/admin/stores/{store_id}/orders")
def admin_orders(store_id: int, authorization: Authorization = None) -> list[dict[str, Any]]:
    require_store(authorization, store_id)
    with closing(core.connect_db()) as connection:
        rows = connection.execute(
            "SELECT * FROM orders WHERE store_id = ? ORDER BY id DESC LIMIT 50", (store_id,)
        ).fetchall()
        return [pos.pos_order(connection, row) for row in rows]


# --- reports ---------------------------------------------------------------------


@dataclass(frozen=True)
class ReportFilter:
    store_ids: list[int]
    start_date: date | None
    end_date: date | None
    register_id: int | None
    shift_id: int | None
    cashier_id: int | None


def report_filter(
    user: AdminUser,
    store_id: int | None,
    start_date: date | None,
    end_date: date | None,
    register_id: int | None,
    shift_id: int | None,
    cashier_id: int | None,
) -> ReportFilter:
    with closing(core.connect_db()) as connection:
        visible = visible_store_ids(connection, user)
        if store_id is not None:
            stores.store_exists(connection, store_id)
            if store_id not in visible:
                raise HTTPException(status_code=403, detail="Нет доступа к этой точке.")
            selected = [store_id]
        else:
            selected = visible
    return ReportFilter(selected, start_date, end_date, register_id, shift_id, cashier_id)


def order_conditions(filters: ReportFilter, *, shift_field: str = "shift_id") -> tuple[str, list[Any]]:
    """SQL conditions for the store, register, shift and cashier filters."""
    conditions = [f"store_id IN ({','.join('?' * len(filters.store_ids))})" if filters.store_ids else "0"]
    values: list[Any] = list(filters.store_ids)
    if filters.register_id is not None:
        conditions.append("register_id = ?")
        values.append(filters.register_id)
    if filters.shift_id is not None:
        conditions.append(f"{shift_field} = ?")
        values.append(filters.shift_id)
    if filters.cashier_id is not None:
        conditions.append("cashier_id = ?")
        values.append(filters.cashier_id)
    return " AND ".join(conditions), values


def build_report(filters: ReportFilter) -> tuple[dict[str, Any], list[sqlite3.Row]]:
    """Sales of the period. With a shift filter the whole shift counts, whatever its dates."""
    start, end, start_utc, end_utc = core.report_period_bounds(filters.start_date, filters.end_date)
    where, values = order_conditions(filters)
    period = "" if filters.shift_id is not None else " AND created_at >= ? AND created_at < ?"
    period_values = [] if filters.shift_id is not None else [start_utc, end_utc]
    with closing(core.connect_db()) as connection:
        rows = connection.execute(
            f"""
            SELECT * FROM orders WHERE {where}{period}
              AND payment_status NOT IN ('cancelled', 'refunded')
            ORDER BY created_at, id
            """,
            [*values, *period_values],
        ).fetchall()
        refund_where, refund_values = order_conditions(filters, shift_field="refund_shift_id")
        refund_period = "" if filters.shift_id is not None else " AND refunded_at >= ? AND refunded_at < ?"
        refunds = connection.execute(
            f"""
            SELECT COUNT(*) AS count, COALESCE(SUM(total), 0) AS amount FROM orders
            WHERE {refund_where}{refund_period} AND payment_status = 'refunded'
            """,
            [*refund_values, *period_values],
        ).fetchone()
        names = {
            "store": {row["id"]: row["name"] for row in connection.execute("SELECT id, name FROM stores")},
            "register": {row["id"]: row["name"] for row in connection.execute("SELECT id, name FROM devices")},
            "cashier": {row["id"]: row["name"] for row in connection.execute("SELECT id, name FROM cashiers")},
        }

    product_summary: dict[str, dict[str, Any]] = {}
    groups: dict[str, dict[Any, dict[str, Any]]] = {"store": {}, "register": {}, "cashier": {}}
    item_count = 0
    total_amount = Decimal("0")
    totals = {"paid": Decimal("0"), "unpaid": Decimal("0"), "cash": Decimal("0"), "card": Decimal("0")}
    discounts = Decimal("0")
    for row in rows:
        amount = Decimal(str(row["total"]))
        total_amount += amount
        discounts += Decimal(str(row["discount"] or 0))
        if row["payment_status"] == "paid":
            totals["paid"] += amount
            if row["payment_method"] in ("cash", "card"):
                totals[row["payment_method"]] += amount
        else:
            totals["unpaid"] += amount
        for group, key in (("store", row["store_id"]), ("register", row["register_id"]), ("cashier", row["cashier_id"])):
            if key is None:
                continue
            summary = groups[group].setdefault(
                key, {"id": key, "name": names[group].get(key, "—"), "orders": 0, "amount": Decimal("0")}
            )
            summary["orders"] += 1
            summary["amount"] += amount
        for item in json.loads(row["items_json"]):
            key = item["product_id"]
            summary = product_summary.setdefault(
                key,
                {
                    "product_id": key,
                    "name": item["name"],
                    "unit": item.get("unit", "шт."),
                    "quantity": 0,
                    "amount": Decimal("0"),
                },
            )
            summary["quantity"] += item["quantity"]
            summary["amount"] += Decimal(str(item["unit_price"])) * item["quantity"]
            if item.get("weight_grams") is not None:
                summary["weight_grams"] = summary.get("weight_grams", 0) + item["weight_grams"] * item["quantity"]
            item_count += item["quantity"]

    def money(value: Decimal) -> float:
        return float(round(value, 2))

    def grouped(group: str) -> list[dict[str, Any]]:
        return sorted(
            ({**entry, "amount": money(entry["amount"])} for entry in groups[group].values()),
            key=lambda entry: entry["amount"],
            reverse=True,
        )

    report = {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "orders_count": len(rows),
        "items_count": item_count,
        "total_amount": money(total_amount),
        "average_order_amount": money(total_amount / len(rows)) if rows else 0,
        "paid_amount": money(totals["paid"]),
        "unpaid_amount": money(totals["unpaid"]),
        "cash_amount": money(totals["cash"]),
        "card_amount": money(totals["card"]),
        "discounts_amount": money(discounts),
        "refunds_count": refunds["count"],
        "refunds_amount": pos.money(refunds["amount"]),
        "by_store": grouped("store"),
        "by_register": grouped("register"),
        "by_cashier": grouped("cashier"),
        "products": sorted(
            ({**item, "amount": float(item["amount"])} for item in product_summary.values()),
            key=lambda item: item["amount"],
            reverse=True,
        ),
    }
    return report, rows



@router.get("/api/admin/report")
def admin_report(
    authorization: Authorization = None,
    store_id: int | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    register_id: int | None = None,
    shift_id: int | None = None,
    cashier_id: int | None = None,
) -> dict[str, Any]:
    user = require_user(authorization)
    report, _ = build_report(report_filter(user, store_id, start_date, end_date, register_id, shift_id, cashier_id))
    return report


@router.get("/api/admin/report.csv")
def download_admin_report(
    authorization: Authorization = None,
    store_id: int | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    register_id: int | None = None,
    shift_id: int | None = None,
    cashier_id: int | None = None,
) -> Response:
    user = require_user(authorization)
    report, orders = build_report(
        report_filter(user, store_id, start_date, end_date, register_id, shift_id, cashier_id)
    )
    with closing(core.connect_db()) as connection:
        names = {
            "store": {row["id"]: row["name"] for row in connection.execute("SELECT id, name FROM stores")},
            "device": {row["id"]: row["name"] for row in connection.execute("SELECT id, name FROM devices")},
            "cashier": {row["id"]: row["name"] for row in connection.execute("SELECT id, name FROM cashiers")},
        }
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["Период", report["start_date"], report["end_date"]])
    writer.writerow(["Количество заказов", report["orders_count"]])
    writer.writerow(["Количество позиций", report["items_count"]])
    writer.writerow(["Общая сумма", report["total_amount"]])
    writer.writerow(["Наличные", report["cash_amount"]])
    writer.writerow(["Карта", report["card_amount"]])
    writer.writerow(["Возвраты", report["refunds_amount"]])
    writer.writerow([])
    writer.writerow(
        [
            "Точка",
            "Касса",
            "Смена",
            "Кассир",
            "Номер заказа",
            "Внутренний номер",
            "Дата и время (UTC)",
            "В зале / с собой",
            "Товар",
            "Вариант",
            "Добавки",
            "Количество",
            "Единица",
            "Цена за единицу",
            "Сумма порции",
            "Вес порции (кг)",
            "Сумма позиции",
            "Комментарий к заказу",
            "Оплата",
        ]
    )
    for order in orders:
        for item in json.loads(order["items_json"]):
            writer.writerow(
                [
                    names["store"].get(order["store_id"], ""),
                    names["device"].get(order["register_id"], ""),
                    order["shift_id"] or "",
                    names["cashier"].get(order["cashier_id"], ""),
                    core.display_order_number(order["store_seq"]),
                    order["order_number"],
                    order["created_at"],
                    core.SERVICE_TYPE_LABELS.get(order["service_type"], ""),
                    item["name"],
                    item["variant"]["name"] if item["variant"] else "",
                    ", ".join(modifier["name"] for modifier in item["modifiers"]),
                    item["quantity"],
                    item.get("unit", "шт."),
                    item["unit_price"],
                    item.get("portion_amount") or "",
                    item["weight_grams"] / 1000 if item.get("weight_grams") else "",
                    item["unit_price"] * item["quantity"],
                    order["comment"],
                    core.PAYMENT_STATUS_LABELS.get(order["payment_status"], order["payment_status"])
                    + (
                        f" ({core.PAYMENT_METHOD_LABELS[order['payment_method']].lower()})"
                        if order["payment_method"] in core.PAYMENT_METHOD_LABELS
                        else ""
                    ),
                ]
            )

    filename = f"paprika-orders-{report['start_date']}-{report['end_date']}.csv"
    return Response(
        content="﻿" + output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/api/admin/report/orders")
def report_orders(
    authorization: Authorization = None,
    store_id: int | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    register_id: int | None = None,
    shift_id: int | None = None,
    cashier_id: int | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Orders of the report period, newest first, with everything needed for the order card.

    Unlike the totals, the list also shows cancelled and refunded orders.
    """
    user = require_user(authorization)
    filters = report_filter(user, store_id, start_date, end_date, register_id, shift_id, cashier_id)
    _, _, start_utc, end_utc = core.report_period_bounds(start_date, end_date)
    where, values = order_conditions(filters)
    if shift_id is None:
        where += " AND created_at >= ? AND created_at < ?"
        values += [start_utc, end_utc]
    with closing(core.connect_db()) as connection:
        total = connection.execute(f"SELECT COUNT(*) FROM orders WHERE {where}", values).fetchone()[0]
        rows = connection.execute(
            f"SELECT * FROM orders WHERE {where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            [*values, limit, offset],
        ).fetchall()
        store_names = {row["id"]: row["name"] for row in connection.execute("SELECT id, name FROM stores")}
        orders = [
            {
                **pos.pos_order(connection, row),
                "store_name": store_names.get(row["store_id"], ""),
                "shift_id": row["shift_id"],
                "refunded_at": row["refunded_at"],
                "cancelled_at": row["cancelled_at"],
            }
            for row in rows
        ]
    return {"total": total, "orders": orders}


@router.get("/api/admin/report-filters")
def report_filters(store_id: int | None = None, authorization: Authorization = None) -> dict[str, Any]:
    """Registers and cashiers to choose from in the report filters."""
    user = require_user(authorization)
    filters = report_filter(user, store_id, None, None, None, None, None)
    with closing(core.connect_db()) as connection:
        placeholders = ",".join("?" * len(filters.store_ids)) or "NULL"
        registers = connection.execute(
            f"""
            SELECT devices.id, devices.name, devices.store_id, stores.name AS store_name FROM devices
            JOIN stores ON stores.id = devices.store_id
            WHERE devices.kind = 'register' AND devices.store_id IN ({placeholders})
            ORDER BY stores.id, devices.name
            """,
            filters.store_ids,
        ).fetchall()
        cashiers = connection.execute(
            f"""
            SELECT cashiers.id, cashiers.name, cashiers.store_id, stores.name AS store_name FROM cashiers
            JOIN stores ON stores.id = cashiers.store_id
            WHERE cashiers.store_id IN ({placeholders})
            ORDER BY stores.id, cashiers.name
            """,
            filters.store_ids,
        ).fetchall()
    return {"registers": [dict(row) for row in registers], "cashiers": [dict(row) for row in cashiers]}


@router.get("/api/admin/shifts")
def list_shifts(
    authorization: Authorization = None,
    store_id: int | None = None,
    register_id: int | None = None,
    cashier_id: int | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[dict[str, Any]]:
    """Shifts opened in the period, with the Z report (or the current X report if still open)."""
    user = require_user(authorization)
    filters = report_filter(user, store_id, start_date, end_date, register_id, None, cashier_id)
    _, _, start_utc, end_utc = core.report_period_bounds(start_date, end_date)
    conditions = [f"store_id IN ({','.join('?' * len(filters.store_ids)) or 'NULL'})", "opened_at >= ?", "opened_at < ?"]
    values: list[Any] = [*filters.store_ids, start_utc, end_utc]
    if register_id is not None:
        conditions.append("register_id = ?")
        values.append(register_id)
    if cashier_id is not None:
        conditions.append("(cashier_id = ? OR closed_by = ?)")
        values.extend([cashier_id, cashier_id])
    with closing(core.connect_db()) as connection:
        rows = connection.execute(
            f"SELECT * FROM shifts WHERE {' AND '.join(conditions)} ORDER BY opened_at DESC LIMIT 200", values
        ).fetchall()
        store_names = {row["id"]: row["name"] for row in connection.execute("SELECT id, name FROM stores")}
        result = []
        for row in rows:
            report = json.loads(row["report_json"]) if row["report_json"] else pos.shift_report(connection, row)
            report.setdefault("register_name", pos.device_name(connection, row["register_id"]))
            result.append({**report, "store_name": store_names.get(row["store_id"], ""), "open": row["closed_at"] is None})
    return result
