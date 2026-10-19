import uuid
from contextlib import closing

from app import main
from tests.conftest import ADMIN, REGISTER


def create_store(client, name="Paprika Центр"):
    response = client.post("/api/admin/stores", headers=ADMIN, json={"name": name, "address": "ул. Рудаки 1"})
    assert response.status_code == 201, response.text
    return response.json()


def create_device(client, store_id, kind, name, headers=ADMIN):
    response = client.post(f"/api/admin/stores/{store_id}/devices", headers=headers, json={"kind": kind, "name": name})
    assert response.status_code == 201, response.text
    return response.json()


def add_cashier(client, store_id, name, pin, headers=ADMIN):
    response = client.post(
        f"/api/admin/stores/{store_id}/cashiers",
        headers=headers,
        json={"name": name, "pin": pin, "can_view_revenue": True},
    )
    assert response.status_code == 201, response.text
    return response.json()


def pos_login(client, register_key, pin):
    response = client.post("/api/pos/login", headers={"X-Device-Key": register_key}, json={"pin": pin})
    assert response.status_code == 200, response.text
    return {"X-POS-Token": response.json()["token"]}


def kiosk_order(client, kiosk_key, product_id="4", quantity=1):
    response = client.post(
        "/api/orders",
        headers={"X-Device-Key": kiosk_key, "Idempotency-Key": str(uuid.uuid4())},
        json={"service_type": "takeaway", "items": [{"product_id": product_id, "quantity": quantity}]},
    )
    assert response.status_code == 201, response.text
    return response.json()


def sell(client, headers, product_id="4", method="card"):
    response = client.post(
        "/api/pos/sales",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
        json={"items": [{"product_id": product_id, "quantity": 1}], "payment_method": method, "print_receipt": False},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_owner_logs_in_with_password(client):
    assert client.post("/api/admin/login", json={"login": "owner", "password": "wrong"}).status_code == 401
    response = client.post("/api/admin/login", json={"login": "owner", "password": "owner-password"})
    assert response.status_code == 200, response.text
    token = response.json()["token"]
    me = client.get("/api/admin/me", headers={"Authorization": f"Bearer {token}"}).json()
    assert me["user"]["role"] == "owner"
    assert [store["name"] for store in me["stores"]] == ["Paprika"]
    client.post("/api/admin/logout", headers={"Authorization": f"Bearer {token}"})
    assert client.get("/api/admin/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_stores_have_their_own_menu_cashiers_orders_and_numbers(client):
    store = create_store(client)
    kiosk = create_device(client, store["id"], "kiosk", "Киоск Центр")
    register = create_device(client, store["id"], "register", "Касса Центр")
    assert kiosk["key"] and register["key"]
    device = client.get("/api/device", headers={"X-Device-Key": kiosk["key"]}).json()
    assert device["store_name"] == "Paprika Центр"

    # Each store numbers its own orders from 1.
    first_store = kiosk_order(client, "kiosk-key-store-1")
    second_store = kiosk_order(client, kiosk["key"])
    assert first_store["order_number"] == second_store["order_number"] == "1"
    assert first_store["order_ref"] != second_store["order_ref"]

    # A product hidden in the new store stays in the first store's menu.
    product = main.catalog_for(store["id"]).product_by_id["4"]
    hidden = client.put(
        f"/api/admin/stores/{store['id']}/products/4",
        headers=ADMIN,
        json={"name": product.name, "category_id": product.category_id, "price": "20", "available": False},
    )
    assert hidden.status_code == 200
    second_menu = {p["id"]: p for p in client.get("/api/menu", headers={"X-Device-Key": kiosk["key"]}).json()["products"]}
    first_menu = {p["id"]: p for p in client.get("/api/menu").json()["products"]}
    assert second_menu["4"]["available"] is False and second_menu["4"]["price"] == 20
    assert first_menu["4"]["available"] is True and first_menu["4"]["price"] == 13
    rejected = client.post(
        "/api/orders",
        headers={"X-Device-Key": kiosk["key"], "Idempotency-Key": str(uuid.uuid4())},
        json={"service_type": "takeaway", "items": [{"product_id": "4", "quantity": 1}]},
    )
    assert rejected.status_code == 409

    # The same PIN can be used in two stores; a cashier logs in only at a register of their store.
    add_cashier(client, 1, "Алия", "1234")
    add_cashier(client, store["id"], "Бахром", "1234")
    first_cashier = pos_login(client, "register-key-store-1", "1234")
    second_cashier = pos_login(client, register["key"], "1234")
    assert client.get("/api/pos/session", headers=first_cashier).json()["cashier"]["name"] == "Алия"
    session = client.get("/api/pos/session", headers=second_cashier).json()
    assert session["cashier"]["name"] == "Бахром"
    assert session["store"]["name"] == "Paprika Центр"
    assert session["register"]["name"] == "Касса Центр"

    client.post("/api/pos/shifts/open", headers=second_cashier, json={"opening_cash": "0"})
    waiting = client.get("/api/pos/orders?scope=kiosk_unpaid", headers=second_cashier).json()
    assert [order["order_ref"] for order in waiting] == [second_store["order_ref"]]
    other_store_order = client.post(
        f"/api/pos/orders/{first_store['order_ref']}/pay",
        headers=second_cashier,
        json={"payment_method": "card", "print_receipt": False},
    )
    assert other_store_order.status_code == 404


def test_two_registers_of_a_store_have_separate_shifts(client):
    second = create_device(client, 1, "register", "Касса 2")
    add_cashier(client, 1, "Алия", "1234")
    add_cashier(client, 1, "Бахром", "5678")
    at_first = pos_login(client, "register-key-store-1", "1234")
    at_second = pos_login(client, second["key"], "5678")

    assert client.post("/api/pos/shifts/open", headers=at_first, json={"opening_cash": "100"}).status_code == 200
    assert client.get("/api/pos/session", headers=at_second).json()["shift"] is None
    assert client.post("/api/pos/shifts/open", headers=at_second, json={"opening_cash": "50"}).status_code == 200

    sell(client, at_first, method="cash")
    sell(client, at_second)
    sell(client, at_second)
    first_report = client.get("/api/pos/shifts/current", headers=at_first).json()
    second_report = client.get("/api/pos/shifts/current", headers=at_second).json()
    assert (first_report["sales_count"], second_report["sales_count"]) == (1, 2)
    assert first_report["register_name"] == "Тестовая касса"
    assert second_report["register_name"] == "Касса 2"

    closed = client.post("/api/pos/shifts/close", headers=at_second, json={"counted_cash": "50", "print_report": False})
    assert closed.status_code == 200
    assert client.get("/api/pos/session", headers=at_first).json()["shift"] is not None

    # Reports filter by register, shift and cashier.
    all_sales = client.get("/api/admin/report?store_id=1", headers=ADMIN).json()
    assert all_sales["orders_count"] == 3
    assert {entry["name"]: entry["orders"] for entry in all_sales["by_register"]} == {"Тестовая касса": 1, "Касса 2": 2}
    by_register = client.get(f"/api/admin/report?register_id={second['id']}", headers=ADMIN).json()
    assert by_register["orders_count"] == 2
    assert by_register["card_amount"] == 26
    by_shift = client.get(f"/api/admin/report?shift_id={first_report['shift_id']}", headers=ADMIN).json()
    assert by_shift["orders_count"] == 1
    assert by_shift["cash_amount"] == 13
    filters = client.get("/api/admin/report-filters?store_id=1", headers=ADMIN).json()
    bahrom = next(cashier for cashier in filters["cashiers"] if cashier["name"] == "Бахром")
    by_cashier = client.get(f"/api/admin/report?cashier_id={bahrom['id']}", headers=ADMIN).json()
    assert by_cashier["orders_count"] == 2
    assert {register["name"] for register in filters["registers"]} >= {"Тестовая касса", "Касса 2"}

    shifts = client.get(f"/api/admin/shifts?register_id={second['id']}", headers=ADMIN).json()
    assert len(shifts) == 1
    assert shifts[0]["open"] is False
    assert shifts[0]["sales_count"] == 2
    assert shifts[0]["register_name"] == "Касса 2"
    csv_text = client.get(f"/api/admin/report.csv?register_id={second['id']}", headers=ADMIN).text
    assert "Точка;Касса;Смена;Кассир" in csv_text
    assert ";Касса 2;" in csv_text


def test_manager_sees_only_granted_stores(client):
    other = create_store(client, "Paprika Север")
    manager = client.post(
        "/api/admin/users",
        headers=ADMIN,
        json={"login": "manager1", "name": "Управляющий", "password": "manager-pass", "store_ids": [other["id"]]},
    )
    assert manager.status_code == 201, manager.text
    assert manager.json()["store_ids"] == [other["id"]]
    token = client.post("/api/admin/login", json={"login": "manager1", "password": "manager-pass"}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    assert [store["id"] for store in client.get("/api/admin/stores", headers=headers).json()] == [other["id"]]
    assert client.get("/api/admin/stores/1/cashiers", headers=headers).status_code == 403
    assert client.get("/api/admin/stores/1/products", headers=headers).status_code == 403
    assert client.get("/api/admin/report?store_id=1", headers=headers).status_code == 403
    assert client.post("/api/admin/stores", headers=headers, json={"name": "Новая"}).status_code == 403
    assert client.get("/api/admin/users", headers=headers).status_code == 403

    # The manager runs their own store: devices, cashiers and reports.
    kiosk = create_device(client, other["id"], "kiosk", "Киоск Север", headers=headers)
    add_cashier(client, other["id"], "Дилшод", "4321", headers=headers)
    kiosk_order(client, "kiosk-key-store-1")
    kiosk_order(client, kiosk["key"])
    report = client.get("/api/admin/report", headers=headers).json()
    assert report["orders_count"] == 1
    assert [entry["name"] for entry in report["by_store"]] == ["Paprika Север"]

    # Access taken away by the owner works at once.
    client.put(f"/api/admin/users/{manager.json()['id']}", headers=ADMIN, json={"store_ids": []})
    assert client.get(f"/api/admin/stores/{other['id']}/cashiers", headers=headers).status_code == 403
    client.put(f"/api/admin/users/{manager.json()['id']}", headers=ADMIN, json={"active": False})
    assert client.get("/api/admin/me", headers=headers).status_code == 401


def test_device_keys_can_be_renewed_and_devices_disabled(client):
    kiosk = create_device(client, 1, "kiosk", "Киоск 2")
    headers = {"X-Device-Key": kiosk["key"]}
    assert client.get("/api/menu", headers=headers).status_code == 200
    # A register key does not work for the kiosk API and the other way round.
    assert client.get("/api/menu", headers=REGISTER).status_code == 403
    assert client.post("/api/pos/login", headers=headers, json={"pin": "1234"}).status_code == 403

    renewed = client.post(f"/api/admin/stores/1/devices/{kiosk['id']}/key", headers=ADMIN).json()
    assert client.get("/api/menu", headers=headers).status_code == 401
    assert client.get("/api/menu", headers={"X-Device-Key": renewed["key"]}).status_code == 200

    client.put(f"/api/admin/stores/1/devices/{kiosk['id']}", headers=ADMIN, json={"active": False})
    assert client.get("/api/menu", headers={"X-Device-Key": renewed["key"]}).status_code == 401
    devices = client.get("/api/admin/stores/1/devices", headers=ADMIN).json()
    assert all("key" not in device and "key_hash" not in device for device in devices)

    # A disabled store stops all its devices.
    client.put("/api/admin/stores/1", headers=ADMIN, json={"active": False})
    assert client.get("/api/menu").status_code == 401


def test_owner_cannot_lock_themselves_out(client):
    me = client.get("/api/admin/me", headers=ADMIN).json()["user"]
    assert client.put(f"/api/admin/users/{me['id']}", headers=ADMIN, json={"active": False}).status_code == 409
    weak = client.post(
        "/api/admin/users", headers=ADMIN, json={"login": "m2", "name": "М", "password": "short"}
    )
    assert weak.status_code == 422
    duplicate = client.post(
        "/api/admin/users", headers=ADMIN, json={"login": "OWNER", "name": "М", "password": "long-enough"}
    )
    assert duplicate.status_code == 409


def test_old_single_store_database_is_moved_to_the_first_store(tmp_path, monkeypatch):
    import sqlite3

    database = tmp_path / "old.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT, idempotency_key TEXT NOT NULL UNIQUE,
                request_hash TEXT NOT NULL, order_number TEXT NOT NULL UNIQUE, total INTEGER NOT NULL,
                items_json TEXT NOT NULL, created_at TEXT NOT NULL, mode TEXT NOT NULL,
                print_status TEXT NOT NULL DEFAULT 'not_attempted'
            );
            INSERT INTO orders (idempotency_key, request_hash, order_number, total, items_json, created_at, mode)
            VALUES ('k1', 'h', 'P-00001', 13, '[]', '2026-10-01T10:00:00+00:00', 'local'),
                   ('k2', 'h', 'P-00002', 13, '[]', '2026-10-01T11:00:00+00:00', 'local');
            CREATE TABLE product_settings (
                product_id TEXT PRIMARY KEY, name TEXT NOT NULL, category_id TEXT NOT NULL,
                price TEXT NOT NULL, available INTEGER NOT NULL, image TEXT NOT NULL, unit TEXT NOT NULL,
                is_custom INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO product_settings VALUES ('4', 'Кола', 'c', '13', 0, '/x.jpg', 'шт.', 0);
            CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO app_settings VALUES ('printer', '{"host": "10.0.0.5"}');
            CREATE TABLE sync_outbox (id INTEGER PRIMARY KEY, kind TEXT, entity_id INTEGER);
            """
        )
    monkeypatch.setattr(main, "DB_PATH", database)
    main.initialize_db()
    main.initialize_db()
    with closing(main.connect_db()) as connection:
        assert [tuple(row) for row in connection.execute("SELECT id, name FROM stores")] == [(1, "Paprika")]
        assert [tuple(row) for row in connection.execute("SELECT store_id, store_seq FROM orders ORDER BY id")] == [
            (1, 1),
            (1, 2),
        ]
        assert connection.execute("SELECT store_id FROM product_settings").fetchone()[0] == 1
        assert connection.execute("SELECT key FROM app_settings WHERE key LIKE 'printer%'").fetchone()[0] == "printer:1"
        kinds = {row["kind"] for row in connection.execute("SELECT kind FROM devices")}
        assert kinds == {"kiosk", "register", "queue"}
        tables = {row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert "sync_outbox" not in tables


def test_report_lists_orders_with_details(client):
    add_cashier(client, 1, "Алия", "1234")
    cashier = pos_login(client, "register-key-store-1", "1234")
    client.post("/api/pos/shifts/open", headers=cashier, json={"opening_cash": "0"})
    unpaid = kiosk_order(client, "kiosk-key-store-1", product_id="36")
    cancelled = kiosk_order(client, "kiosk-key-store-1")
    client.post(f"/api/pos/orders/{cancelled['order_ref']}/cancel", headers=cashier)
    sale = sell(client, cashier, method="cash")

    listed = client.get("/api/admin/report/orders?store_id=1", headers=ADMIN).json()
    assert listed["total"] == 3
    by_ref = {order["order_ref"]: order for order in listed["orders"]}
    assert [order["order_ref"] for order in listed["orders"]][0] == sale["order_ref"]
    assert by_ref[cancelled["order_ref"]]["payment_status"] == "cancelled"
    assert by_ref[cancelled["order_ref"]]["cancelled_at"]
    detail = by_ref[sale["order_ref"]]
    assert detail["cashier_name"] == "Алия"
    assert detail["payment_method"] == "cash"
    assert detail["store_name"] == "Paprika"
    assert detail["items"][0]["product_id"] == "4"
    assert by_ref[unpaid["order_ref"]]["items"][0]["product_id"] == "36"
    # The totals still leave out the cancelled order.
    assert client.get("/api/admin/report?store_id=1", headers=ADMIN).json()["orders_count"] == 2

    page = client.get("/api/admin/report/orders?store_id=1&limit=2&offset=2", headers=ADMIN).json()
    assert [order["order_ref"] for order in page["orders"]] == [unpaid["order_ref"]]
    alia = client.get("/api/admin/report-filters?store_id=1", headers=ADMIN).json()["cashiers"][0]
    by_cashier = client.get(f"/api/admin/report/orders?cashier_id={alia['id']}", headers=ADMIN).json()
    assert [order["order_ref"] for order in by_cashier["orders"]] == [sale["order_ref"]]

    other = create_store(client, "Север")
    manager = client.post(
        "/api/admin/users", headers=ADMIN,
        json={"login": "manager2", "name": "М", "password": "manager-pass", "store_ids": [other["id"]]},
    )
    assert manager.status_code == 201
    token = client.post("/api/admin/login", json={"login": "manager2", "password": "manager-pass"}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/admin/report/orders?store_id=1", headers=headers).status_code == 403
    assert client.get("/api/admin/report/orders", headers=headers).json() == {"total": 0, "orders": []}
