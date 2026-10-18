import uuid

from app import main, pos
from tests.conftest import ADMIN, REGISTER, take_print_jobs


def use_printer(monkeypatch):
    monkeypatch.setattr(main, "PRINTER_HOST", "printer.example")


def add_cashier(client, name="Алия", pin="1234", can_manage_shift=True):
    response = client.post(
        "/api/admin/stores/1/cashiers",
        headers=ADMIN,
        json={"name": name, "pin": pin, "can_manage_shift": can_manage_shift},
    )
    assert response.status_code == 201, response.text
    return response.json()


def login(client, pin="1234"):
    response = client.post("/api/pos/login", headers=REGISTER, json={"pin": pin})
    assert response.status_code == 200, response.text
    return {"X-POS-Token": response.json()["token"]}


def open_shift(client, headers, opening_cash="100"):
    response = client.post("/api/pos/shifts/open", headers=headers, json={"opening_cash": opening_cash})
    assert response.status_code == 200, response.text
    return response.json()


def sell(client, headers, body):
    return client.post(
        "/api/pos/sales",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
        json={"print_receipt": False, **body},
    )


def kiosk_order(client, items):
    response = client.post(
        "/api/orders",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={"service_type": "takeaway", "items": items},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_cashiers_are_managed_by_staff_and_log_in_by_pin(client):
    assert client.get("/api/admin/stores/1/cashiers").status_code == 401
    cashier = add_cashier(client)
    assert cashier == {"id": cashier["id"], "store_id": 1, "name": "Алия", "active": True, "can_manage_shift": True}
    assert client.post("/api/admin/stores/1/cashiers", headers=ADMIN, json={"name": "Б", "pin": "1234"}).status_code == 409
    assert client.post("/api/admin/stores/1/cashiers", headers=ADMIN, json={"name": "Б", "pin": "12"}).status_code == 422

    assert client.post("/api/pos/login", headers=REGISTER, json={"pin": "9999"}).status_code == 401
    headers = login(client)
    session = client.get("/api/pos/session", headers=headers).json()
    assert session["cashier"]["name"] == "Алия"
    assert session["shift"] is None
    assert client.get("/api/pos/session").status_code == 401
    assert client.get("/api/pos/menu", headers=headers).status_code == 200

    disabled = client.put(f"/api/admin/stores/1/cashiers/{cashier['id']}", headers=ADMIN, json={"active": False})
    assert disabled.json()["active"] is False
    assert client.get("/api/pos/session", headers=headers).status_code == 401
    assert client.post("/api/pos/login", headers=REGISTER, json={"pin": "1234"}).status_code == 401


def test_too_many_wrong_pins_are_throttled(client):
    add_cashier(client)
    for _ in range(pos.LOGIN_MAX_FAILURES):
        assert client.post("/api/pos/login", headers=REGISTER, json={"pin": "0000"}).status_code == 401
    assert client.post("/api/pos/login", headers=REGISTER, json={"pin": "1234"}).status_code == 429


def test_sale_with_discount_cash_change_and_no_kiosk_limit(client, monkeypatch):
    add_cashier(client)
    headers = login(client)
    product = main.catalog_for(1).product_by_id["108"]  # 100 somoni pizza
    assert product.price == 100

    no_shift = sell(client, headers, {"items": [{"product_id": "108", "quantity": 1}], "payment_method": "card"})
    assert no_shift.status_code == 409
    open_shift(client, headers)

    use_printer(monkeypatch)
    big = sell(
        client,
        headers,
        {
            "items": [
                {"product_id": "108", "quantity": 12},
                {"product_id": "1", "quantity": 1, "portion_kind": "weight", "portion_value": 1000},
            ],
            "discount_type": "percent",
            "discount_value": "10",
            "payment_method": "cash",
            "amount_received": "1200",
            "service_type": "dine_in",
            "print_receipt": True,
        },
    )
    assert big.status_code == 201, big.text
    order = big.json()
    assert order["source"] == "pos"
    assert order["payment_status"] == "paid"
    assert order["subtotal"] == 1295
    assert order["discount"] == 129.5
    assert order["total"] == 1165.5
    assert order["change_due"] == 34.5
    assert order["cashier_name"] == "Алия"
    assert order["print_status"] == "queued"
    assert order["receipt_status"] == "queued"
    kitchen, receipt = take_print_jobs(client)[1]
    assert "КУХНЯ".encode("cp866") in kitchen
    for text in ("ТОВАРНЫЙ ЧЕК", "Кассир: Алия", "Скидка", "ИТОГО", "Сдача", "Нефискальный документ"):
        assert text.encode("cp866") in receipt
    assert "1165,5 с".encode("cp866") in receipt

    short = sell(
        client,
        headers,
        {"items": [{"product_id": "4", "quantity": 1}], "payment_method": "cash", "amount_received": "5"},
    )
    assert short.status_code == 422
    too_much_discount = sell(
        client,
        headers,
        {
            "items": [{"product_id": "4", "quantity": 1}],
            "payment_method": "card",
            "discount_type": "amount",
            "discount_value": "50",
        },
    )
    assert too_much_discount.status_code == 422


def test_sale_is_idempotent(client):
    add_cashier(client)
    headers = login(client)
    open_shift(client, headers)
    key = {"Idempotency-Key": str(uuid.uuid4())}
    body = {"items": [{"product_id": "4", "quantity": 2}], "payment_method": "card", "print_receipt": False}
    first = client.post("/api/pos/sales", headers={**headers, **key}, json=body)
    second = client.post("/api/pos/sales", headers={**headers, **key}, json=body)
    assert first.json()["order_ref"] == second.json()["order_ref"]
    assert len(client.get("/api/pos/orders", headers=headers).json()) == 1


def test_kiosk_orders_are_paid_or_cancelled_at_the_register(client):
    add_cashier(client)
    headers = login(client)
    open_shift(client, headers, "0")
    first = kiosk_order(client, [{"product_id": "4", "quantity": 1}])
    second = kiosk_order(client, [{"product_id": "36", "quantity": 1}])

    waiting = client.get("/api/pos/orders?scope=kiosk_unpaid", headers=headers).json()
    assert [order["order_ref"] for order in waiting] == [first["order_ref"], second["order_ref"]]

    paid = client.post(
        f"/api/pos/orders/{first['order_ref']}/pay",
        headers=headers,
        json={"payment_method": "cash", "amount_received": "20", "print_receipt": False},
    )
    assert paid.status_code == 200, paid.text
    assert paid.json()["payment_status"] == "paid"
    assert paid.json()["change_due"] == 7
    again = client.post(
        f"/api/pos/orders/{first['order_ref']}/pay", headers=headers, json={"payment_method": "card"}
    )
    assert again.status_code == 409

    cancelled = client.post(f"/api/pos/orders/{second['order_ref']}/cancel", headers=headers)
    assert cancelled.json()["payment_status"] == "cancelled"
    assert client.get("/api/pos/orders?scope=kiosk_unpaid", headers=headers).json() == []

    report = client.get("/api/admin/report", headers=ADMIN).json()
    assert report["orders_count"] == 1
    assert report["total_amount"] == 13


def test_refund_and_shift_reports(client, monkeypatch):
    add_cashier(client)
    headers = login(client)
    open_shift(client, headers, "100")
    cash = sell(client, headers, {"items": [{"product_id": "36", "quantity": 2}], "payment_method": "cash", "amount_received": "100"})
    card = sell(client, headers, {"items": [{"product_id": "4", "quantity": 1}], "payment_method": "card"})
    assert cash.status_code == card.status_code == 201

    refunded = client.post(f"/api/pos/orders/{card.json()['order_ref']}/refund", headers=headers)
    assert refunded.json()["payment_status"] == "refunded"
    assert client.post(f"/api/pos/orders/{card.json()['order_ref']}/refund", headers=headers).status_code == 409

    x_report = client.get("/api/pos/shifts/current", headers=headers).json()
    assert x_report["sales_count"] == 2
    assert x_report["sales_cash"] == 90
    assert x_report["sales_card"] == 13
    assert x_report["refunds_card"] == 13
    assert x_report["revenue"] == 90
    assert x_report["expected_cash"] == 190

    use_printer(monkeypatch)
    closed = client.post("/api/pos/shifts/close", headers=headers, json={"counted_cash": "185"})
    assert closed.status_code == 200
    z_report = closed.json()
    assert z_report["counted_cash"] == 185
    assert z_report["difference"] == -5
    assert z_report["print_status"] == "queued"
    assert "Z-ОТЧЁТ".encode("cp866") in take_print_jobs(client)[1][0]
    assert client.get("/api/pos/session", headers=headers).json()["shift"] is None
    assert client.get("/api/pos/shifts/current", headers=headers).status_code == 409

    admin_report = client.get("/api/admin/report", headers=ADMIN).json()
    assert admin_report["orders_count"] == 1
    assert admin_report["total_amount"] == 90


def test_receipt_reprint_requires_paid_order(client, monkeypatch):
    add_cashier(client)
    headers = login(client)
    open_shift(client, headers)
    unpaid = kiosk_order(client, [{"product_id": "4", "quantity": 1}])
    assert client.post(f"/api/pos/orders/{unpaid['order_ref']}/receipt", headers=headers).status_code == 409
    sale = sell(client, headers, {"items": [{"product_id": "4", "quantity": 1}], "payment_method": "card"})
    assert client.post(f"/api/pos/orders/{sale.json()['order_ref']}/receipt", headers=headers).status_code == 503
    use_printer(monkeypatch)
    reprinted = client.post(f"/api/pos/orders/{sale.json()['order_ref']}/receipt", headers=headers)
    assert reprinted.json() == {"receipt_status": "queued"}
    assert "Оплата картой".encode("cp866") in take_print_jobs(client)[1][0]


def test_pos_page_is_served(client):
    response = client.get("/pos")
    assert response.status_code == 200
    assert "pos.js" in response.text


def test_pages_and_scripts_are_revalidated_by_browsers(client):
    for path in ("/", "/pos", "/static/app.js", "/static/pos/pos.js", "/static/styles.css"):
        assert client.get(path).headers["cache-control"] == "no-cache", path


def test_receipts_tab_lists_only_the_latest_15(client):
    add_cashier(client)
    headers = login(client)
    open_shift(client, headers)
    refs = []
    for _ in range(17):
        sale = sell(client, headers, {"items": [{"product_id": "4", "quantity": 1}], "payment_method": "card"})
        refs.append(sale.json()["order_ref"])
    recent = client.get("/api/pos/orders?scope=recent", headers=headers).json()
    assert [order["order_ref"] for order in recent] == list(reversed(refs))[:15]


def test_shift_needs_access_granted_by_admin(client):
    manager = add_cashier(client, "Алия", "1234")
    cashier = add_cashier(client, "Бахром", "5678", can_manage_shift=False)
    assert cashier["can_manage_shift"] is False
    headers = login(client, "5678")
    assert client.get("/api/pos/session", headers=headers).json()["cashier"]["can_manage_shift"] is False

    denied = client.post("/api/pos/shifts/open", headers=headers, json={"opening_cash": "0"})
    assert denied.status_code == 403
    assert "доступа к смене" in denied.json()["detail"]

    # A cashier with access opens the shift; the other one can sell but not see or close the shift.
    open_shift(client, login(client, "1234"), "0")
    sale = sell(client, headers, {"items": [{"product_id": "4", "quantity": 1}], "payment_method": "card"})
    assert sale.status_code == 201, sale.text
    assert client.get("/api/pos/shifts/current", headers=headers).status_code == 403
    assert client.post("/api/pos/shifts/current/print", headers=headers).status_code == 403
    assert client.post("/api/pos/shifts/close", headers=headers, json={"counted_cash": "0"}).status_code == 403

    # Access granted by the admin works at once, without logging in again.
    granted = client.put(f"/api/admin/stores/1/cashiers/{cashier['id']}", headers=ADMIN, json={"can_manage_shift": True})
    assert granted.json()["can_manage_shift"] is True
    assert client.get("/api/pos/shifts/current", headers=headers).status_code == 200
    revoked = client.put(f"/api/admin/stores/1/cashiers/{manager['id']}", headers=ADMIN, json={"can_manage_shift": False})
    assert revoked.json()["can_manage_shift"] is False


def test_existing_cashiers_keep_shift_access_after_upgrade(tmp_path, monkeypatch):
    import sqlite3

    database = tmp_path / "old.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE cashiers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, "
            "pin_hash TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO cashiers (name, pin_hash, created_at) VALUES ('Старый', 'x', 'now')")
    monkeypatch.setattr(main, "DB_PATH", database)
    main.initialize_db()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT can_manage_shift FROM cashiers").fetchone()[0] == 1
