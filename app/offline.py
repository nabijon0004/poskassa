"""Offline work of the cash register.

While online, the register keeps a copy of what it needs to work without internet:
the store's menu, cashiers (with their PIN hashes, to check PINs offline), the printer
and receipt settings, and the current shift. Without internet it records shifts and sales
locally, prints on the store's printer itself, and sends the queue here when the
connection returns. Each operation carries its own id, so sending it again is harmless.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Annotated, Any, Literal, Union

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

import app.main as core
from app import pos, stores, telegram

router = APIRouter()

# Timestamps from the register's clock outside this window are replaced by the server time.
MAX_CLOCK_AHEAD = timedelta(minutes=10)
MAX_OFFLINE_AGE = timedelta(days=30)
MAX_OPERATIONS = 200


@router.get("/api/pos/offline-data")
def offline_data(x_device_key: stores.DeviceKey = None) -> dict[str, Any]:
    register = stores.require_device(x_device_key, "register")
    with closing(core.connect_db()) as connection:
        cashiers = connection.execute(
            "SELECT * FROM cashiers WHERE store_id = ? AND active = 1 ORDER BY name", (register.store_id,)
        ).fetchall()
        shift = pos.current_shift(connection, register.id)
        shift_data = (
            {
                "id": shift["id"],
                "opened_at": shift["opened_at"],
                "opening_cash": shift["opening_cash"],
                "cashier_name": pos.cashier_name(connection, shift["cashier_id"]),
            }
            if shift
            else None
        )
    return {
        "generated_at": pos.now_iso(),
        "store": {"id": register.store_id, "name": register.store_name},
        "register": {"id": register.id, "name": register.name},
        "currency": core.CURRENCY,
        "receipt_title": core.RECEIPT_TITLE,
        "menu": core.menu_response(register.store_id).model_dump(),
        # PIN hashes (PBKDF2) let the register check a cashier's PIN without the server.
        "cashiers": [{**pos.public_cashier(row), "pin_hash": row["pin_hash"]} for row in cashiers],
        "printer": core.load_printer_settings(register.store_id).model_dump(),
        "shift": shift_data,
    }


class OpenShiftOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["open_shift"]
    id: str = Field(min_length=8, max_length=64)
    at: datetime
    cashier_id: int
    opening_cash: Decimal = Field(default=Decimal("0"), ge=0, le=1_000_000, decimal_places=2)


class SaleOperation(pos.SaleInput):
    type: Literal["sale"]
    id: str = Field(min_length=8, max_length=64)
    at: datetime
    cashier_id: int
    offline_number: str = Field(default="", max_length=10)
    printed: bool = False


Operation = Annotated[Union[OpenShiftOperation, SaleOperation], Field(discriminator="type")]


class SyncInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operations: list[Operation] = Field(max_length=MAX_OPERATIONS)


def operation_time(at: datetime) -> str:
    """The register's time of the operation, unless its clock is clearly wrong."""
    now = datetime.now(timezone.utc)
    when = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
    if when > now + MAX_CLOCK_AHEAD or when < now - MAX_OFFLINE_AGE:
        core.logger.warning("Offline operation time %s replaced by the server time", at.isoformat())
        return now.isoformat()
    return when.astimezone(timezone.utc).isoformat()


def store_cashier(connection: sqlite3.Connection, store_id: int, cashier_id: int) -> sqlite3.Row:
    # A cashier disabled after the sale still made it; only the store must match.
    cashier = connection.execute(
        "SELECT * FROM cashiers WHERE id = ? AND store_id = ?", (cashier_id, store_id)
    ).fetchone()
    if cashier is None:
        raise HTTPException(status_code=422, detail="Кассир не найден в этой точке.")
    return cashier


def apply_open_shift(register: stores.Device, operation: OpenShiftOperation) -> dict[str, Any]:
    with closing(core.connect_db()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute("SELECT id FROM shifts WHERE client_id = ?", (operation.id,)).fetchone()
        if existing:
            connection.rollback()
            return {"shift_id": existing["id"]}
        cashier = store_cashier(connection, register.store_id, operation.cashier_id)
        current = pos.current_shift(connection, register.id)
        if current is not None:
            # The shift was opened online in the meantime: the offline sales belong to it.
            connection.rollback()
            return {"shift_id": current["id"]}
        cursor = connection.execute(
            """
            INSERT INTO shifts (cashier_id, opened_at, opening_cash, store_id, register_id, client_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                cashier["id"], operation_time(operation.at), pos.money(operation.opening_cash),
                register.store_id, register.id, operation.id,
            ),
        )
        connection.commit()
    telegram.notify_shift_opened(
        store_name=register.store_name, register_name=register.name, cashier_name=cashier["name"],
        shift_id=cursor.lastrowid, opened_at=operation_time(operation.at),
        opening_cash=pos.money(operation.opening_cash), offline=True,
    )
    return {"shift_id": cursor.lastrowid}


def apply_sale(register: stores.Device, operation: SaleOperation) -> dict[str, Any]:
    normalized, subtotal = core.resolve_order_items(
        operation, core.catalog_for(register.store_id), enforce_total_limit=False, require_available=False
    )
    payment = pos.apply_payment(Decimal(str(subtotal)), operation)
    with closing(core.connect_db()) as connection:
        cashier = store_cashier(connection, register.store_id, operation.cashier_id)
        shift = pos.current_shift(connection, register.id)
    if shift is None:
        raise HTTPException(status_code=409, detail="На кассе нет открытой смены для офлайн-продажи.")
    at = operation_time(operation.at)
    request_hash = hashlib.sha256(
        operation.model_dump_json(include={"items", "payment_method", "amount_received", "discount_type", "discount_value"}).encode()
    ).hexdigest()
    row, _ = core.store_order(
        core.parse_idempotency_key(operation.id),
        request_hash,
        {
            "total": payment["total"],
            "items_json": json.dumps(normalized, ensure_ascii=False),
            "created_at": at,
            "comment": operation.comment,
            "service_type": operation.service_type or "",
            "source": "pos",
            "payment_status": "paid",
            "payment_method": payment["payment_method"],
            "subtotal": payment["subtotal"],
            "discount": payment["discount"],
            "amount_received": payment["amount_received"],
            "change_due": payment["change_due"],
            "paid_at": at,
            "shift_id": shift["id"],
            "cashier_id": cashier["id"],
            # Already handed over in the store: it does not go to the queue screen.
            "kitchen_status": "",
            "print_status": "printed" if operation.printed else "not_attempted",
            "store_id": register.store_id,
            "device_id": register.id,
            "register_id": register.id,
            "offline_number": operation.offline_number,
        },
    )
    return {"order_ref": row["order_number"], "order_number": core.display_order_number(row["store_seq"]), "total": row["total"]}


@router.post("/api/pos/offline-sync")
def offline_sync(payload: SyncInput, x_device_key: stores.DeviceKey = None) -> dict[str, Any]:
    """Apply the register's offline operations in order; stop at the first one that fails."""
    register = stores.require_device(x_device_key, "register")
    results = []
    for operation in payload.operations:
        try:
            result = apply_open_shift(register, operation) if operation.type == "open_shift" else apply_sale(register, operation)
        except HTTPException as exc:
            results.append({"id": operation.id, "status": "error", "error": exc.detail})
            break
        results.append({"id": operation.id, "status": "ok", **result})
    synced = sum(result["status"] == "ok" for result in results)
    if synced:
        core.logger.info("Register %s synced %d offline operations", register.id, synced)
    return {"results": results}
