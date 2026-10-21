"""Print queue of each store and the API of the print agent.

The server runs in the cloud and cannot reach printers in the stores' local networks.
Documents are queued per store; the cash register program (PaprikaPOS.exe) in the store
takes the jobs with its register key and sends them to the printer over the local network
or to a printer installed in Windows on that computer.
"""
from __future__ import annotations

import base64
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

import app.main as core
from app import stores

router = APIRouter()

# A job taken by an agent that did not report back is offered again after this time.
CLAIM_TIMEOUT = timedelta(seconds=60)
# Jobs nobody printed in time (the register program was closed) are not printed late.
JOB_EXPIRY = timedelta(minutes=15)
JOBS_PER_REQUEST = 10
# The register program counts as online if it asked for jobs within this time.
AGENT_ONLINE_WINDOW = timedelta(seconds=30)


def now() -> datetime:
    return datetime.now(timezone.utc)


def initialize_print_tables(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS print_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            data BLOB NOT NULL,
            order_ref TEXT,
            status TEXT NOT NULL DEFAULT 'queued',
            claimed_by INTEGER,
            claimed_at TEXT,
            finished_at TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT ''
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS print_jobs_store_status ON print_jobs (store_id, status, id)"
    )
    # Printers installed in Windows on the register computer, as its program reports them.
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(devices)")}
    if "printers" not in columns:
        connection.execute("ALTER TABLE devices ADD COLUMN printers TEXT NOT NULL DEFAULT '[]'")


def enqueue_job(store_id: int, data: bytes, *, kind: str, title: str, order_ref: str | None = None) -> int:
    with closing(core.connect_db()) as connection:
        cursor = connection.execute(
            """
            INSERT INTO print_jobs (store_id, created_at, kind, title, data, order_ref)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (store_id, now().isoformat(), kind, title, data, order_ref),
        )
        connection.commit()
        return cursor.lastrowid


def set_order_print_status(connection: sqlite3.Connection, order_ref: str | None, status: str) -> None:
    if order_ref:
        connection.execute("UPDATE orders SET print_status = ? WHERE order_number = ?", (status, order_ref))


def claim_jobs(store_id: int, device_id: int) -> list[sqlite3.Row]:
    current = now()
    with closing(core.connect_db()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        expired = connection.execute(
            "SELECT id, order_ref FROM print_jobs WHERE store_id = ? AND status = 'queued' AND created_at < ?",
            (store_id, (current - JOB_EXPIRY).isoformat()),
        ).fetchall()
        for job in expired:
            connection.execute(
                """
                UPDATE print_jobs SET status = 'failed', finished_at = ?,
                    error = 'Программа кассы не забрала чек вовремя'
                WHERE id = ?
                """,
                (current.isoformat(), job["id"]),
            )
            set_order_print_status(connection, job["order_ref"], "failed")
        connection.execute(
            """
            UPDATE print_jobs SET status = 'queued', claimed_by = NULL, claimed_at = NULL
            WHERE store_id = ? AND status = 'claimed' AND claimed_at < ?
            """,
            (store_id, (current - CLAIM_TIMEOUT).isoformat()),
        )
        jobs = connection.execute(
            "SELECT * FROM print_jobs WHERE store_id = ? AND status = 'queued' ORDER BY id LIMIT ?",
            (store_id, JOBS_PER_REQUEST),
        ).fetchall()
        connection.executemany(
            """
            UPDATE print_jobs SET status = 'claimed', claimed_by = ?, claimed_at = ?, attempts = attempts + 1
            WHERE id = ?
            """,
            [(device_id, current.isoformat(), job["id"]) for job in jobs],
        )
        connection.commit()
    return jobs


def device_printers(row: sqlite3.Row) -> list[str]:
    try:
        printers = json.loads(row["printers"])
    except ValueError:
        return []
    return [name for name in printers if isinstance(name, str)] if isinstance(printers, list) else []


def agents_status(connection: sqlite3.Connection, store_id: int) -> list[dict[str, Any]]:
    """Registers of the store and whether their program is taking print jobs."""
    online_after = (now() - AGENT_ONLINE_WINDOW).isoformat()
    rows = connection.execute(
        """
        SELECT id, name, active, last_seen_at, printers FROM devices
        WHERE store_id = ? AND kind = 'register' ORDER BY id
        """,
        (store_id,),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "name": row["name"],
            "active": bool(row["active"]),
            "last_seen_at": row["last_seen_at"],
            "online": bool(row["active"] and row["last_seen_at"] and row["last_seen_at"] >= online_after),
            "printers": device_printers(row),
        }
        for row in rows
    ]


def recent_jobs(connection: sqlite3.Connection, store_id: int, limit: int = 10) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT id, created_at, kind, title, status, finished_at, error FROM print_jobs
        WHERE store_id = ? ORDER BY id DESC LIMIT ?
        """,
        (store_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]


# --- agent API (cash register program) ------------------------------------------------


class PrinterList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    printers: list[str] = Field(max_length=100)


class JobResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["printed", "failed"]
    error: str = Field(default="", max_length=300)


@router.get("/api/agent/print-jobs")
def take_print_jobs(x_device_key: stores.DeviceKey = None) -> dict[str, Any]:
    device = stores.require_device(x_device_key, "register")
    settings = core.load_printer_settings(device.store_id)
    if settings.connection_type == "system" and not has_printer(device.id, settings.system_name):
        # The printer is installed on another register of the store: that one prints.
        jobs = []
    else:
        jobs = claim_jobs(device.store_id, device.id)
    return {
        "printer": {
            "connection_type": settings.connection_type,
            "host": settings.host,
            "port": settings.port,
            "system_name": settings.system_name,
            "timeout": settings.timeout,
        },
        "jobs": [
            {"id": job["id"], "title": job["title"], "data": base64.b64encode(job["data"]).decode("ascii")}
            for job in jobs
        ],
    }


def has_printer(device_id: int, name: str) -> bool:
    with closing(core.connect_db()) as connection:
        row = connection.execute("SELECT printers FROM devices WHERE id = ?", (device_id,)).fetchone()
    return row is not None and name in device_printers(row)


@router.put("/api/agent/printers")
def report_printers(payload: PrinterList, x_device_key: stores.DeviceKey = None) -> dict[str, int]:
    """The register program reports the printers installed in Windows on its computer."""
    device = stores.require_device(x_device_key, "register")
    printers = sorted({name.strip()[:255] for name in payload.printers if name.strip()})
    with closing(core.connect_db()) as connection:
        connection.execute(
            "UPDATE devices SET printers = ? WHERE id = ?", (json.dumps(printers, ensure_ascii=False), device.id)
        )
        connection.commit()
    return {"printers": len(printers)}


@router.post("/api/agent/print-jobs/{job_id}")
def report_print_job(job_id: int, payload: JobResult, x_device_key: stores.DeviceKey = None) -> dict[str, str]:
    device = stores.require_device(x_device_key, "register")
    with closing(core.connect_db()) as connection:
        job = connection.execute(
            "SELECT * FROM print_jobs WHERE id = ? AND store_id = ?", (job_id, device.store_id)
        ).fetchone()
        if job is None:
            raise HTTPException(status_code=404, detail="Задание печати не найдено.")
        connection.execute(
            "UPDATE print_jobs SET status = ?, finished_at = ?, error = ? WHERE id = ?",
            (payload.status, now().isoformat(), payload.error, job_id),
        )
        set_order_print_status(connection, job["order_ref"], payload.status)
        connection.commit()
    if payload.status == "failed":
        core.logger.warning("Store %s could not print %s: %s", device.store_id, job["title"], payload.error)
    return {"status": payload.status}
