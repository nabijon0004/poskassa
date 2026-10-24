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
    orders = client.get("/api/admin/stores/1/orders", headers=ADMIN).json()["orders"]
    assert orders[0]["order_ref"] == created["order_ref"]
    assert orders[0]["print_status"] == "failed"


def test_old_jobs_are_not_printed_late(client, monkeypatch):
    monkeypatch.setattr(main, "PRINTER_HOST", "printer.example")
    order(client)
    age_jobs("created_at", timedelta(minutes=20))
    assert take_print_jobs(client)[1] == []
    assert client.get("/api/admin/stores/1/orders", headers=ADMIN).json()["orders"][0]["print_status"] == "failed"


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


def test_a_windows_printer_is_printed_on_by_the_register_that_has_it(client):
    second = client.post(
        "/api/admin/stores/1/devices", headers=ADMIN, json={"kind": "register", "name": "Касса 2"}
    ).json()
    reported = client.put("/api/agent/printers", headers=REGISTER, json={"printers": ["XP-80C", " ", "XP-80C"]})
    assert reported.status_code == 200, reported.text
    client.put("/api/agent/printers", headers={"X-Device-Key": second["key"]}, json={"printers": ["Microsoft Print to PDF"]})
    agents = client.get("/api/admin/stores/1/printer/status", headers=ADMIN).json()["agents"]
    assert {agent["name"]: agent["printers"] for agent in agents}["Касса 2"] == ["Microsoft Print to PDF"]
    assert ["XP-80C"] in [agent["printers"] for agent in agents]

    settings = client.get("/api/admin/stores/1/printer", headers=ADMIN).json()
    settings.update(connection_type="system", host="", system_name="XP-80C")
    assert client.put("/api/admin/stores/1/printer", headers=ADMIN, json=settings).status_code == 200
    assert client.get("/api/admin/stores/1/printer/status", headers=ADMIN).json()["configured"]
    order(client)

    # The register without this printer leaves the job to the one that has it.
    assert take_print_jobs(client, key=second["key"])[1] == []
    printer, jobs = take_print_jobs(client)
    assert printer["connection_type"] == "system"
    assert printer["system_name"] == "XP-80C"
    assert len(jobs) == 1


def test_a_windows_printer_must_be_chosen(client):
    settings = client.get("/api/admin/stores/1/printer", headers=ADMIN).json()
    settings.update(connection_type="system", host="printer.example", system_name="")
    client.put("/api/admin/stores/1/printer", headers=ADMIN, json=settings)
    assert not client.get("/api/admin/stores/1/printer/status", headers=ADMIN).json()["configured"]
    assert client.post("/api/admin/stores/1/printer/test", headers=ADMIN, json=settings).status_code == 503
    settings["system_name"] = "bad\nname"
    assert client.put("/api/admin/stores/1/printer", headers=ADMIN, json=settings).status_code == 422
