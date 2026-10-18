from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import queue_screen
from tests.conftest import QUEUE_KEY
from tests.test_pos import add_cashier, kiosk_order, login, open_shift, sell


@pytest.fixture()
def screen(client):
    return TestClient(queue_screen.queue_app, headers={"X-Device-Key": QUEUE_KEY})


@pytest.fixture()
def cashier(client):
    add_cashier(client)
    headers = login(client)
    open_shift(client, headers, "0")
    return headers


def set_status(client, headers, order_ref, status):
    response = client.post(f"/api/pos/orders/{order_ref}/kitchen", headers=headers, json={"status": status})
    assert response.status_code == 200, response.text
    return response.json()


def test_kiosk_order_is_preparing_until_the_cashier_marks_it_ready(client, screen, cashier):
    first = kiosk_order(client, [{"product_id": "4", "quantity": 1}])
    second = kiosk_order(client, [{"product_id": "36", "quantity": 1}])
    assert first["kitchen_status"] == "preparing"
    assert screen.get("/api/queue").json() == {
        "store_name": "Paprika",
        "preparing": [first["order_number"], second["order_number"]],
        "ready": [],
    }

    ready = set_status(client, cashier, first["order_ref"], "ready")
    assert ready["kitchen_status"] == "ready"
    assert ready["ready_at"]
    assert screen.get("/api/queue").json() == {
        "store_name": "Paprika",
        "preparing": [second["order_number"]],
        "ready": [first["order_number"]],
    }
    kitchen = client.get("/api/pos/orders?scope=kitchen", headers=cashier).json()
    assert [order["kitchen_status"] for order in kitchen] == ["ready", "preparing"]

    # A mistaken tap can be undone.
    set_status(client, cashier, first["order_ref"], "preparing")
    assert screen.get("/api/queue").json()["ready"] == []


def test_ready_orders_leave_the_screen_after_five_minutes(client, cashier):
    order = kiosk_order(client, [{"product_id": "4", "quantity": 1}])
    set_status(client, cashier, order["order_ref"], "ready")
    now = datetime.now(timezone.utc)
    assert queue_screen.queue_state(1, now + timedelta(minutes=4, seconds=50))["ready"] == [order["order_number"]]
    assert queue_screen.queue_state(1, now + timedelta(minutes=5, seconds=10)) == {"preparing": [], "ready": []}


def test_cancelled_orders_and_sales_without_kitchen_are_not_shown(client, screen, cashier):
    cancelled = kiosk_order(client, [{"product_id": "4", "quantity": 1}])
    assert client.post(f"/api/pos/orders/{cancelled['order_ref']}/cancel", headers=cashier).status_code == 200
    drink = sell(client, cashier, {"items": [{"product_id": "4", "quantity": 1}], "payment_method": "card", "send_to_kitchen": False})
    food = sell(client, cashier, {"items": [{"product_id": "36", "quantity": 1}], "payment_method": "card"})
    assert drink.json()["kitchen_status"] is None
    assert screen.get("/api/queue").json() == {
        "store_name": "Paprika",
        "preparing": [food.json()["order_number"]],
        "ready": [],
    }

    response = client.post(f"/api/pos/orders/{drink.json()['order_ref']}/kitchen", headers=cashier, json={"status": "ready"})
    assert response.status_code == 409
    response = client.post(f"/api/pos/orders/{cancelled['order_ref']}/kitchen", headers=cashier, json={"status": "ready"})
    assert response.status_code == 409


def test_kitchen_status_requires_a_cashier(client, screen):
    order = kiosk_order(client, [{"product_id": "4", "quantity": 1}])
    assert client.post(f"/api/pos/orders/{order['order_ref']}/kitchen", json={"status": "ready"}).status_code == 401
    # The queue screen app exposes only the page, the logo and order numbers.
    assert screen.get("/api/orders").status_code in (404, 405)
    assert screen.get("/api/admin/orders").status_code == 404


def test_queue_page_is_served(screen):
    page = screen.get("/")
    assert page.status_code == 200
    assert "Готовится" in page.text and "/assets/queue.js" in page.text
    assert screen.get("/assets/queue.js").status_code == 200
    assert screen.get("/assets/queue.css").status_code == 200
