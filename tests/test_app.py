import uuid
from contextlib import closing

from app import main
from tests.conftest import take_print_jobs


def catalog():
    return main.catalog_for(1)


def test_menu_is_available_from_local_catalog(client):
    response = client.get("/api/menu")

    assert response.status_code == 200
    assert "mode" not in response.json()
    assert len(response.json()["products"]) == 105
    fractional_product = next(
        product for product in response.json()["products"] if product["price"] == 2.5
    )
    assert fractional_product["unit"] in {"шт.", "кг"}
    assert client.get("/assets/logo.jpg").status_code == 200
    assert client.get(fractional_product["image"]).headers["content-type"] == "image/jpeg"


def test_order_uses_server_prices_and_is_unpaid_and_idempotent(client):
    key = str(uuid.uuid4())
    fractional_product = next(product for product in catalog().products if product.price == 2.5)
    payload = {
        "service_type": "dine_in",
        "items": [
            {
                "product_id": fractional_product.id,
                "quantity": 2,
            }
        ]
    }
    first = client.post("/api/orders", headers={"Idempotency-Key": key}, json=payload)
    second = client.post("/api/orders", headers={"Idempotency-Key": key}, json=payload)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["order_number"] == second.json()["order_number"]
    assert first.json()["total"] == 5.0
    assert first.json()["items"][0]["unit"] == fractional_product.unit
    assert first.json()["payment_status"] == "unpaid"
    assert first.json()["order_number"] == "1"
    assert first.json()["order_ref"] == "P-00001"
    assert first.json()["print_status"] == "failed"


def test_existing_idempotency_hash_still_replays_without_portion_fields(client):
    key = str(uuid.uuid4())
    payload = {"service_type": "dine_in", "items": [{"product_id": "4", "quantity": 1}]}
    created = client.post(
        "/api/orders",
        headers={"Idempotency-Key": key},
        json=payload,
    )
    previous_payload = {
        "service_type": "dine_in",
        "items": [
            {
                "product_id": "4",
                "quantity": 1,
                "variant_id": None,
                "modifier_ids": [],
            }
        ]
    }
    previous_hash = main.hashlib.sha256(
        main.json.dumps(
            previous_payload, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    with closing(main.connect_db()) as connection:
        connection.execute(
            "UPDATE orders SET request_hash = ? WHERE idempotency_key = ?",
            (previous_hash, key),
        )
        connection.commit()

    replay = client.post(
        "/api/orders",
        headers={"Idempotency-Key": key},
        json={**payload, "items": [{**payload["items"][0], "portion_kind": None, "portion_value": None}]},
    )

    assert created.status_code == 201
    assert replay.status_code == 201
    assert replay.json()["order_number"] == created.json()["order_number"]


def order_portion(client, kind, value, product_id="1"):
    return client.post(
        "/api/orders",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={
            "service_type": "dine_in",
            "items": [
                {
                    "product_id": product_id,
                    "quantity": 1,
                    "portion_kind": kind,
                    "portion_value": value,
                }
            ]
        },
    )


def test_strips_and_wings_accept_any_weight_or_amount(client):
    for product_id in ("1", "2", "16", "90", "91"):
        product = catalog().product_by_id[product_id]
        assert product.price == 95
        assert product.weighted.min_grams == 200
        assert product.weighted.max_grams == 11000
        assert product.weighted.min_amount == 20
        assert product.weighted.max_amount == 300
        assert product.weighted.preset_grams == [200, 500, 1000, 1500]
        assert product.weighted.default_grams == 1000

    for kind, value, amount, weight in (
        # 0,2 kg costs the minimum 20 somoni rather than 19.
        ("weight", 200, 20, 200),
        ("weight", 210, 20, 210),
        ("weight", 220, 20.9, 220),
        ("weight", 350, 33.25, 350),
        ("weight", 3000, 285, 3000),
        # 11 kg (1045) is allowed, but the kiosk order limit is 1000 somoni.
        ("weight", 10000, 950, 10000),
        ("price", 20, 20, 211),
        ("price", 37, 37, 389),
        ("price", 300, 300, 3158),
    ):
        response = order_portion(client, kind, value)

        assert response.status_code == 201
        item = response.json()["items"][0]
        assert response.json()["total"] == amount
        assert item["unit"] == "порц."
        assert item["portion_kind"] == kind
        assert item["portion_amount"] == amount
        assert item["weight_grams"] == weight


def test_weighted_portion_is_required_and_limited(client):
    missing = client.post(
        "/api/orders",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={"service_type": "dine_in", "items": [{"product_id": "1", "quantity": 1}]},
    )

    assert missing.status_code == 422
    assert order_portion(client, "weight", 199).status_code == 422
    assert order_portion(client, "weight", 11001).status_code == 422
    assert order_portion(client, "price", 19).status_code == 422
    assert order_portion(client, "price", 301).status_code == 422
    assert order_portion(client, "price", 20, product_id="4").status_code == 422


def test_hot_drinks_other_and_ice_cream_are_not_in_menu(client):
    menu = client.get("/api/menu").json()
    category_names = {category["name"] for category in menu["categories"]}

    assert category_names.isdisjoint({"Горячие напитки", "Прочие", "Прочее", "Мороженое"})
    assert {product["category_id"] for product in menu["products"]} <= {
        category["id"] for category in menu["categories"]
    }
    for removed_id in ("11", "13", "26", "71", "77"):
        assert removed_id not in catalog().product_by_id


def test_existing_order_mode_is_migrated_to_local(client):
    created = client.post(
        "/api/orders",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={"service_type": "dine_in", "items": [{"product_id": "4", "quantity": 1}]},
    )
    with closing(main.connect_db()) as connection:
        connection.execute(
            "UPDATE orders SET mode = 'demo' WHERE order_number = ?",
            (created.json()["order_ref"],),
        )
        connection.commit()

    main.initialize_db()

    with closing(main.connect_db()) as connection:
        row = connection.execute(
            "SELECT mode FROM orders WHERE order_number = ?",
            (created.json()["order_ref"],),
        ).fetchone()
    public_orders = client.get(
        "/api/admin/stores/1/orders",
        headers={"Authorization": "Bearer test-staff-secret"},
    ).json()["orders"]

    assert row["mode"] == "local"
    assert "mode" not in public_orders[0]


def test_order_comment_is_limited_saved_and_printed(client, monkeypatch):
    monkeypatch.setattr(main, "PRINTER_HOST", "printer.example")
    comment = "Без лука"
    response = client.post(
        "/api/orders",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={"service_type": "dine_in", "items": [{"product_id": "4", "quantity": 1}], "comment": comment},
    )

    assert response.status_code == 201
    assert response.json()["comment"] == comment
    assert response.json()["print_status"] == "queued"
    _, sent = take_print_jobs(client)
    assert f"Комментарий: {comment}".encode("cp866") in sent[0]
    exported = client.get(
        "/api/admin/report.csv",
        headers={"Authorization": "Bearer test-staff-secret"},
    )
    assert exported.status_code == 200
    assert comment in exported.text

    too_long = client.post(
        "/api/orders",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={"service_type": "dine_in", "items": [{"product_id": "4", "quantity": 1}], "comment": "x" * 101},
    )
    assert too_long.status_code == 422


def test_admin_can_create_edit_and_upload_product_photo(client):
    headers = {"Authorization": "Bearer test-staff-secret"}
    created = client.post(
        "/api/admin/stores/1/products",
        headers=headers,
        json={
            "name": "Тестовое блюдо",
            "category_id": catalog().categories[0].id,
            "price": "12.50",
            "available": True,
            "unit": "шт.",
        },
    )

    assert created.status_code == 201
    product_id = created.json()["id"]
    edited = client.put(
        f"/api/admin/stores/1/products/{product_id}",
        headers=headers,
        json={
            "name": "Новое название",
            "category_id": catalog().categories[0].id,
            "price": "15.75",
            "available": False,
        },
    )
    assert edited.status_code == 200
    assert edited.json()["name"] == "Новое название"
    assert edited.json()["price"] == 15.75
    assert edited.json()["available"] is False

    uploaded = client.post(
        f"/api/admin/stores/1/products/{product_id}/image",
        headers=headers,
        files={"image": ("product.jpg", b"\xff\xd8\xffimage-data", "image/jpeg")},
    )
    assert uploaded.status_code == 200
    assert uploaded.json()["image"].startswith("/static/images/uploads/")
    image_name = uploaded.json()["image"].rsplit("/", 1)[-1]
    assert (main.IMAGE_UPLOAD_DIR / image_name).is_file()

    main.refresh_catalog()
    persisted = client.get("/api/admin/stores/1/products", headers=headers).json()
    saved = next(product for product in persisted["products"] if product["id"] == product_id)
    assert saved["name"] == "Новое название"
    assert saved["price"] == 15.75
    assert saved["available"] is False
    assert saved["image"] == uploaded.json()["image"]
    rejected = client.post(
        "/api/orders",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={"service_type": "dine_in", "items": [{"product_id": product_id, "quantity": 1}]},
    )
    assert rejected.status_code == 409


def test_report_preserves_fractional_prices_and_product_units(client):
    product = next(product for product in catalog().products if product.price == 2.5)
    created = client.post(
        "/api/orders",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={"service_type": "dine_in", "items": [{"product_id": product.id, "quantity": 3}]},
    )
    report = client.get(
        "/api/admin/report",
        headers={"Authorization": "Bearer test-staff-secret"},
    )

    assert created.status_code == 201
    assert report.status_code == 200
    assert report.json()["total_amount"] == 7.5
    assert report.json()["products"] == [
        {
            "product_id": product.id,
            "name": product.name,
            "unit": product.unit,
            "quantity": 3,
            "amount": 7.5,
        }
    ]


def test_weighted_portions_are_included_in_report_and_csv(client):
    created = client.post(
        "/api/orders",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={
            "service_type": "dine_in",
            "items": [
                {"product_id": "1", "quantity": 2, "portion_kind": "price", "portion_value": 50}
            ]
        },
    )
    headers = {"Authorization": "Bearer test-staff-secret"}
    report = client.get("/api/admin/report", headers=headers).json()
    csv_response = client.get("/api/admin/report.csv", headers=headers)

    assert created.status_code == 201
    assert report["products"] == [
        {
            "product_id": "1",
            "name": catalog().product_by_id["1"].name,
            "unit": "порц.",
            "quantity": 2,
            "amount": 100,
            "weight_grams": 1052,
        }
    ]
    assert "Сумма порции;Вес порции (кг)" in csv_response.text
    assert ";50.0;0.526;" in csv_response.text


def test_idempotency_key_cannot_be_reused_for_different_order(client):
    key = str(uuid.uuid4())
    headers = {"Idempotency-Key": key}
    first = client.post(
        "/api/orders",
        headers=headers,
        json={"service_type": "dine_in", "items": [{"product_id": "4", "quantity": 1}]},
    )
    changed = client.post(
        "/api/orders",
        headers=headers,
        json={"service_type": "dine_in", "items": [{"product_id": "4", "quantity": 2}]},
    )

    assert first.status_code == 201
    assert changed.status_code == 409


def test_admin_endpoints_require_admin_login(client):
    denied = client.get("/api/admin/stores")
    allowed = client.get(
        "/api/admin/stores",
        headers={"Authorization": "Bearer test-staff-secret"},
    )

    assert denied.status_code == 401
    assert allowed.status_code == 200
    assert allowed.json()[0]["name"] == "Paprika"


def test_created_order_is_printed_once_as_kitchen_receipt(client, monkeypatch):
    monkeypatch.setattr(main, "PRINTER_HOST", "printer.example")
    key = str(uuid.uuid4())
    payload = {
        "service_type": "dine_in",
        "items": [
            {"product_id": "4", "quantity": 2},
            {"product_id": "1", "quantity": 1, "portion_kind": "weight", "portion_value": 500},
        ]
    }
    response = client.post("/api/orders", headers={"Idempotency-Key": key}, json=payload)
    retry = client.post("/api/orders", headers={"Idempotency-Key": key}, json=payload)

    assert response.status_code == 201
    assert response.json()["print_status"] == "queued"
    assert retry.json()["order_number"] == response.json()["order_number"]
    printer, sent = take_print_jobs(client)
    assert printer["host"] == "printer.example"
    assert len(sent) == 1
    assert take_print_jobs(client)[1] == []
    orders = client.get("/api/admin/stores/1/orders", headers={"Authorization": "Bearer test-staff-secret"})
    assert orders.json()["orders"][0]["print_status"] == "printed"
    data = sent[0]
    number = response.json()["order_number"].encode("ascii")
    assert data.startswith(b"\x1b@\x1bt\x11")
    assert b"\x1d!\x11" + number + b"\n" in data
    assert f"2 x {catalog().product_by_id['4'].name}".encode("cp866") in data
    assert "    0,5 кг".encode("cp866") in data
    assert "ИТОГО".encode("cp866") in data
    assert "КУХНЯ".encode("cp866") in data
    assert data.endswith(b"\x1bd\x03\x1dV\x01")


def test_staff_printer_settings_change_receipt_design(client, monkeypatch):
    headers = {"Authorization": "Bearer test-staff-secret"}
    settings = client.get("/api/admin/stores/1/printer", headers=headers).json()
    assert settings["host"] == ""
    assert settings["chars_per_line"] == 48

    settings["host"] = "192.168.1.77"
    settings["port"] = 9101
    settings["receipt"].update(
        header="ПИЦЦЕРИЯ",
        number_size=3,
        show_prices=True,
        show_total=False,
        footer="Приятного аппетита!",
        copies=2,
        cut=False,
    )
    saved = client.put("/api/admin/stores/1/printer", headers=headers, json=settings)
    assert saved.status_code == 200
    assert client.get("/api/admin/stores/1/printer", headers=headers).json() == settings

    created = client.post(
        "/api/orders",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={"service_type": "dine_in", "items": [{"product_id": "4", "quantity": 1}]},
    )

    assert created.json()["print_status"] == "queued"
    printer, sent = take_print_jobs(client)
    assert (printer["host"], printer["port"]) == ("192.168.1.77", 9101)
    data = sent[0]
    single = data[: len(data) // 2]
    assert data == single * 2
    assert "ПИЦЦЕРИЯ".encode("cp866") in single
    assert b"\x1d!\x22" + created.json()["order_number"].encode("ascii") in single
    assert f"{catalog().product_by_id['4'].price:g} с".encode("cp866") in single
    assert "ИТОГО".encode("cp866") not in single
    assert "Приятного аппетита!".encode("cp866") in single
    assert b"\x1dV" not in single


def test_printer_preview_check_and_test_print_use_unsaved_form(client, monkeypatch):
    headers = {"Authorization": "Bearer test-staff-secret"}
    settings = client.get("/api/admin/stores/1/printer", headers=headers).json()
    settings["chars_per_line"] = 42
    settings["receipt"]["header"] = "Кухня  №1"

    preview = client.post("/api/admin/stores/1/printer/preview", headers=headers, json=settings)
    assert preview.status_code == 200
    lines = preview.json()["lines"]
    assert preview.json()["chars_per_line"] == 42
    assert lines[0] == {"text": "Кухня №1", "align": "center", "bold": True, "width": 1, "height": 1}
    assert all(len(line["text"]) * line["width"] <= 42 for line in lines)

    not_configured = client.post("/api/admin/stores/1/printer/test", headers=headers, json=settings)
    assert not_configured.status_code == 503

    settings["host"] = "printer.local"
    printed = client.post("/api/admin/stores/1/printer/test", headers=headers, json=settings)
    assert printed.status_code == 200
    status = client.get("/api/admin/stores/1/printer/status", headers=headers).json()
    assert status["jobs"][0]["status"] == "queued"

    _, sent = take_print_jobs(client)
    assert b"\x1d!\x1112\n" in sent[0]
    assert client.get("/api/admin/stores/1/printer", headers=headers).json()["host"] == ""
    status = client.get("/api/admin/stores/1/printer/status", headers=headers).json()
    assert status["jobs"][0]["status"] == "printed"
    assert any(agent["online"] and agent["name"] == "Тестовая касса" for agent in status["agents"])


def test_printer_settings_are_validated_and_protected(client):
    headers = {"Authorization": "Bearer test-staff-secret"}
    settings = client.get("/api/admin/stores/1/printer", headers=headers).json()

    assert client.get("/api/admin/stores/1/printer").status_code == 401
    assert client.put("/api/admin/stores/1/printer", json=settings).status_code == 401
    for change in (
        {"host": "bad host; rm"},
        {"port": 0},
        {"encoding": "no-such-codec"},
        {"chars_per_line": 50},
    ):
        response = client.put("/api/admin/stores/1/printer", headers=headers, json={**settings, **change})
        assert response.status_code == 422, change
    bad_receipt = {**settings, "receipt": {**settings["receipt"], "number_size": 5}}
    assert client.put("/api/admin/stores/1/printer", headers=headers, json=bad_receipt).status_code == 422


def test_admin_report_summarizes_order_quantities_and_exports_csv(client):
    headers = {"Authorization": "Bearer test-staff-secret"}
    for product_id, quantity in (("4", 2), ("3", 1)):
        response = client.post(
            "/api/orders",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json={"service_type": "dine_in", "items": [{"product_id": product_id, "quantity": quantity}]},
        )
        assert response.status_code == 201

    with closing(main.connect_db()) as connection:
        connection.execute(
            "UPDATE orders SET created_at = ? WHERE order_number = 'P-00001'",
            ("2026-10-17T10:00:00+00:00",),
        )
        connection.execute(
            "UPDATE orders SET created_at = ? WHERE order_number = 'P-00002'",
            ("2026-10-16T10:00:00+00:00",),
        )
        connection.commit()

    response = client.get(
        "/api/admin/report?start_date=2026-10-17&end_date=2026-10-17",
        headers=headers,
    )
    report = response.json()
    csv_response = client.get(
        "/api/admin/report.csv?start_date=2026-10-17&end_date=2026-10-17",
        headers=headers,
    )

    assert response.status_code == 200
    assert report["orders_count"] == 1
    assert report["items_count"] == 2
    assert report["total_amount"] == 26
    assert report["products"] == [
        {
            "product_id": "4",
            "name": catalog().product_by_id["4"].name,
            "unit": catalog().product_by_id["4"].unit,
            "quantity": 2,
            "amount": 26,
        }
    ]
    assert "mode" not in report
    assert csv_response.status_code == 200
    assert "Количество заказов;1" in csv_response.text
    assert catalog().product_by_id["4"].name in csv_response.text
    assert "Режим" not in csv_response.text


def test_staff_can_reprint_existing_local_order_without_creating_an_order(client, monkeypatch):
    headers = {"Authorization": "Bearer test-staff-secret"}
    created = client.post(
        "/api/orders",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={
            "service_type": "dine_in",
            "items": [{"product_id": "4", "quantity": 1}],
            "comment": "Без сахара",
        },
    )
    monkeypatch.setattr(main, "PRINTER_HOST", "printer.example")
    printed = client.post(
        "/api/admin/stores/1/print",
        headers=headers,
        json={"order_ref": created.json()["order_ref"]},
    )
    assert client.get("/api/admin/stores/1/orders", headers=headers).json()["orders"][0]["print_status"] == "queued"
    _, sent = take_print_jobs(client)
    orders = client.get("/api/admin/stores/1/orders", headers=headers).json()["orders"]

    assert printed.status_code == 200
    assert orders[0]["print_status"] == "printed"
    assert len(orders) == 1
    assert len(sent) == 1
    assert "Без сахара".encode("cp866") in sent[0]


def test_admin_report_rejects_invalid_period_and_requires_auth(client):
    unauthorized = client.get("/api/admin/report")
    invalid_period = client.get(
        "/api/admin/report?start_date=2026-10-18&end_date=2026-10-17",
        headers={"Authorization": "Bearer test-staff-secret"},
    )

    assert unauthorized.status_code == 401
    assert invalid_period.status_code == 422


def test_pizzas_and_burgers_have_ingredients_editable_by_staff(client):
    menu = client.get("/api/menu").json()
    category_names = {category["id"]: category["name"] for category in menu["categories"]}
    for product in menu["products"]:
        name = product["name"].casefold()
        if category_names[product["category_id"]] == "Пиццы" or "бургер" in name:
            assert product["ingredients"], product["name"]
    assert catalog().product_by_id["36"].ingredients == "Томатный соус, моцарелла, пепперони"
    assert catalog().product_by_id["4"].ingredients == ""

    headers = {"Authorization": "Bearer test-staff-secret"}
    product = catalog().product_by_id["36"]
    payload = {
        "name": product.name,
        "category_id": product.category_id,
        "price": "45",
        "available": True,
    }
    unchanged = client.put("/api/admin/stores/1/products/36", headers=headers, json=payload)
    custom = client.put(
        "/api/admin/stores/1/products/36",
        headers=headers,
        json={**payload, "ingredients": "  Томатный соус, моцарелла, острая пепперони  "},
    )

    assert unchanged.status_code == 200
    assert unchanged.json()["ingredients"] == "Томатный соус, моцарелла, пепперони"
    assert custom.status_code == 200
    main.refresh_catalog()
    assert catalog().product_by_id["36"].ingredients == "Томатный соус, моцарелла, острая пепперони"

    cleared = client.put(
        "/api/admin/stores/1/products/36", headers=headers, json={**payload, "ingredients": ""}
    )
    assert cleared.status_code == 200
    main.refresh_catalog()
    assert catalog().product_by_id["36"].ingredients == ""


def test_staff_priority_orders_products_in_menu(client):
    headers = {"Authorization": "Bearer test-staff-secret"}
    original_ids = [product["id"] for product in client.get("/api/menu").json()["products"]]

    def set_priority(product_id, priority):
        product = catalog().product_by_id[product_id]
        return client.put(
            f"/api/admin/stores/1/products/{product_id}",
            headers=headers,
            json={
                "name": product.name,
                "category_id": product.category_id,
                "price": str(product.price),
                "available": product.available,
                "priority": priority,
            },
        )

    assert set_priority("36", 2).status_code == 200
    assert set_priority("84", 1).status_code == 200
    assert set_priority("4", 2).status_code == 200

    menu_ids = [product["id"] for product in client.get("/api/menu").json()["products"]]
    rest = [product_id for product_id in original_ids if product_id not in {"84", "4", "36"}]
    # Equal priorities keep the workbook order ("4" comes before "36" in tovar.xlsx).
    assert menu_ids == ["84", "4", "36", *rest]
    admin_ids = [
        product["id"]
        for product in client.get("/api/admin/stores/1/products", headers=headers).json()["products"]
    ]
    assert admin_ids == menu_ids

    main.refresh_catalog()
    assert catalog().product_by_id["84"].priority == 1

    product = catalog().product_by_id["84"]
    without_priority_field = client.put(
        "/api/admin/stores/1/products/84",
        headers=headers,
        json={
            "name": product.name,
            "category_id": product.category_id,
            "price": str(product.price),
            "available": True,
        },
    )
    assert without_priority_field.json()["priority"] == 1
    assert set_priority("84", None).status_code == 200
    assert catalog().product_by_id["84"].priority is None
    assert set_priority("4", 0).status_code == 422
    assert set_priority("4", 1000).status_code == 422


def test_orders_above_1000_somoni_must_go_to_cash_desk(client):
    assert client.get("/api/menu").json()["max_order_total"] == 1000
    assert catalog().product_by_id["108"].price == 100

    def order(items):
        return client.post(
            "/api/orders",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json={"service_type": "dine_in", "items": items},
        )

    exactly_limit = order([{"product_id": "108", "quantity": 10}])
    over_limit = order(
        [{"product_id": "108", "quantity": 10}, {"product_id": "4", "quantity": 1}]
    )
    weighted_over_limit = order(
        [
            {"product_id": "1", "quantity": 4, "portion_kind": "weight", "portion_value": 3000},
        ]
    )

    assert exactly_limit.status_code == 201
    assert exactly_limit.json()["total"] == 1000
    assert over_limit.status_code == 422
    assert "через кассу" in over_limit.json()["detail"]
    assert weighted_over_limit.status_code == 422
    headers = {"Authorization": "Bearer test-staff-secret"}
    assert len(client.get("/api/admin/stores/1/orders", headers=headers).json()["orders"]) == 1


def test_customer_must_choose_dine_in_or_takeaway(client, monkeypatch):
    headers = {"Authorization": "Bearer test-staff-secret"}

    def order(service_type=None):
        body = {"items": [{"product_id": "4", "quantity": 1}]}
        if service_type is not None:
            body["service_type"] = service_type
        return client.post(
            "/api/orders", headers={"Idempotency-Key": str(uuid.uuid4())}, json=body
        )

    missing = order()
    invalid = order("delivery")
    assert missing.status_code == 422
    assert "с собой" in missing.json()["detail"]
    assert invalid.status_code == 422

    monkeypatch.setattr(main, "PRINTER_HOST", "printer.example")
    takeaway = order("takeaway")
    dine_in = order("dine_in")
    _, sent = take_print_jobs(client)

    assert takeaway.status_code == 201
    assert takeaway.json()["service_type"] == "takeaway"
    assert dine_in.json()["service_type"] == "dine_in"
    assert b"\x1d!\x11" + "С СОБОЙ".encode("cp866") in sent[0]
    assert b"\x1d!\x11" + "В ЗАЛЕ".encode("cp866") in sent[1]

    orders = client.get("/api/admin/stores/1/orders", headers=headers).json()["orders"]
    assert [entry["service_type"] for entry in orders] == ["dine_in", "takeaway"]
    exported = client.get("/api/admin/report.csv", headers=headers).text
    assert "В зале / с собой" in exported
    assert ";С собой;" in exported and ";В зале;" in exported

    settings = client.get("/api/admin/stores/1/printer", headers=headers).json()
    settings["receipt"]["show_service_type"] = False
    preview = client.post("/api/admin/stores/1/printer/preview", headers=headers, json=settings)
    assert all(line["text"] != "С СОБОЙ" for line in preview.json()["lines"])


def test_tajik_letters_in_comment_print_as_closest_russian(client, monkeypatch):
    monkeypatch.setattr(main, "PRINTER_HOST", "printer.example")
    response = client.post(
        "/api/orders",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={
            "service_type": "takeaway",
            "items": [{"product_id": "4", "quantity": 1}],
            "comment": "Қаҳва ҷудо, ғӯшт нагӣ",
        },
    )

    assert response.status_code == 201
    assert response.json()["comment"] == "Қаҳва ҷудо, ғӯшт нагӣ"
    _, sent = take_print_jobs(client)
    assert "Комментарий: Кахва чудо, гушт наги".encode("cp866") in sent[0]
    assert b"?" not in sent[0]


def test_order_numbers_run_from_1_to_30_and_restart(client, monkeypatch):
    monkeypatch.setattr(main, "PRINTER_HOST", "printer.example")
    numbers, refs, sent = [], [], []
    for _ in range(32):
        response = client.post(
            "/api/orders",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json={"service_type": "dine_in", "items": [{"product_id": "4", "quantity": 1}]},
        )
        assert response.status_code == 201
        numbers.append(response.json()["order_number"])
        refs.append(response.json()["order_ref"])
        sent.extend(take_print_jobs(client)[1])

    assert numbers == [str(number) for number in range(1, 31)] + ["1", "2"]
    assert len(set(refs)) == 32
    assert refs[30] == "P-00031"
    assert b"\x1d!\x1130\n" in sent[29]
    assert b"\x1d!\x111\n" in sent[30]

    headers = {"Authorization": "Bearer test-staff-secret"}
    reprinted = client.post("/api/admin/stores/1/print", headers=headers, json={"order_ref": refs[0]})
    assert reprinted.json()["order_number"] == "1"
    assert reprinted.json()["order_ref"] == "P-00001"
    exported = client.get("/api/admin/report.csv", headers=headers).text
    assert ";1;P-00031;" in exported
    assert ";30;P-00030;" in exported
