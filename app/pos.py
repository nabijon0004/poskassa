"""Cash register (POS) API: shifts, sales, kiosk order payment and returns.

A register is a device of a store, activated with its key. Cashiers of that store log in
by PIN; each register has its own shift. The register uses the store's menu, orders,
order numbers and printer. Receipts are non-fiscal.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import time as time_module
from collections import deque
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

import app.main as core
from app import queue_screen, stores

router = APIRouter()

SESSION_TTL_SECONDS = 12 * 60 * 60
LOGIN_WINDOW_SECONDS = 5 * 60
LOGIN_MAX_FAILURES = 10
PIN_PATTERN = r"^\d{4,6}$"
# The receipts tab shows only the latest receipts.
RECENT_RECEIPTS_LIMIT = 15

# token -> (cashier_id, register_id, expires_at). Cashiers log in again after a server restart.
_sessions: dict[str, tuple[int, int, float]] = {}
# register_id -> times of wrong PINs
_failed_logins: dict[int, deque[float]] = {}


# --- models ----------------------------------------------------------------------


class CashierCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=60)
    pin: str = Field(pattern=PIN_PATTERN)
    can_manage_shift: bool = False

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("Укажите имя кассира.")
        return value


class CashierUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=60)
    pin: str | None = Field(default=None, pattern=PIN_PATTERN)
    active: bool | None = None
    can_manage_shift: bool | None = None


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pin: str = Field(pattern=PIN_PATTERN)


class OpenShiftInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    opening_cash: Decimal = Field(default=Decimal("0"), ge=0, le=1_000_000, decimal_places=2)


class CloseShiftInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    counted_cash: Decimal = Field(ge=0, le=1_000_000, decimal_places=2)
    print_report: bool = True


class PaymentFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payment_method: Literal["cash", "card"]
    amount_received: Decimal | None = Field(default=None, ge=0, le=1_000_000, decimal_places=2)
    discount_type: Literal["percent", "amount"] | None = None
    discount_value: Decimal = Field(default=Decimal("0"), ge=0, le=1_000_000, decimal_places=2)
    print_receipt: bool = True


class SaleInput(PaymentFields):
    items: list[core.OrderLineInput] = Field(min_length=1, max_length=100)
    service_type: Literal["dine_in", "takeaway"] | None = None
    comment: str = Field(default="", max_length=100)
    send_to_kitchen: bool = True

    @field_validator("comment")
    @classmethod
    def normalize_comment(cls, value: str) -> str:
        return core.OrderInput.normalize_comment(value)


class PayOrderInput(PaymentFields):
    pass


class KitchenStatusInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["preparing", "ready"]


# --- storage ---------------------------------------------------------------------


def add_missing_columns(connection: sqlite3.Connection, table: str, columns: dict[str, str]) -> set[str]:
    existing = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
    added = set()
    for name, definition in columns.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            added.add(name)
    return added


def initialize_pos_tables(connection: sqlite3.Connection, default_store_id: int) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS cashiers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            pin_hash TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
        """
    )
    # Opening/closing shifts and X/Z reports need access granted by the admin.
    # Cashiers who existed before this setting keep the access they had.
    added = add_missing_columns(
        connection,
        "cashiers",
        {
            "can_manage_shift": "INTEGER NOT NULL DEFAULT 0",
            "store_id": f"INTEGER NOT NULL DEFAULT {default_store_id}",
        },
    )
    if "can_manage_shift" in added:
        connection.execute("UPDATE cashiers SET can_manage_shift = 1")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS shifts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cashier_id INTEGER NOT NULL,
            opened_at TEXT NOT NULL,
            opening_cash REAL NOT NULL DEFAULT 0,
            closed_at TEXT,
            closed_by INTEGER,
            counted_cash REAL,
            report_json TEXT
        )
        """
    )
    add_missing_columns(
        connection,
        "shifts",
        {
            "store_id": f"INTEGER NOT NULL DEFAULT {default_store_id}",
            "register_id": "INTEGER",
            # A shift opened by a register without internet, sent later (see app/offline.py).
            "client_id": "TEXT",
        },
    )
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS shifts_client_id ON shifts (client_id)")
    # Shifts from before registers existed belong to the first register of their store.
    connection.execute(
        """
        UPDATE shifts SET register_id = (
            SELECT MIN(devices.id) FROM devices
            WHERE devices.store_id = shifts.store_id AND devices.kind = 'register'
        )
        WHERE register_id IS NULL
        """
    )
    connection.execute(
        """
        UPDATE orders SET register_id = (SELECT shifts.register_id FROM shifts WHERE shifts.id = orders.shift_id)
        WHERE register_id IS NULL AND shift_id IS NOT NULL
        """
    )


def hash_pin(pin: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, 120_000)
    return f"{salt.hex()}${digest.hex()}"


def pin_matches(pin: str, stored: str) -> bool:
    salt_hex, _, _ = stored.partition("$")
    return hmac.compare_digest(hash_pin(pin, bytes.fromhex(salt_hex)), stored)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def money(value: Decimal | float | int) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def public_cashier(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "store_id": row["store_id"],
        "name": row["name"],
        "active": bool(row["active"]),
        "can_manage_shift": bool(row["can_manage_shift"]),
    }


def find_cashier_by_pin(
    connection: sqlite3.Connection, store_id: int, pin: str, *, exclude_id: int | None = None
):
    """PINs are unique within a store."""
    for row in connection.execute("SELECT * FROM cashiers WHERE store_id = ? AND active = 1", (store_id,)):
        if row["id"] != exclude_id and pin_matches(pin, row["pin_hash"]):
            return row
    return None


def end_cashier_sessions(cashier_id: int) -> None:
    for token, (session_cashier, _, _) in list(_sessions.items()):
        if session_cashier == cashier_id:
            _sessions.pop(token, None)


def current_shift(connection: sqlite3.Connection, register_id: int) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM shifts WHERE register_id = ? AND closed_at IS NULL ORDER BY id DESC LIMIT 1",
        (register_id,),
    ).fetchone()


def cashier_name(connection: sqlite3.Connection, cashier_id: int | None) -> str:
    if cashier_id is None:
        return ""
    row = connection.execute("SELECT name FROM cashiers WHERE id = ?", (cashier_id,)).fetchone()
    return row["name"] if row else ""


def device_name(connection: sqlite3.Connection, device_id: int | None) -> str:
    if device_id is None:
        return ""
    row = connection.execute("SELECT name FROM devices WHERE id = ?", (device_id,)).fetchone()
    return row["name"] if row else ""


def pos_order(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    return {
        **core.public_order(row),
        "cashier_name": cashier_name(connection, row["cashier_id"]),
        "register_name": device_name(connection, row["register_id"]),
    }


# --- auth --------------------------------------------------------------------------


@dataclass(frozen=True)
class PosContext:
    cashier: sqlite3.Row
    register_id: int
    register_name: str
    store_id: int
    store_name: str


def require_cashier(token: str | None) -> PosContext:
    session = _sessions.get(token or "")
    if session is None or session[2] < time_module.time():
        _sessions.pop(token or "", None)
        raise HTTPException(status_code=401, detail="Войдите в кассу по PIN-коду.")
    cashier_id, register_id, _ = session
    with closing(core.connect_db()) as connection:
        register = connection.execute(
            """
            SELECT devices.*, stores.name AS store_name FROM devices
            JOIN stores ON stores.id = devices.store_id
            WHERE devices.id = ? AND devices.active = 1 AND stores.active = 1
            """,
            (register_id,),
        ).fetchone()
        cashier = connection.execute(
            "SELECT * FROM cashiers WHERE id = ? AND active = 1", (cashier_id,)
        ).fetchone()
    if register is None or cashier is None or cashier["store_id"] != register["store_id"]:
        _sessions.pop(token or "", None)
        raise HTTPException(status_code=401, detail="Кассир или касса отключены.")
    return PosContext(cashier, register["id"], register["name"], register["store_id"], register["store_name"])


def require_shift_access(context: PosContext) -> None:
    if not context.cashier["can_manage_shift"]:
        raise HTTPException(status_code=403, detail="Нет доступа к смене. Обратитесь к администратору.")


def require_open_shift(connection: sqlite3.Connection, context: PosContext) -> sqlite3.Row:
    shift = current_shift(connection, context.register_id)
    if shift is None:
        raise HTTPException(status_code=409, detail="Смена не открыта.")
    return shift


PosToken = Annotated[str | None, Header(alias="X-POS-Token")]


# --- payment helpers ---------------------------------------------------------------


def apply_payment(subtotal: Decimal, payment: PaymentFields) -> dict[str, Any]:
    """Discount, total, received amount and change for a payment."""
    if payment.discount_type == "percent":
        if payment.discount_value > 100:
            raise HTTPException(status_code=422, detail="Скидка не может быть больше 100%.")
        discount = subtotal * payment.discount_value / 100
    elif payment.discount_type == "amount":
        discount = payment.discount_value
    else:
        discount = Decimal("0")
    discount = discount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if discount > subtotal:
        raise HTTPException(status_code=422, detail="Скидка больше суммы чека.")
    total = subtotal - discount
    if payment.payment_method == "cash":
        received = payment.amount_received if payment.amount_received is not None else total
        if received < total:
            raise HTTPException(status_code=422, detail="Получено меньше суммы к оплате.")
    else:
        received = total
    return {
        "subtotal": money(subtotal),
        "discount": money(discount),
        "total": money(total),
        "payment_method": payment.payment_method,
        "amount_received": money(received),
        "change_due": money(received - total),
    }


def print_customer_receipt(order: dict[str, Any], name: str, store_id: int) -> str:
    try:
        settings = core.load_printer_settings(store_id)
        core.OrderPrinter.print_lines(
            core.build_customer_receipt(order, settings, cashier_name=name),
            store_id,
            title=f"Чек № {order['order_number']}",
            settings=settings,
        )
        return "queued"
    except core.PrinterError as exc:
        core.logger.warning("Customer receipt failed for order %s: %s", order["order_number"], exc)
        return "failed"


# --- session ------------------------------------------------------------------------


def session_state(context: PosContext) -> dict[str, Any]:
    with closing(core.connect_db()) as connection:
        shift = current_shift(connection, context.register_id)
        shift_data = (
            {
                "id": shift["id"],
                "opened_at": shift["opened_at"],
                "opening_cash": shift["opening_cash"],
                "cashier_name": cashier_name(connection, shift["cashier_id"]),
            }
            if shift
            else None
        )
    return {
        "cashier": public_cashier(context.cashier),
        "shift": shift_data,
        "currency": core.CURRENCY,
        "register": {"id": context.register_id, "name": context.register_name},
        "store": {"id": context.store_id, "name": context.store_name},
    }


@router.post("/api/pos/login")
def login(payload: LoginInput, x_device_key: stores.DeviceKey = None) -> dict[str, Any]:
    register = stores.require_device(x_device_key, "register")
    now = time_module.time()
    failures = _failed_logins.setdefault(register.id, deque())
    while failures and failures[0] < now - LOGIN_WINDOW_SECONDS:
        failures.popleft()
    if len(failures) >= LOGIN_MAX_FAILURES:
        raise HTTPException(status_code=429, detail="Слишком много попыток. Подождите несколько минут.")
    with closing(core.connect_db()) as connection:
        cashier = find_cashier_by_pin(connection, register.store_id, payload.pin)
    if cashier is None:
        failures.append(now)
        raise HTTPException(status_code=401, detail="Неверный PIN-код.")
    token = secrets.token_urlsafe(32)
    _sessions[token] = (cashier["id"], register.id, now + SESSION_TTL_SECONDS)
    context = PosContext(cashier, register.id, register.name, register.store_id, register.store_name)
    return {"token": token, **session_state(context)}


@router.post("/api/pos/logout")
def logout(x_pos_token: PosToken = None) -> dict[str, bool]:
    _sessions.pop(x_pos_token or "", None)
    return {"ok": True}


@router.get("/api/pos/session")
def get_session(x_pos_token: PosToken = None) -> dict[str, Any]:
    return session_state(require_cashier(x_pos_token))


@router.get("/api/pos/menu")
def pos_menu(x_pos_token: PosToken = None) -> core.MenuResponse:
    return core.menu_response(require_cashier(x_pos_token).store_id)


# --- shifts -------------------------------------------------------------------------


def shift_report(connection: sqlite3.Connection, shift: sqlite3.Row) -> dict[str, Any]:
    sales = {
        row["payment_method"]: row
        for row in connection.execute(
            """
            SELECT payment_method, COUNT(*) AS count, SUM(total) AS amount, SUM(discount) AS discount
            FROM orders WHERE shift_id = ? AND payment_status IN ('paid', 'refunded')
            GROUP BY payment_method
            """,
            (shift["id"],),
        )
    }
    refunds = {
        row["payment_method"]: row
        for row in connection.execute(
            """
            SELECT payment_method, COUNT(*) AS count, SUM(total) AS amount
            FROM orders WHERE refund_shift_id = ? GROUP BY payment_method
            """,
            (shift["id"],),
        )
    }

    def total(rows: dict[str, sqlite3.Row], method: str | None, field: str) -> float:
        selected = rows.values() if method is None else [rows[method]] if method in rows else []
        return money(sum((row[field] or 0) for row in selected))

    sales_total = total(sales, None, "amount")
    refunds_total = total(refunds, None, "amount")
    expected_cash = money(
        shift["opening_cash"] + total(sales, "cash", "amount") - total(refunds, "cash", "amount")
    )
    report = {
        "shift_id": shift["id"],
        "store_id": shift["store_id"],
        "register_id": shift["register_id"],
        "register_name": device_name(connection, shift["register_id"]),
        "cashier_name": cashier_name(connection, shift["cashier_id"]),
        "closed_by_name": cashier_name(connection, shift["closed_by"]),
        "opened_at": shift["opened_at"],
        "closed_at": shift["closed_at"],
        "opening_cash": money(shift["opening_cash"]),
        "sales_count": int(total(sales, None, "count")),
        "sales_total": sales_total,
        "sales_cash": total(sales, "cash", "amount"),
        "sales_card": total(sales, "card", "amount"),
        "discounts_total": total(sales, None, "discount"),
        "refunds_count": int(total(refunds, None, "count")),
        "refunds_total": refunds_total,
        "refunds_cash": total(refunds, "cash", "amount"),
        "refunds_card": total(refunds, "card", "amount"),
        "revenue": money(sales_total - refunds_total),
        "expected_cash": expected_cash,
        "counted_cash": shift["counted_cash"],
        "difference": (
            money(shift["counted_cash"] - expected_cash) if shift["counted_cash"] is not None else None
        ),
    }
    return report


def shift_report_lines(report: dict[str, Any], title: str) -> list[dict[str, Any]]:
    settings = core.load_printer_settings(report["store_id"])
    columns = settings.chars_per_line
    line = core._amount_line

    def when(value: str | None) -> str:
        return datetime.fromisoformat(value).astimezone().strftime("%d.%m.%Y %H:%M") if value else "—"

    lines = [
        core._receipt_line(title, align="center", bold=True, width=2, height=2),
        core._receipt_line(f"Смена № {report['shift_id']}", align="center", bold=True),
        core._receipt_line(report["register_name"][:columns], align="center"),
        core._receipt_line(f"Кассир: {report['cashier_name']}"[:columns], align="center"),
        core._receipt_line(f"Открыта: {when(report['opened_at'])}"),
        core._receipt_line(f"Закрыта: {when(report['closed_at'])}"),
        core._receipt_line("-" * columns),
        core._receipt_line(f"Продаж: {report['sales_count']}"),
        line("Продажи всего", report["sales_total"], columns, bold=True),
        line("  наличные", report["sales_cash"], columns),
        line("  карта", report["sales_card"], columns),
        line("Скидки", report["discounts_total"], columns),
        core._receipt_line(f"Возвратов: {report['refunds_count']}"),
        line("Возвраты всего", report["refunds_total"], columns),
        line("Выручка", report["revenue"], columns, bold=True),
        core._receipt_line("-" * columns),
        line("Наличные на начало", report["opening_cash"], columns),
        line("Наличные в кассе", report["expected_cash"], columns, bold=True),
    ]
    if report["counted_cash"] is not None:
        lines.append(line("Пересчитано", report["counted_cash"], columns))
        lines.append(line("Расхождение", report["difference"], columns, bold=True))
    lines.append(core._receipt_line("Нефискальный документ", align="center"))
    return lines


def try_print(lines: list[dict[str, Any]], store_id: int, title: str) -> str:
    try:
        core.OrderPrinter.print_lines(lines, store_id, title=title)
        return "queued"
    except core.PrinterError as exc:
        core.logger.warning("Could not print %s: %s", title, exc)
        return "failed"


@router.post("/api/pos/shifts/open")
def open_shift(payload: OpenShiftInput, x_pos_token: PosToken = None) -> dict[str, Any]:
    context = require_cashier(x_pos_token)
    require_shift_access(context)
    with closing(core.connect_db()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        if current_shift(connection, context.register_id) is not None:
            connection.rollback()
            raise HTTPException(status_code=409, detail="Смена уже открыта.")
        connection.execute(
            """
            INSERT INTO shifts (cashier_id, opened_at, opening_cash, store_id, register_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                context.cashier["id"], now_iso(), money(payload.opening_cash),
                context.store_id, context.register_id,
            ),
        )
        connection.commit()
    return session_state(context)


@router.get("/api/pos/shifts/current")
def current_shift_report(x_pos_token: PosToken = None) -> dict[str, Any]:
    context = require_cashier(x_pos_token)
    require_shift_access(context)
    with closing(core.connect_db()) as connection:
        return shift_report(connection, require_open_shift(connection, context))


@router.post("/api/pos/shifts/current/print")
def print_x_report(x_pos_token: PosToken = None) -> dict[str, Any]:
    context = require_cashier(x_pos_token)
    require_shift_access(context)
    with closing(core.connect_db()) as connection:
        report = shift_report(connection, require_open_shift(connection, context))
    return {"print_status": try_print(shift_report_lines(report, "X-ОТЧЁТ"), context.store_id, "X-отчёт")}


@router.post("/api/pos/shifts/close")
def close_shift(payload: CloseShiftInput, x_pos_token: PosToken = None) -> dict[str, Any]:
    context = require_cashier(x_pos_token)
    require_shift_access(context)
    with closing(core.connect_db()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        shift = current_shift(connection, context.register_id)
        if shift is None:
            connection.rollback()
            raise HTTPException(status_code=409, detail="Смена не открыта.")
        connection.execute(
            "UPDATE shifts SET closed_at = ?, closed_by = ?, counted_cash = ? WHERE id = ?",
            (now_iso(), context.cashier["id"], money(payload.counted_cash), shift["id"]),
        )
        closed = connection.execute("SELECT * FROM shifts WHERE id = ?", (shift["id"],)).fetchone()
        report = shift_report(connection, closed)
        connection.execute(
            "UPDATE shifts SET report_json = ? WHERE id = ?",
            (json.dumps(report, ensure_ascii=False), shift["id"]),
        )
        connection.commit()
    report["print_status"] = (
        try_print(shift_report_lines(report, "Z-ОТЧЁТ"), context.store_id, "Z-отчёт")
        if payload.print_report
        else "not_requested"
    )
    return report


# --- sales -----------------------------------------------------------------------


@router.post("/api/pos/sales", status_code=201)
def create_sale(
    payload: SaleInput,
    x_pos_token: PosToken = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    context = require_cashier(x_pos_token)
    parsed_key = core.parse_idempotency_key(idempotency_key)
    normalized, subtotal = core.resolve_order_items(
        payload, core.catalog_for(context.store_id), enforce_total_limit=False
    )
    payment = apply_payment(Decimal(str(subtotal)), payload)
    with closing(core.connect_db()) as connection:
        shift = require_open_shift(connection, context)
    request_hash = hashlib.sha256(
        payload.model_dump_json(exclude={"print_receipt", "send_to_kitchen"}).encode()
    ).hexdigest()
    paid_at = now_iso()
    row, created = core.store_order(
        parsed_key,
        request_hash,
        {
            "total": payment["total"],
            "items_json": json.dumps(normalized, ensure_ascii=False),
            "created_at": paid_at,
            "comment": payload.comment,
            "service_type": payload.service_type or "",
            "source": "pos",
            "payment_status": "paid",
            "payment_method": payment["payment_method"],
            "subtotal": payment["subtotal"],
            "discount": payment["discount"],
            "amount_received": payment["amount_received"],
            "change_due": payment["change_due"],
            "paid_at": paid_at,
            "shift_id": shift["id"],
            "cashier_id": context.cashier["id"],
            "kitchen_status": "preparing" if payload.send_to_kitchen else "",
            "store_id": context.store_id,
            "device_id": context.register_id,
            "register_id": context.register_id,
        },
    )
    with closing(core.connect_db()) as connection:
        order = pos_order(connection, row)
    if not created:
        return order
    if payload.send_to_kitchen:
        try:
            core.OrderPrinter.print_order(order, context.store_id)
            order["print_status"] = "queued"
        except core.PrinterError as exc:
            order["print_status"] = "failed"
            core.logger.warning("Kitchen print failed for order %s: %s", order["order_number"], exc)
        core.update_print_status(parsed_key, order["print_status"])
    order["receipt_status"] = (
        print_customer_receipt(order, context.cashier["name"], context.store_id)
        if payload.print_receipt
        else "not_requested"
    )
    return order


def load_order(connection: sqlite3.Connection, context: PosContext, order_ref: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM orders WHERE order_number = ? AND store_id = ?", (order_ref, context.store_id)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Заказ не найден.")
    return row


@router.get("/api/pos/orders")
def list_orders(
    scope: Literal["kiosk_unpaid", "recent", "kitchen"] = "recent", x_pos_token: PosToken = None
) -> list[dict[str, Any]]:
    context = require_cashier(x_pos_token)
    _, _, start_utc, _ = core.report_period_bounds(None, None)
    with closing(core.connect_db()) as connection:
        if scope == "kitchen":
            rows = queue_screen.queue_orders(connection, context.store_id)
        elif scope == "kiosk_unpaid":
            rows = connection.execute(
                """
                SELECT * FROM orders
                WHERE store_id = ? AND source = 'kiosk' AND payment_status = 'unpaid' AND created_at >= ?
                ORDER BY id
                """,
                (context.store_id, start_utc),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM orders WHERE store_id = ? ORDER BY id DESC LIMIT ?",
                (context.store_id, RECENT_RECEIPTS_LIMIT),
            ).fetchall()
        return [pos_order(connection, row) for row in rows]


@router.post("/api/pos/orders/{order_ref}/pay")
def pay_order(order_ref: str, payload: PayOrderInput, x_pos_token: PosToken = None) -> dict[str, Any]:
    context = require_cashier(x_pos_token)
    with closing(core.connect_db()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            shift = require_open_shift(connection, context)
            row = load_order(connection, context, order_ref)
            if row["payment_status"] != "unpaid":
                raise HTTPException(status_code=409, detail="Заказ уже оплачен или отменён.")
            payment = apply_payment(Decimal(str(row["total"])), payload)
            connection.execute(
                """
                UPDATE orders SET payment_status = 'paid', payment_method = ?, subtotal = ?,
                    discount = ?, total = ?, amount_received = ?, change_due = ?, paid_at = ?,
                    shift_id = ?, cashier_id = ?, register_id = ?
                WHERE id = ?
                """,
                (
                    payment["payment_method"], payment["subtotal"], payment["discount"],
                    payment["total"], payment["amount_received"], payment["change_due"],
                    now_iso(), shift["id"], context.cashier["id"], context.register_id, row["id"],
                ),
            )
            connection.commit()
        except HTTPException:
            connection.rollback()
            raise
        order = pos_order(connection, load_order(connection, context, order_ref))
    order["receipt_status"] = (
        print_customer_receipt(order, context.cashier["name"], context.store_id)
        if payload.print_receipt
        else "not_requested"
    )
    return order


@router.post("/api/pos/orders/{order_ref}/cancel")
def cancel_order(order_ref: str, x_pos_token: PosToken = None) -> dict[str, Any]:
    context = require_cashier(x_pos_token)
    with closing(core.connect_db()) as connection:
        row = load_order(connection, context, order_ref)
        if row["payment_status"] != "unpaid":
            raise HTTPException(status_code=409, detail="Отменить можно только неоплаченный заказ.")
        connection.execute(
            "UPDATE orders SET payment_status = 'cancelled', cancelled_at = ? WHERE id = ?",
            (now_iso(), row["id"]),
        )
        connection.commit()
        return pos_order(connection, load_order(connection, context, order_ref))


@router.post("/api/pos/orders/{order_ref}/kitchen")
def set_kitchen_status(
    order_ref: str, payload: KitchenStatusInput, x_pos_token: PosToken = None
) -> dict[str, Any]:
    """Mark an order ready for the queue screen, or return it to «Готовится»."""
    context = require_cashier(x_pos_token)
    with closing(core.connect_db()) as connection:
        row = load_order(connection, context, order_ref)
        if not row["kitchen_status"] or row["payment_status"] in {"cancelled", "refunded"}:
            raise HTTPException(status_code=409, detail="Заказ не стоит в очереди.")
        if row["kitchen_status"] != payload.status:
            connection.execute(
                "UPDATE orders SET kitchen_status = ?, ready_at = ? WHERE id = ?",
                (payload.status, now_iso() if payload.status == "ready" else None, row["id"]),
            )
            connection.commit()
        return pos_order(connection, load_order(connection, context, order_ref))


@router.post("/api/pos/orders/{order_ref}/refund")
def refund_order(order_ref: str, x_pos_token: PosToken = None) -> dict[str, Any]:
    context = require_cashier(x_pos_token)
    with closing(core.connect_db()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            shift = require_open_shift(connection, context)
            row = load_order(connection, context, order_ref)
            if row["payment_status"] != "paid":
                raise HTTPException(status_code=409, detail="Вернуть можно только оплаченный заказ.")
            connection.execute(
                """
                UPDATE orders SET payment_status = 'refunded', refunded_at = ?, refund_shift_id = ?
                WHERE id = ?
                """,
                (now_iso(), shift["id"], row["id"]),
            )
            connection.commit()
        except HTTPException:
            connection.rollback()
            raise
        return pos_order(connection, load_order(connection, context, order_ref))


@router.post("/api/pos/orders/{order_ref}/receipt")
def reprint_receipt(order_ref: str, x_pos_token: PosToken = None) -> dict[str, Any]:
    context = require_cashier(x_pos_token)
    with closing(core.connect_db()) as connection:
        row = load_order(connection, context, order_ref)
        if row["payment_status"] != "paid":
            raise HTTPException(status_code=409, detail="Чек печатается только для оплаченного заказа.")
        order = pos_order(connection, row)
    status = print_customer_receipt(order, order["cashier_name"], context.store_id)
    if status == "failed":
        raise HTTPException(status_code=503, detail="Не удалось напечатать чек.")
    return {"receipt_status": status}
