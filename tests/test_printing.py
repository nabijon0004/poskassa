import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone

from app import main
from tests.conftest import ADMIN, REGISTER, take_print_jobs


def order(client):
    response = client.post(
        "/api/orders",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={"service_type": "takeaway", "items": [{"product_id": "4", "quantity": 1}]},
    )
    assert response.status_code == 201, response.text
    return response.json()


def age_jobs(column, delta):
    with closing(main.connect_db()) as connection:
        connection.execute(
            f"UPDATE print_jobs SET {column} = ?", ((datetime.now(timezone.utc) - delta).isoformat(),)
        )
        connection.commit()


def test_a_job_is_taken_once_and_offered_again_if_the_agent_does_not_report(client, monkeypatch):
    monkeypatch.setattr(main, "PRINTER_HOST", "printer.example")
    created = order(client)

    first = client.get("/api/agent/print-jobs", headers=REGISTER).json()
    assert len(first["jobs"]) == 1
    assert client.get("/api/agent/print-jobs", headers=REGISTER).json()["jobs"] == []

    # The register program crashed before reporting: the job comes back after the timeout.
    age_jobs("claimed_at", timedelta(seconds=90))
    again = client.get("/api/agent/print-jobs", headers=REGISTER).json()
    assert [job["id"] for job in again["jobs"]] == [first["jobs"][0]["id"]]
    failed = client.post(
        f"/api/agent/print-jobs/{again['jobs'][0]['id']}",
        headers=REGISTER,
        json={"status": "failed", "error": "Нет связи с принтером"},
    )
    assert failed.status_code == 200
    orders = client.get("/api/admin/stores/1/orders", headers=ADMIN).json()
    assert orders[0]["order_ref"] == created["order_ref"]
    assert orders[0]["print_status"] == "failed"


def test_old_jobs_are_not_printed_late(client, monkeypatch):
    monkeypatch.setattr(main, "PRINTER_HOST", "printer.example")
    order(client)
    age_jobs("created_at", timedelta(minutes=20))
    assert take_print_jobs(client)[1] == []
    assert client.get("/api/admin/stores/1/orders", headers=ADMIN).json()[0]["print_status"] == "failed"


def test_agent_needs_a_register_of_the_same_store(client, monkeypatch):
    monkeypatch.setattr(main, "PRINTER_HOST", "printer.example")
    order(client)
    assert client.get("/api/agent/print-jobs").status_code == 403  # the kiosk key
    store = client.post("/api/admin/stores", headers=ADMIN, json={"name": "Другая"}).json()
    other = client.post(
        f"/api/admin/stores/{store['id']}/devices", headers=ADMIN, json={"kind": "register", "name": "Касса"}
    ).json()
    assert take_print_jobs(client, key=other["key"])[1] == []
    job_id = take_print_jobs(client, result="printed")
    assert len(job_id[1]) == 1
    foreign = client.post("/api/agent/print-jobs/1", headers={"X-Device-Key": other["key"]}, json={"status": "printed"})
    assert foreign.status_code == 404
