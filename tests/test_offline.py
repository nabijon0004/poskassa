import uuid
from datetime import datetime, timedelta, timezone

from app import pos
from tests.conftest import ADMIN, REGISTER
from tests.test_pos import add_cashier, login, open_shift


def sale_operation(cashier_id, at, **changes):
    return {
        "type": "sale",
        "id": str(uuid.uuid4()),
        "at": at.isoformat(),
        "cashier_id": cashier_id,
        "offline_number": "О1",
        "items": [{"product_id": "4", "quantity": 2}],
        "payment_method": "cash",
        "amount_received": "30",
        "service_type": "takeaway",
        "printed": True,
        **changes,
    }


def sync(client, operations):
    response = client.post("/api/pos/offline-sync", headers=REGISTER, json={"operations": operations})
    assert response.status_code == 200, response.text
    return response.json()["results"]


def test_register_gets_what_it_needs_to_work_offline(client):
    cashier = add_cashier(client)
    data = client.get("/api/pos/offline-data", headers=REGISTER).json()
    assert data["store"]["name"] == "Paprika"
    assert data["register"]["name"] == "Тестовая касса"
    assert len(data["menu"]["products"]) == 105
    assert data["printer"]["receipt"]["header"] == "КУХНЯ"
    assert data["shift"] is None
    [offline_cashier] = data["cashiers"]
    assert offline_cashier["id"] == cashier["id"]
    assert pos.pin_matches("1234", offline_cashier["pin_hash"])
    # Only the register's key gives this data.
    assert client.get("/api/pos/offline-data").status_code == 403
    assert client.get("/api/pos/offline-data", headers={"X-Device-Key": "wrong"}).status_code == 401


def test_offline_shift_and_sales_are_synced_once_with_their_time(client):
    cashier = add_cashier(client)
    opened = datetime.now(timezone.utc) - timedelta(hours=2)
    shift_operation = {"type": "open_shift", "id": str(uuid.uuid4()), "at": opened.isoformat(), "cashier_id": cashier["id"], "opening_cash": "100"}
    first = sale_operation(cashier["id"], opened + timedelta(minutes=5))
    second = sale_operation(cashier["id"], opened + timedelta(minutes=9), offline_number="О2", payment_method="card", amount_received=None)

    results = sync(client, [shift_operation, first, second])
    assert [result["status"] for result in results] == ["ok", "ok", "ok"]
    # Sending the same queue again (the answer was lost) changes nothing.
    assert [result["order_ref"] for result in sync(client, [shift_operation, first, second])[1:]] == [
        results[1]["order_ref"], results[2]["order_ref"],
    ]

    orders = client.get("/api/admin/report/orders?store_id=1&start_date=2000-01-01&end_date=2100-01-01", headers=ADMIN).json()
    assert orders["total"] == 2
    synced = {order["order_ref"]: order for order in orders["orders"]}[results[1]["order_ref"]]
    assert synced["created_at"].startswith((opened + timedelta(minutes=5)).isoformat()[:16])
    assert synced["offline_number"] == "О1"
    assert synced["total"] == 26 and synced["change_due"] == 4
    assert synced["cashier_name"] == "Алия"
    assert synced["kitchen_status"] is None
    assert synced["print_status"] == "printed"

    # The shift opened offline is the register's current shift and counts the sales.
    headers = login(client)
    report = client.get("/api/pos/shifts/current", headers=headers).json()
    assert report["opened_at"].startswith(opened.isoformat()[:16])
    assert report["sales_count"] == 2
    assert report["expected_cash"] == 126


def test_offline_sales_join_a_shift_opened_online(client):
    cashier = add_cashier(client)
    open_shift(client, login(client), "50")
    now = datetime.now(timezone.utc)
    shift_operation = {"type": "open_shift", "id": str(uuid.uuid4()), "at": now.isoformat(), "cashier_id": cashier["id"]}
    results = sync(client, [shift_operation, sale_operation(cashier["id"], now)])
    assert [result["status"] for result in results] == ["ok", "ok"]
    report = client.get("/api/pos/shifts/current", headers=login(client)).json()
    assert report["opening_cash"] == 50
    assert report["sales_count"] == 1


def test_sync_stops_at_a_failing_operation_and_keeps_the_order(client):
    cashier = add_cashier(client)
    now = datetime.now(timezone.utc)
    # No shift is open on the server and none was opened offline.
    results = sync(client, [sale_operation(cashier["id"], now), sale_operation(cashier["id"], now)])
    assert len(results) == 1
    assert results[0]["status"] == "error"
    assert "смены" in results[0]["error"]

    other_store_cashier = 999
    open_shift(client, login(client))
    results = sync(client, [sale_operation(other_store_cashier, now)])
    assert results[0]["status"] == "error"


def test_wrong_register_clock_is_replaced_by_server_time(client):
    cashier = add_cashier(client)
    open_shift(client, login(client))
    future = datetime.now(timezone.utc) + timedelta(days=3)
    [result] = sync(client, [sale_operation(cashier["id"], future)])
    orders = client.get("/api/admin/report/orders?store_id=1", headers=ADMIN).json()["orders"]
    order = next(order for order in orders if order["order_ref"] == result["order_ref"])
    assert datetime.fromisoformat(order["created_at"]) < datetime.now(timezone.utc) + timedelta(minutes=1)
