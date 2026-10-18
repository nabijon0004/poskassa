"""Customer queue screen: order numbers being prepared and orders that are ready.

Kiosk orders (and register sales sent to the kitchen) start as «Готовится»; the cashier
marks them «Готово». A ready number stays on the screen for KIOSK_QUEUE_READY_SECONDS.
Each screen is a device of a store and shows only that store's orders.

The screen is served on its own port (KIOSK_QUEUE_PORT, default 8001) by a second
server started together with the main application. It shows only order numbers.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Any, Generator

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import app.main as core
from app import stores

QUEUE_HOST = os.getenv("KIOSK_QUEUE_HOST", "0.0.0.0")
# 0 disables the queue screen server.
QUEUE_PORT = int(os.getenv("KIOSK_QUEUE_PORT", "8001"))
READY_DISPLAY_SECONDS = int(os.getenv("KIOSK_QUEUE_READY_SECONDS", "300"))
# Address of the queue screen for activation links, e.g. https://queue.example.com (optional).
QUEUE_PUBLIC_URL = os.getenv("KIOSK_QUEUE_PUBLIC_URL", "").strip().rstrip("/")
# Orders never marked ready drop off the screen after this time.
MAX_ORDER_AGE = timedelta(hours=12)
QUEUE_DIR = core.STATIC_DIR / "queue"
NO_CACHE = {"Cache-Control": "no-cache"}


def queue_orders(
    connection: sqlite3.Connection, store_id: int, now: datetime | None = None
) -> list[sqlite3.Row]:
    """Orders shown in the queue: preparing ones and those ready for less than the display time."""
    now = now or datetime.now(timezone.utc)
    return connection.execute(
        """
        SELECT * FROM orders
        WHERE store_id = ? AND created_at >= ?
          AND payment_status NOT IN ('cancelled', 'refunded')
          AND (kitchen_status = 'preparing' OR (kitchen_status = 'ready' AND ready_at >= ?))
        ORDER BY id
        """,
        (
            store_id,
            (now - MAX_ORDER_AGE).isoformat(),
            (now - timedelta(seconds=READY_DISPLAY_SECONDS)).isoformat(),
        ),
    ).fetchall()


def queue_state(store_id: int, now: datetime | None = None) -> dict[str, Any]:
    with closing(core.connect_db()) as connection:
        rows = queue_orders(connection, store_id, now)
    preparing = [
        core.display_order_number(row["store_seq"]) for row in rows if row["kitchen_status"] == "preparing"
    ]
    # The newest ready order comes first.
    ready_rows = sorted(
        (row for row in rows if row["kitchen_status"] == "ready"), key=lambda row: row["ready_at"], reverse=True
    )
    return {
        "preparing": preparing,
        "ready": [core.display_order_number(row["store_seq"]) for row in ready_rows],
    }


queue_app = FastAPI(title="Paprika queue screen", docs_url=None, redoc_url=None, openapi_url=None)
queue_app.mount("/assets", StaticFiles(directory=QUEUE_DIR), name="queue-assets")


@queue_app.get("/", include_in_schema=False)
def queue_page() -> FileResponse:
    return FileResponse(QUEUE_DIR / "index.html", headers=NO_CACHE)


@queue_app.get("/device.js", include_in_schema=False)
def device_script() -> FileResponse:
    return FileResponse(core.STATIC_DIR / "device.js", headers=NO_CACHE)


@queue_app.get("/voice.js", include_in_schema=False)
def voice_script() -> FileResponse:
    return FileResponse(core.STATIC_DIR / "voice.js", headers=NO_CACHE)


@queue_app.get("/logo.jpg", include_in_schema=False)
def queue_logo() -> FileResponse:
    return FileResponse(core.LOGO_PATH, headers=NO_CACHE)


@queue_app.get("/api/queue")
def get_queue(x_device_key: stores.DeviceKey = None) -> dict[str, Any]:
    device = stores.require_device(x_device_key, "queue")
    return {"store_name": device.store_name, **queue_state(device.store_id)}


class QueueServer(uvicorn.Server):
    # Ctrl+C is handled by the main server, which then stops this one.
    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        yield


def start_server() -> tuple[QueueServer, asyncio.Task] | None:
    if not QUEUE_PORT:
        return None
    server = QueueServer(uvicorn.Config(queue_app, host=QUEUE_HOST, port=QUEUE_PORT, log_level="warning"))

    async def serve() -> None:
        try:
            await server.serve()
        except SystemExit:  # uvicorn exits when the port is busy; keep the kiosk running
            core.logger.error("Queue screen could not start on port %d", QUEUE_PORT)

    task = asyncio.create_task(serve())
    core.logger.info("Queue screen on http://%s:%d", QUEUE_HOST, QUEUE_PORT)
    return server, task


async def stop_server(server: QueueServer, task: asyncio.Task) -> None:
    server.should_exit = True
    with contextlib.suppress(Exception, asyncio.CancelledError):
        await task
