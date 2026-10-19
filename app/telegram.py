"""Telegram notifications about opened and closed shifts.

Set KIOSK_TELEGRAM_BOT_TOKEN and KIOSK_TELEGRAM_CHAT_ID (several chats separated by
commas) to enable them. Messages are sent in a background thread: a slow or unreachable
Telegram never delays the register.
"""
from __future__ import annotations

import html
import json
import os
import threading
import time
import urllib.request
from contextlib import closing
from datetime import datetime
from typing import Any

import app.main as core

BOT_TOKEN = os.getenv("KIOSK_TELEGRAM_BOT_TOKEN", "").strip()
CHAT_IDS = [chat.strip() for chat in os.getenv("KIOSK_TELEGRAM_CHAT_ID", "").split(",") if chat.strip()]
TIMEOUT_SECONDS = 10


def enabled() -> bool:
    return bool(BOT_TOKEN and CHAT_IDS)


def _api(method: str, payload: dict[str, Any], timeout: float = TIMEOUT_SECONDS) -> Any:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response).get("result")


def _send_to(chat_id: str | int, text: str) -> None:
    try:
        _api("sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML"})
    except Exception as exc:  # noqa: BLE001 - a notification must never break a shift
        core.logger.warning("Could not send Telegram message to %s: %s", chat_id, exc)


def _post(text: str) -> None:
    for chat_id in CHAT_IDS:
        _send_to(chat_id, text)


def send(text: str) -> None:
    if enabled():
        threading.Thread(target=_post, args=(text,), daemon=True).start()


def _when(value: str | None) -> str:
    return datetime.fromisoformat(value).astimezone().strftime("%d.%m.%Y %H:%M") if value else "—"


def _escape(value: Any) -> str:
    return html.escape(str(value))


def notify_shift_opened(
    *, store_name: str, register_name: str, cashier_name: str, shift_id: int,
    opened_at: str, opening_cash: float, offline: bool = False,
) -> None:
    lines = [
        f"🟢 <b>{_escape(store_name)}</b>",
        f"Время: {_when(opened_at)}",
        f"Выручка на начало: {core.format_receipt_money(opening_cash)}",
    ]
    send("\n".join(lines))


def notify_shift_closed(store_name: str, report: dict[str, Any]) -> None:
    lines = [
        f"🔴 <b>{_escape(store_name)}</b>",
        f"Время: {_when(report['closed_at'])}",
        f"Выручка: {core.format_receipt_money(report['revenue'])}",
    ]
    send("\n".join(lines))


# --- revenue on request ------------------------------------------------------------

# Code sent to the bot -> store name, e.g. "5555=Paprika Gafurov,1111=Paprika 12mkr".
# The bot answers with that store's current revenue, only in KIOSK_TELEGRAM_CHAT_ID chats.
REVENUE_CODES = {
    code.strip(): name.strip()
    for code, _, name in (
        item.partition("=") for item in os.getenv("KIOSK_TELEGRAM_REVENUE_CODES", "").split(",")
    )
    if code.strip() and name.strip()
}
POLL_SECONDS = 25
_stop = threading.Event()


def current_revenue_text(store_name: str) -> str:
    from app import pos

    now = datetime.now().astimezone().strftime("%d.%m.%Y %H:%M")
    with closing(core.connect_db()) as connection:
        store = connection.execute(
            "SELECT id, name FROM stores WHERE lower(name) = lower(?) ORDER BY id LIMIT 1", (store_name,)
        ).fetchone()
        if store is None:
            return f"Точка «{_escape(store_name)}» не найдена."
        shifts = connection.execute(
            "SELECT * FROM shifts WHERE store_id = ? AND closed_at IS NULL ORDER BY id", (store["id"],)
        ).fetchall()
        reports = [pos.shift_report(connection, shift) for shift in shifts]
    lines = [f"💰 <b>{_escape(store['name'])}</b>", f"Время: {now}"]
    if reports:
        lines.append(f"Смена открыта: {_when(min(report['opened_at'] for report in reports))}")
        revenue = sum(report["revenue"] for report in reports)
        lines.append(f"Текущая выручка: {core.format_receipt_money(revenue)}")
    else:
        lines.append("Смена закрыта")
    return "\n".join(lines)


def _poll() -> None:
    allowed = set(CHAT_IDS)
    offset = None
    started = time.time()
    while not _stop.is_set():
        try:
            payload: dict[str, Any] = {"timeout": POLL_SECONDS, "allowed_updates": ["message"]}
            if offset is not None:
                payload["offset"] = offset
            updates = _api("getUpdates", payload, timeout=POLL_SECONDS + 10) or []
        except Exception as exc:  # noqa: BLE001 - keep polling after network errors
            core.logger.warning("Telegram polling failed: %s", exc)
            _stop.wait(15)
            continue
        for update in updates:
            offset = update["update_id"] + 1
            message = update.get("message") or {}
            chat_id = str((message.get("chat") or {}).get("id", ""))
            # Messages sent while the server was down are not answered.
            if message.get("date", 0) < started - 60 or chat_id not in allowed:
                continue
            store_name = REVENUE_CODES.get((message.get("text") or "").strip())
            if store_name:
                try:
                    text = current_revenue_text(store_name)
                except Exception as exc:  # noqa: BLE001
                    core.logger.exception("Could not build the revenue reply")
                    text = f"Не удалось получить выручку: {_escape(exc)}"
                _send_to(chat_id, text)


def start_bot() -> None:
    if enabled() and REVENUE_CODES:
        _stop.clear()
        threading.Thread(target=_poll, name="telegram-bot", daemon=True).start()
        core.logger.info("Telegram bot answers revenue codes for %d stores", len(REVENUE_CODES))


def stop_bot() -> None:
    _stop.set()
