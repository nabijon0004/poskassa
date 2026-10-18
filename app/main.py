from __future__ import annotations

import codecs
import csv
import hashlib
import hmac
import io
import json
import logging
import os
import re
import socket
import sqlite3
import textwrap
import uuid
from contextlib import asynccontextmanager, closing
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from posixpath import normpath
from typing import Annotated, Any, AsyncIterator, Literal
from unicodedata import normalize
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator


ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"
CATALOG_PATH = ROOT / "tovar.xlsx"
LOGO_PATH = ROOT / "logo.jpg"
DB_PATH = Path(os.getenv("KIOSK_DB_PATH", str(ROOT / "data" / "kiosk.sqlite3")))
ADMIN_TOKEN = os.getenv("KIOSK_ADMIN_TOKEN", "")
CURRENCY = os.getenv("KIOSK_CURRENCY", "с")
# Larger orders must be placed at the cash desk.
MAX_ORDER_TOTAL = Decimal(os.getenv("KIOSK_MAX_ORDER_TOTAL", "1000"))
# Customer-facing order numbers run 1..ORDER_NUMBER_MAX and then start again from 1.
ORDER_NUMBER_MAX = int(os.getenv("KIOSK_ORDER_NUMBER_MAX", "50"))
PRINTER_HOST = os.getenv("KIOSK_PRINTER_HOST", "").strip()
PRINTER_PORT = int(os.getenv("KIOSK_PRINTER_PORT", "9100"))
PRINTER_TIMEOUT = float(os.getenv("KIOSK_PRINTER_TIMEOUT", "3"))
PRINTER_CODEPAGE = int(os.getenv("KIOSK_PRINTER_CODEPAGE", "17"))
PRINTER_ENCODING = os.getenv("KIOSK_PRINTER_ENCODING", "cp866")
IMAGE_UPLOAD_DIR = STATIC_DIR / "images" / "uploads"
MAX_PRODUCT_IMAGE_SIZE = 5 * 1024 * 1024

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger("paprika-kiosk")


class Modifier(BaseModel):
    id: str
    name: str
    price: int = Field(ge=0)


class WeightedPricing(BaseModel):
    price_per_kg: float
    min_grams: int
    max_grams: int
    min_amount: int
    max_amount: int
    preset_grams: list[int]
    default_grams: int


class Product(BaseModel):
    id: str
    name: str
    category_id: str
    description: str
    price: float = Field(ge=0)
    available: bool = True
    image: str
    unit: str = "шт."
    ingredients: str = ""
    # 1 is shown first; products without a priority keep the workbook order after them.
    priority: int | None = Field(default=None, ge=1, le=999)
    variants: list[Modifier] = Field(default_factory=list)
    modifiers: list[Modifier] = Field(default_factory=list)
    weighted: WeightedPricing | None = None


class Category(BaseModel):
    id: str
    name: str


class MenuResponse(BaseModel):
    currency: str
    max_order_total: float
    categories: list[Category]
    products: list[Product]


class OrderLineInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: str = Field(min_length=1, max_length=100)
    quantity: int = Field(ge=1, le=20)
    variant_id: str | None = Field(default=None, max_length=100)
    modifier_ids: list[str] = Field(default_factory=list, max_length=20)
    portion_kind: Literal["weight", "price"] | None = None
    portion_value: int | None = Field(default=None, ge=1, le=100000)


class ProductUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    category_id: str = Field(min_length=1, max_length=100)
    price: Decimal = Field(ge=0, max_digits=10, decimal_places=2)
    available: bool
    ingredients: str | None = Field(default=None, max_length=300)
    priority: int | None = Field(default=None, ge=1, le=999)


class ProductCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    category_id: str = Field(min_length=1, max_length=100)
    price: Decimal = Field(ge=0, max_digits=10, decimal_places=2)
    available: bool = True
    unit: str = Field(default="шт.", pattern=r"^(шт\.|кг)$")
    ingredients: str | None = Field(default=None, max_length=300)
    priority: int | None = Field(default=None, ge=1, le=999)


class ReceiptSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    header: str = Field(default="КУХНЯ", max_length=48)
    number_size: int = Field(default=2, ge=1, le=4)
    show_time: bool = True
    show_items: bool = True
    items_large: bool = False
    show_prices: bool = False
    show_total: bool = True
    show_comment: bool = True
    show_service_type: bool = True
    footer: str = Field(default="", max_length=96)
    copies: int = Field(default=1, ge=1, le=3)
    feed_lines: int = Field(default=3, ge=0, le=10)
    cut: bool = True

    @field_validator("header", "footer")
    @classmethod
    def single_line(cls, value: str) -> str:
        return " ".join(value.split())


class PrinterSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = Field(default="", max_length=253, pattern=r"^[A-Za-z0-9.:-]*$")
    port: int = Field(default=9100, ge=1, le=65535)
    timeout: float = Field(default=3, ge=0.5, le=30)
    # 80 mm paper: 48 characters (576 dots) on most printers, 42 on 512-dot ones.
    chars_per_line: Literal[42, 48] = 48
    codepage: int = Field(default=17, ge=0, le=255)
    encoding: str = Field(default="cp866", min_length=1, max_length=20)
    receipt: ReceiptSettings = Field(default_factory=ReceiptSettings)

    @field_validator("host", "encoding")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("encoding")
    @classmethod
    def known_encoding(cls, value: str) -> str:
        try:
            codecs.lookup(value)
        except LookupError as exc:
            raise ValueError("Неизвестная кодировка.") from exc
        return value


class PrintInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The unique internal reference (P-00051), not the repeating short number.
    order_ref: str = Field(pattern=r"^[A-Z0-9-]{1,40}$")


SERVICE_TYPE_LABELS = {"dine_in": "В зале", "takeaway": "С собой"}


class OrderInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[OrderLineInput] = Field(min_length=1, max_length=50)
    comment: str = Field(default="", max_length=100)
    # Required for new orders; optional in the model so the error message is clear.
    service_type: Literal["dine_in", "takeaway"] | None = None

    @field_validator("comment")
    @classmethod
    def normalize_comment(cls, value: str) -> str:
        return "".join(" " if ord(char) < 32 or ord(char) == 127 else char for char in value).strip()


CATEGORIES: list[Category] = []
PRODUCTS: list[Product] = []
PRODUCT_BY_ID: dict[str, Product] = {}
VARIANT_BY_ID: dict[str, dict[str, Modifier]] = {}
MODIFIER_BY_ID: dict[str, dict[str, Modifier]] = {}
CATALOG_ERROR: str | None = None

WEIGHTED_PRICE_PER_KG = Decimal("95")
WEIGHTED_MIN_GRAMS = 200
WEIGHTED_MAX_GRAMS = 3000
WEIGHTED_MIN_AMOUNT = 20
WEIGHTED_MAX_AMOUNT = 300
# Quick-choice weights in the product window; the customer can also enter any amount.
WEIGHTED_PRESET_GRAMS = [500, 1000, 1500, 2000]
WEIGHTED_DEFAULT_GRAMS = 1000
STRIP_AND_WING_TERMS = ("стрипс", "крылыш")
EXCLUDED_CATEGORIES = {"горячие напитки", "прочие", "прочее", "мороженое"}


def price_for_grams(grams: int) -> Decimal:
    return (Decimal(grams) * WEIGHTED_PRICE_PER_KG / Decimal("1000")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def grams_for_amount(amount: int) -> int:
    return int(
        (Decimal(amount) / WEIGHTED_PRICE_PER_KG * Decimal("1000")).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )


def weighted_pricing(name: str, unit: str) -> WeightedPricing | None:
    normalized_name = normalize("NFKC", name).casefold()
    has_fixed_weight = re.search(r"\d+\s*(?:г|гр|грамм)", normalized_name) is not None
    if (
        unit != "кг"
        or has_fixed_weight
        or not any(term in normalized_name for term in STRIP_AND_WING_TERMS)
    ):
        return None
    return WeightedPricing(
        price_per_kg=float(WEIGHTED_PRICE_PER_KG),
        min_grams=WEIGHTED_MIN_GRAMS,
        max_grams=WEIGHTED_MAX_GRAMS,
        min_amount=WEIGHTED_MIN_AMOUNT,
        max_amount=WEIGHTED_MAX_AMOUNT,
        preset_grams=WEIGHTED_PRESET_GRAMS,
        default_grams=WEIGHTED_DEFAULT_GRAMS,
    )


XLSX_NS = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
           "relationships": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
           "package": "http://schemas.openxmlformats.org/package/2006/relationships"}


def _excel_column_index(cell_reference: str) -> int:
    column = 0
    for char in cell_reference:
        if not char.isalpha():
            break
        column = column * 26 + ord(char.upper()) - ord("A") + 1
    if column == 0:
        raise ValueError(f"Некорректный адрес ячейки {cell_reference!r}.")
    return column - 1


def _excel_cell_value(cell: ElementTree.Element, shared_strings: list[str]) -> str:
    value = cell.find("main:v", XLSX_NS)
    if cell.attrib.get("t") == "inlineStr":
        return "".join(
            text.text or ""
            for text in cell.findall(".//main:t", XLSX_NS)
        ).strip()
    if value is None or value.text is None:
        return ""
    if cell.attrib.get("t") == "s":
        return shared_strings[int(value.text)]
    return value.text.strip()


# Ordered (keywords, photo) rules: every keyword must occur in the product name.
# More specific rules come first.
PRODUCT_PHOTO_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("мясной пир",), "pizza-meat-feast"),
    (("мясное ассорти",), "pizza-meat-assorted"),
    (("пицц", "мясн"), "pizza-meat"),
    (("пицц", "ассорти"), "pizza-supreme"),
    (("пицц", "кур"), "pizza-chicken"),
    (("пепперони",), "pizza-pepperoni"),
    (("салями",), "pizza-salami"),
    (("4-сыра",), "pizza-four-cheese"),
    (("каприз",), "pizza-capricciosa"),
    (("фирменн",), "pizza-house"),
    (("барбекю",), "pizza-bbq"),
    (("маргарит",), "pizza-margherita"),
    (("гавай",), "pizza-hawaiian"),
    (("пицц", "цезарь"), "pizza-caesar"),
    (("4-сезона",), "pizza-four-seasons"),
    (("стрипс", "остр"), "strips-spicy"),
    (("стрипс",), "strips"),
    (("крылыш", "остр"), "wings-hot"),
    (("крылыш",), "wings"),
    (("наггетс",), "nuggets"),
    (("соус", "чесно"), "sauce-garlic"),
    (("соус", "томат"), "ketchup"),
    (("соус", "сыр"), "sauce-cheese"),
    (("oca cola",), "coca-cola"),
    (("разлив",), "soda-cup"),
    (("pepsi",), "pepsi"),
    (("fanta",), "fanta"),
    (("rc ",), "rc-cola"),
    (("детск",), "kids-juice"),
    (("сок",), "juice"),
    (("sok",), "juice"),
    (("добрый",), "juice"),
    (("сочная долина",), "juice"),
    (("чай",), "iced-tea"),
    (("бонаква",), "water"),
    (("вода",), "water"),
    (("мохито",), "mojito"),
    (("чизбургер",), "cheeseburger"),
    (("делюкс",), "burger-deluxe"),
    (("шефбургер", "курин"), "burger-chef-chicken"),
    (("шефбургер",), "burger-chef"),
    (("чикенбургер",), "burger-chicken"),
    (("гамбургер", "курин"), "burger-chicken"),
    (("гамбургер",), "burger-beef"),
    (("твистер", "куриц"), "wrap-chicken"),
    (("твистер",), "wrap-beef"),
    (("кесадиль",), "quesadilla"),
    (("по дерев",), "wedges"),
    (("фри",), "fries"),
)
PRODUCT_PHOTO_DIR = STATIC_DIR / "images" / "products"

# Typical compositions shown until staff set the cafe's own text in the panel.
PRODUCT_INGREDIENT_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("мясной пир",), "Томатный соус, моцарелла, говядина, куриное филе, ветчина, пепперони, колбаски"),
    (("мясное ассорти",), "Томатный соус, моцарелла, ветчина, колбаса, пепперони, куриное филе"),
    (("пицц", "мясн"), "Томатный соус, моцарелла, говяжий фарш, колбаски, лук, болгарский перец"),
    (("пицц", "ассорти"), "Томатный соус, моцарелла, колбаса, ветчина, шампиньоны, болгарский перец, лук, маслины"),
    (("пицц", "кур"), "Томатный соус, моцарелла, куриное филе, шампиньоны, помидоры"),
    (("пепперони",), "Томатный соус, моцарелла, пепперони"),
    (("салями",), "Томатный соус, моцарелла, салями"),
    (("4-сыра",), "Сливочный соус, моцарелла, чеддер, пармезан, дор блю"),
    (("каприз",), "Томатный соус, моцарелла, ветчина, шампиньоны, помидоры, маслины"),
    (("фирменн",), "Томатный соус, моцарелла, куриное филе, ветчина, шампиньоны, болгарский перец, помидоры"),
    (("барбекю",), "Соус барбекю, моцарелла, куриное филе, лук, болгарский перец"),
    (("маргарит",), "Томатный соус, моцарелла, помидоры, базилик"),
    (("гавай",), "Томатный соус, моцарелла, ветчина, ананасы"),
    (("пицц", "цезарь"), "Соус цезарь, моцарелла, куриное филе, салат, помидоры черри, пармезан"),
    (("4-сезона",), "Томатный соус, моцарелла, ветчина, шампиньоны, пепперони, помидоры, маслины"),
    (("чизбургер",), "Булочка, говяжья котлета, сыр чеддер, лук, маринованные огурцы, кетчуп, горчица"),
    (("делюкс",), "Булочка, говяжья котлета, сыр чеддер, салат, помидор, лук, маринованные огурцы, фирменный соус"),
    (("шефбургер", "курин"), "Булочка, куриное филе в хрустящей панировке, салат, помидор, острый соус"),
    (("шефбургер",), "Булочка, говяжья котлета, салат, помидор, лук, острый соус"),
    (("чикенбургер",), "Булочка, куриная котлета в панировке, салат, майонезный соус"),
    (("гамбургер", "курин"), "Булочка, куриная котлета, салат, помидор, маринованные огурцы, соус"),
    (("гамбургер",), "Булочка, говяжья котлета, лук, маринованные огурцы, кетчуп, горчица"),
    (("твистер", "куриц"), "Пшеничная лепёшка, куриное филе в панировке, салат, помидор, соус"),
    (("твистер",), "Пшеничная лепёшка, говядина, салат, помидор, лук, соус"),
    (("кесадиль", "кур"), "Пшеничная лепёшка, куриное филе, сыр, болгарский перец, помидоры, соус"),
    (("кесадиль",), "Пшеничная лепёшка, говядина, сыр, болгарский перец, помидоры, соус"),
)


def default_ingredients(name: str) -> str:
    name_text = normalize("NFKC", f"{name} ").casefold()
    for keywords, ingredients in PRODUCT_INGREDIENT_RULES:
        if all(keyword in name_text for keyword in keywords):
            return ingredients
    return ""


def _image_for_product(name: str, category: str) -> str:
    name_text = normalize("NFKC", f"{name} ").casefold()
    for keywords, photo in PRODUCT_PHOTO_RULES:
        if all(keyword in name_text for keyword in keywords):
            if (PRODUCT_PHOTO_DIR / f"{photo}.jpg").is_file():
                return f"/static/images/products/{photo}.jpg"
            break
    text = normalize("NFKC", f"{category} {name}").casefold()
    if "пицц" in text:
        image = "pizza.jpg"
    elif "бургер" in text or "булоч" in text:
        image = "burger.jpg"
    elif "курин" in text or "стрипс" in text or "крылыш" in text:
        image = "chicken.jpg"
    elif "соус" in text:
        image = "fries.jpg"
    elif "морож" in text or "коктейл" in text or "десерт" in text:
        image = "ice-cream.jpg"
    elif any(
        word in text
        for word in ("кофе", "капучино", "латте", "эспрессо", "горячие напитки")
    ):
        image = "coffee.jpg"
    elif any(
        word in text
        for word in ("напит", "cola", "pepsi", "fanta", "сок", "чай", "вода", "стакан")
    ):
        image = "drink.jpg"
    else:
        image = "fries.jpg"
    return f"/static/images/{image}"


def load_catalog(path: Path = CATALOG_PATH) -> tuple[list[Category], list[Product]]:
    if not path.is_file():
        raise FileNotFoundError(f"Файл меню не найден: {path.name}.")

    try:
        with ZipFile(path) as workbook:
            names = workbook.namelist()
            if "xl/workbook.xml" not in names:
                raise ValueError("В файле не найден лист книги Excel.")
            if "xl/sharedStrings.xml" in names:
                shared_root = ElementTree.fromstring(workbook.read("xl/sharedStrings.xml"))
                shared_strings = [
                    "".join(text.text or "" for text in item.findall(".//main:t", XLSX_NS))
                    for item in shared_root.findall("main:si", XLSX_NS)
                ]
            else:
                shared_strings = []

            book_root = ElementTree.fromstring(workbook.read("xl/workbook.xml"))
            first_sheet = book_root.find("main:sheets/main:sheet", XLSX_NS)
            if first_sheet is None:
                raise ValueError("В книге Excel нет листов.")
            sheet_relation_id = first_sheet.attrib.get(
                f"{{{XLSX_NS['relationships']}}}id"
            )
            relationships_root = ElementTree.fromstring(
                workbook.read("xl/_rels/workbook.xml.rels")
            )
            relationship = next(
                (
                    relation
                    for relation in relationships_root.findall(
                        "package:Relationship", XLSX_NS
                    )
                    if relation.attrib.get("Id") == sheet_relation_id
                ),
                None,
            )
            if relationship is None:
                raise ValueError("Не удалось найти первый лист книги Excel.")
            sheet_target = relationship.attrib["Target"].replace("\\", "/")
            sheet_path = (
                normpath(sheet_target.lstrip("/"))
                if sheet_target.startswith("/")
                else normpath(f"xl/{sheet_target}")
            )
            if sheet_path not in names:
                raise ValueError("Файл первого листа книги Excel не найден.")
            sheet_root = ElementTree.fromstring(workbook.read(sheet_path))
    except (BadZipFile, ElementTree.ParseError, KeyError, IndexError) as exc:
        raise ValueError("Файл tovar.xlsx повреждён или имеет неподдерживаемый формат.") from exc

    sheet_rows = sheet_root.findall(".//main:sheetData/main:row", XLSX_NS)
    if not sheet_rows:
        raise ValueError("В файле tovar.xlsx нет строк меню.")

    parsed_rows: list[list[str]] = []
    for row in sheet_rows:
        cells: dict[int, str] = {}
        for cell in row.findall("main:c", XLSX_NS):
            cell_ref = cell.attrib.get("r", "")
            if cell_ref:
                cells[_excel_column_index(cell_ref)] = _excel_cell_value(cell, shared_strings)
        parsed_rows.append([cells.get(index, "") for index in range(max(cells, default=-1) + 1)])

    headers = {value.strip().casefold(): index for index, value in enumerate(parsed_rows[0])}
    required_headers = {"id", "наименование", "тип", "категория", "цена"}
    if not required_headers.issubset(headers):
        raise ValueError("В tovar.xlsx должны быть столбцы ID, Наименование, Тип, Категория, Цена.")

    def get_value(row: list[str], field: str) -> str:
        index = headers[field]
        return row[index].strip() if index < len(row) else ""

    categories: list[Category] = []
    category_ids: dict[str, str] = {}
    products: list[Product] = []
    product_ids: set[str] = set()
    for row_number, row in enumerate(parsed_rows[1:], start=2):
        product_id = get_value(row, "id")
        name = get_value(row, "наименование")
        if not product_id and not name:
            continue
        if not product_id or not name:
            raise ValueError(f"В строке {row_number} не заполнен ID или наименование.")
        if product_id in product_ids:
            raise ValueError(f"Повторяется ID товара {product_id!r} (строка {row_number}).")
        product_ids.add(product_id)

        category_name = get_value(row, "категория")
        if not category_name or category_name == "-":
            category_name = "Прочее"
        category_id = category_ids.get(category_name)
        if category_id is None:
            # Numbering includes excluded categories so saved product settings
            # keep pointing at the same category IDs.
            category_id = f"category-{len(category_ids) + 1}"
            category_ids[category_name] = category_id
            if category_name.casefold() not in EXCLUDED_CATEGORIES:
                categories.append(Category(id=category_id, name=category_name))
        if category_name.casefold() in EXCLUDED_CATEGORIES:
            continue

        try:
            price = Decimal(get_value(row, "цена"))
        except InvalidOperation as exc:
            raise ValueError(f"Некорректная цена товара {name!r} в строке {row_number}.") from exc
        if not price.is_finite() or price < 0:
            raise ValueError(f"Цена товара {name!r} должна быть положительным числом.")
        product_type = get_value(row, "тип")
        unit = "кг" if "(кг)" in product_type.casefold() else "шт."
        weighted = weighted_pricing(name, unit)
        products.append(
            Product(
                id=product_id,
                name=name,
                category_id=category_id,
                description=category_name,
                price=float(WEIGHTED_PRICE_PER_KG) if weighted else float(price),
                image=_image_for_product(name, category_name),
                unit=unit,
                ingredients=default_ingredients(name),
                weighted=weighted,
            )
        )

    if not products:
        raise ValueError("В tovar.xlsx не найдено ни одного товара.")
    return categories, products


def refresh_catalog() -> None:
    global CATEGORIES, PRODUCTS, PRODUCT_BY_ID, VARIANT_BY_ID, MODIFIER_BY_ID, CATALOG_ERROR
    try:
        categories, products = load_catalog()
        with closing(connect_db()) as connection:
            saved_products = connection.execute(
                "SELECT * FROM product_settings"
            ).fetchall()
    except (OSError, ValueError) as exc:
        CATEGORIES = []
        PRODUCTS = []
        PRODUCT_BY_ID = {}
        VARIANT_BY_ID = {}
        MODIFIER_BY_ID = {}
        CATALOG_ERROR = str(exc)
        logger.error("Could not load product catalog: %s", exc)
        return

    product_by_id = {product.id: product for product in products}
    category_ids = {category.id for category in categories}
    ingredient_overrides = {
        saved["product_id"]: saved["ingredients"]
        for saved in saved_products
        if saved["ingredients"] is not None
    }
    for saved in saved_products:
        if saved["category_id"] not in category_ids:
            logger.warning(
                "Ignoring saved product %s with missing category %s",
                saved["product_id"],
                saved["category_id"],
            )
            continue
        if saved["is_custom"] and saved["product_id"] not in product_by_id:
            product = Product(
                id=saved["product_id"],
                name=saved["name"],
                category_id=saved["category_id"],
                description=next(
                    category.name
                    for category in categories
                    if category.id == saved["category_id"]
                ),
                price=float(saved["price"]),
                available=bool(saved["available"]),
                image=saved["image"],
                unit=saved["unit"],
                priority=saved["priority"],
            )
            products.append(product)
            product_by_id[product.id] = product
            continue
        product = product_by_id.get(saved["product_id"])
        if product is None:
            continue
        product.name = saved["name"]
        product.category_id = saved["category_id"]
        product.description = next(
            category.name for category in categories if category.id == saved["category_id"]
        )
        product.price = float(saved["price"])
        product.available = bool(saved["available"])
        product.image = saved["image"]
        product.unit = saved["unit"]
        product.priority = saved["priority"]

    for product in products:
        # Staff-uploaded photos win; otherwise pick the photo by the current name.
        if not product.image.startswith("/static/images/uploads/"):
            product.image = _image_for_product(product.name, product.description)
        product.ingredients = ingredient_overrides.get(
            product.id, default_ingredients(product.name)
        )
        product.weighted = weighted_pricing(product.name, product.unit)
        if product.weighted:
            product.price = float(WEIGHTED_PRICE_PER_KG)

    # Stable sort: equal priorities keep their workbook order.
    products.sort(key=lambda product: (product.priority is None, product.priority or 0))

    CATEGORIES = categories
    PRODUCTS = products
    PRODUCT_BY_ID = {product.id: product for product in products}
    VARIANT_BY_ID = {
        product.id: {variant.id: variant for variant in product.variants}
        for product in products
    }
    MODIFIER_BY_ID = {
        product.id: {modifier.id: modifier for modifier in product.modifiers}
        for product in products
    }
    CATALOG_ERROR = None


def save_product_setting(product: Product, *, is_custom: bool) -> None:
    # NULL keeps following the default composition for the (possibly renamed) product.
    ingredients = product.ingredients.strip()
    ingredients_override = (
        None if ingredients == default_ingredients(product.name) else ingredients
    )
    with closing(connect_db()) as connection:
        connection.execute(
            """
            INSERT INTO product_settings
                (product_id, name, category_id, price, available, image, unit, is_custom,
                 ingredients, priority)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(product_id) DO UPDATE SET
                name = excluded.name,
                category_id = excluded.category_id,
                price = excluded.price,
                available = excluded.available,
                image = excluded.image,
                unit = excluded.unit,
                is_custom = excluded.is_custom,
                ingredients = excluded.ingredients,
                priority = excluded.priority
            """,
            (
                product.id,
                product.name,
                product.category_id,
                str(Decimal(str(product.price))),
                int(product.available),
                product.image,
                product.unit,
                int(is_custom),
                ingredients_override,
                product.priority,
            ),
        )
        connection.commit()


class PrinterError(Exception):
    pass


def default_printer_settings() -> PrinterSettings:
    return PrinterSettings(
        host=PRINTER_HOST,
        port=PRINTER_PORT,
        timeout=PRINTER_TIMEOUT,
        codepage=PRINTER_CODEPAGE,
        encoding=PRINTER_ENCODING,
    )


def load_printer_settings() -> PrinterSettings:
    """Settings saved from the staff panel, or the environment defaults."""
    with closing(connect_db()) as connection:
        row = connection.execute(
            "SELECT value FROM app_settings WHERE key = 'printer'"
        ).fetchone()
    if row is None:
        return default_printer_settings()
    try:
        return PrinterSettings.model_validate_json(row["value"])
    except ValueError:
        logger.warning("Ignoring invalid saved printer settings")
        return default_printer_settings()


def save_printer_settings(settings: PrinterSettings) -> None:
    with closing(connect_db()) as connection:
        connection.execute(
            """
            INSERT INTO app_settings (key, value) VALUES ('printer', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (settings.model_dump_json(),),
        )
        connection.commit()


def format_receipt_money(amount: float) -> str:
    value = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    text = f"{value:f}".rstrip("0").rstrip(".").replace(".", ",")
    return f"{text} {CURRENCY}"


def format_receipt_grams(grams: int) -> str:
    if grams >= 1000 and grams % 100 == 0:
        return f"{grams / 1000:g} кг".replace(".", ",")
    return f"{grams} г"


def _receipt_line(
    text: str,
    *,
    align: str = "left",
    bold: bool = False,
    width: int = 1,
    height: int = 1,
) -> dict[str, Any]:
    return {"text": text, "align": align, "bold": bold, "width": width, "height": height}


def build_receipt(order: dict[str, Any], settings: PrinterSettings) -> list[dict[str, Any]]:
    """Lay out a kitchen receipt; used both for printing and for the staff preview."""
    receipt = settings.receipt
    columns = settings.chars_per_line
    separator = _receipt_line("-" * columns)
    lines: list[dict[str, Any]] = []

    def wrapped(text: str, *, width: int = columns, indent: str = "", **style: Any) -> None:
        for chunk in textwrap.wrap(
            text, width, subsequent_indent=indent, break_long_words=True
        ) or [""]:
            lines.append(_receipt_line(chunk, **style))

    if receipt.header:
        wrapped(receipt.header, align="center", bold=True)
    number_columns = columns // receipt.number_size
    lines.append(
        _receipt_line(
            order["order_number"][:number_columns],
            align="center",
            bold=True,
            width=receipt.number_size,
            height=receipt.number_size,
        )
    )
    service_label = SERVICE_TYPE_LABELS.get(order.get("service_type") or "")
    if receipt.show_service_type and service_label:
        lines.append(
            _receipt_line(service_label.upper(), align="center", bold=True, width=2, height=2)
        )
    if receipt.show_time:
        created = datetime.fromisoformat(order["created_at"]).astimezone()
        lines.append(_receipt_line(created.strftime("%d.%m.%Y %H:%M"), align="center"))

    item_height = 2 if receipt.items_large else 1
    if receipt.show_items:
        lines.append(separator)
        for item in order["items"]:
            title = f"{item['quantity']} x {item['name']}"
            price = (
                format_receipt_money(item["unit_price"] * item["quantity"])
                if receipt.show_prices
                else ""
            )
            name_width = columns - len(price) - 1 if price else columns
            name_lines = textwrap.wrap(
                title, name_width, subsequent_indent="    ", break_long_words=True
            ) or [title]
            first = (
                f"{name_lines[0]:<{name_width}} {price}" if price else name_lines[0]
            )
            lines.append(_receipt_line(first, bold=True, height=item_height))
            for extra in name_lines[1:]:
                lines.append(_receipt_line(extra, bold=True, height=item_height))
            details = []
            if item.get("weight_grams"):
                if item.get("portion_kind") == "price":
                    details.append(
                        f"на {format_receipt_money(item['portion_amount'])}"
                        f" ≈ {format_receipt_grams(item['weight_grams'])}"
                    )
                else:
                    details.append(format_receipt_grams(item["weight_grams"]))
            if item.get("variant"):
                details.append(item["variant"]["name"])
            details.extend(f"+ {modifier['name']}" for modifier in item.get("modifiers", []))
            for detail in details:
                for chunk in textwrap.wrap(detail, columns - 4, break_long_words=True):
                    lines.append(_receipt_line("    " + chunk, height=item_height))
        lines.append(separator)

    if receipt.show_total:
        total = format_receipt_money(order["total"])
        lines.append(
            _receipt_line(f"{'ИТОГО':<{columns - len(total) - 1}} {total}", bold=True)
        )
    if receipt.show_comment and order.get("comment"):
        wrapped(f"Комментарий: {order['comment']}", bold=True)
    if receipt.footer:
        wrapped(receipt.footer, align="center")
    return lines


# Thermal printer code pages (CP866, Windows-1251) have no Tajik letters; print the
# closest Russian letters instead of "?".
RECEIPT_CHAR_REPLACEMENTS = str.maketrans({
    "≈": "~",
    "ғ": "г", "Ғ": "Г", "ӣ": "и", "Ӣ": "И", "қ": "к", "Қ": "К",
    "ӯ": "у", "Ӯ": "У", "ҳ": "х", "Ҳ": "Х", "ҷ": "ч", "Ҷ": "Ч",
})


def render_escpos(lines: list[dict[str, Any]], settings: PrinterSettings) -> bytes:
    alignments = {"left": 0, "center": 1, "right": 2}
    body = b"\x1b@" + b"\x1bt" + bytes((settings.codepage,))  # Init + code page.
    for line in lines:
        size = ((line["width"] - 1) << 4) | (line["height"] - 1)
        body += (
            b"\x1ba" + bytes((alignments[line["align"]],))
            + b"\x1bE" + (b"\x01" if line["bold"] else b"\x00")
            + b"\x1d!" + bytes((size,))
            + line["text"].translate(RECEIPT_CHAR_REPLACEMENTS).encode(settings.encoding, errors="replace")
            + b"\n"
        )
    body += b"\x1bE\x00\x1d!\x00\x1ba\x00"
    body += b"\x1bd" + bytes((settings.receipt.feed_lines,))
    if settings.receipt.cut:
        body += b"\x1dV\x01"
    return body * settings.receipt.copies


class OrderPrinter:
    @staticmethod
    def health(settings: PrinterSettings | None = None) -> dict[str, Any]:
        settings = settings or load_printer_settings()
        if not settings.host:
            return {"configured": False, "connected": False, "message": "Принтер не настроен."}
        try:
            with socket.create_connection(
                (settings.host, settings.port), timeout=settings.timeout
            ):
                return {
                    "configured": True,
                    "connected": True,
                    "message": f"Принтер {settings.host}:{settings.port} на связи.",
                }
        except OSError:
            return {
                "configured": True,
                "connected": False,
                "message": f"Нет связи с принтером {settings.host}:{settings.port}.",
            }

    @staticmethod
    def print_order(
        order: dict[str, Any], settings: PrinterSettings | None = None
    ) -> None:
        settings = settings or load_printer_settings()
        if not settings.host:
            raise PrinterError("Принтер не настроен.")
        if not re.fullmatch(r"[A-Z0-9-]{1,40}", order["order_number"]):
            raise PrinterError("Недопустимый формат номера заказа.")
        data = render_escpos(build_receipt(order, settings), settings)
        try:
            with socket.create_connection(
                (settings.host, settings.port), timeout=settings.timeout
            ) as printer:
                printer.sendall(data)
        except OSError as exc:
            raise PrinterError("Не удалось передать чек на принтер.") from exc


def sample_order() -> dict[str, Any]:
    items = [
        {"name": "Пицца пепперони 33см", "quantity": 1, "unit_price": 75.0,
         "unit": "шт.", "weight_grams": None, "variant": None, "modifiers": []},
        {"name": "Стрипсы острые", "quantity": 1, "unit_price": 47.5, "unit": "порц.",
         "portion_kind": "weight", "portion_amount": 47.5, "weight_grams": 500,
         "variant": None, "modifiers": []},
        {"name": "Крылышки", "quantity": 1, "unit_price": 30.0, "unit": "порц.",
         "portion_kind": "price", "portion_amount": 30.0, "weight_grams": 316,
         "variant": None, "modifiers": []},
        {"name": "Pepsi 0.5л", "quantity": 2, "unit_price": 6.0, "unit": "шт.",
         "weight_grams": None, "variant": None, "modifiers": []},
    ]
    return {
        "order_number": "12",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "comment": "Без лука, соус отдельно",
        "service_type": "takeaway",
        "total": sum(item["unit_price"] * item["quantity"] for item in items),
        "items": items,
    }


def connect_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_db() -> None:
    with closing(connect_db()) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                idempotency_key TEXT NOT NULL UNIQUE,
                request_hash TEXT NOT NULL,
                order_number TEXT NOT NULL UNIQUE,
                total INTEGER NOT NULL,
                items_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                mode TEXT NOT NULL,
                print_status TEXT NOT NULL DEFAULT 'not_attempted'
            )
            """
        )
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(orders)")
        }
        if "service_type" not in columns:
            connection.execute("ALTER TABLE orders ADD COLUMN service_type TEXT NOT NULL DEFAULT ''")
        if "comment" not in columns:
            connection.execute("ALTER TABLE orders ADD COLUMN comment TEXT NOT NULL DEFAULT ''")
        connection.execute("UPDATE orders SET mode = 'local' WHERE mode != 'local'")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS product_settings (
                product_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                category_id TEXT NOT NULL,
                price TEXT NOT NULL,
                available INTEGER NOT NULL,
                image TEXT NOT NULL,
                unit TEXT NOT NULL,
                is_custom INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        product_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(product_settings)")
        }
        if "ingredients" not in product_columns:
            connection.execute("ALTER TABLE product_settings ADD COLUMN ingredients TEXT")
        if "priority" not in product_columns:
            connection.execute("ALTER TABLE product_settings ADD COLUMN priority INTEGER")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        connection.commit()


def resolve_order_items(payload: OrderInput) -> tuple[list[dict[str, Any]], float]:
    normalized: list[dict[str, Any]] = []
    total = Decimal("0")
    for line in payload.items:
        product = PRODUCT_BY_ID.get(line.product_id)
        if product is None or not product.available:
            raise HTTPException(status_code=409, detail="Товар недоступен.")

        portion: dict[str, Any] | None = None
        pricing = product.weighted
        if pricing is None:
            if line.portion_kind is not None or line.portion_value is not None:
                raise HTTPException(
                    status_code=422, detail="Выбор порции недоступен для этого товара."
                )
        elif line.portion_kind is None or line.portion_value is None:
            raise HTTPException(
                status_code=422, detail="Укажите вес или сумму порции."
            )
        elif line.portion_kind == "weight":
            if not pricing.min_grams <= line.portion_value <= pricing.max_grams:
                raise HTTPException(
                    status_code=422,
                    detail=f"Вес порции — от {pricing.min_grams} г до {pricing.max_grams // 1000} кг.",
                )
            portion = {
                "kind": "weight",
                "amount": price_for_grams(line.portion_value),
                "grams": line.portion_value,
            }
        else:
            if not pricing.min_amount <= line.portion_value <= pricing.max_amount:
                raise HTTPException(
                    status_code=422,
                    detail=f"Сумма порции — от {pricing.min_amount} до {pricing.max_amount} сомони.",
                )
            portion = {
                "kind": "price",
                "amount": Decimal(line.portion_value),
                "grams": grams_for_amount(line.portion_value),
            }

        variant = None
        if product.variants:
            if line.variant_id is None:
                raise HTTPException(
                    status_code=422, detail=f"Выберите вариант товара «{product.name}»."
                )
            variant = VARIANT_BY_ID[product.id].get(line.variant_id)
            if variant is None:
                raise HTTPException(status_code=422, detail="Недопустимый вариант товара.")
        elif line.variant_id is not None:
            raise HTTPException(status_code=422, detail="У товара нет такого варианта.")

        selected_modifiers = []
        seen_modifiers: set[str] = set()
        for modifier_id in line.modifier_ids:
            if modifier_id in seen_modifiers:
                raise HTTPException(status_code=422, detail="Добавка указана повторно.")
            seen_modifiers.add(modifier_id)
            modifier = MODIFIER_BY_ID[product.id].get(modifier_id)
            if modifier is None:
                raise HTTPException(status_code=422, detail="Недопустимая добавка.")
            selected_modifiers.append(modifier)

        unit_price = (
            portion["amount"]
            if portion
            else Decimal(str(product.price))
        )
        if variant and not portion:
            unit_price += Decimal(str(variant.price))
        unit_price += sum(
            (Decimal(str(modifier.price)) for modifier in selected_modifiers),
            start=Decimal("0"),
        )
        total += unit_price * line.quantity
        if total > MAX_ORDER_TOTAL:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Сумма заказа больше {MAX_ORDER_TOTAL:f} {CURRENCY}. "
                    "Пожалуйста, оформите заказ через кассу."
                ),
            )
        unit = "порц." if portion else product.unit
        normalized.append(
            {
                "product_id": product.id,
                "name": product.name,
                "quantity": line.quantity,
                "unit_price": float(unit_price),
                "unit": unit,
                "portion_kind": portion["kind"] if portion else None,
                "portion_amount": float(portion["amount"]) if portion else None,
                "weight_grams": (
                    portion["grams"] if portion else None
                ),
                "variant": (
                    {"id": variant.id, "name": variant.name} if variant else None
                ),
                "modifiers": [
                    {"id": modifier.id, "name": modifier.name, "price": modifier.price}
                    for modifier in selected_modifiers
                ],
            }
        )
    return normalized, float(total)


def display_order_number(order_id: int) -> str:
    return str((order_id - 1) % ORDER_NUMBER_MAX + 1)


def public_order(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "order_number": display_order_number(row["id"]),
        "order_ref": row["order_number"],
        "total": row["total"],
        "items": json.loads(row["items_json"]),
        "comment": row["comment"],
        "service_type": row["service_type"] or None,
        "created_at": row["created_at"],
        "payment_status": "unpaid",
        "print_status": row["print_status"],
    }


def require_admin(authorization: str | None) -> None:
    if not ADMIN_TOKEN:
        raise HTTPException(
            status_code=503, detail="Администрирование отключено: задайте KIOSK_ADMIN_TOKEN."
        )
    expected = f"Bearer {ADMIN_TOKEN}"
    if authorization is None or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="Требуется авторизация сотрудника.")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    initialize_db()
    refresh_catalog()
    yield


app = FastAPI(
    title="Paprika Self-Service Kiosk",
    version="1.0.0",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def kiosk_home() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/assets/logo.jpg", include_in_schema=False)
def company_logo() -> FileResponse:
    if not LOGO_PATH.is_file():
        raise HTTPException(status_code=404, detail="Файл logo.jpg не найден.")
    return FileResponse(LOGO_PATH, media_type="image/jpeg")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "paprika-kiosk"}


@app.get("/api/menu", response_model=MenuResponse)
def get_menu() -> MenuResponse:
    if CATALOG_ERROR or not PRODUCTS:
        raise HTTPException(
            status_code=503,
            detail=CATALOG_ERROR or "Каталог товаров из файла tovar.xlsx недоступен.",
        )
    return MenuResponse(
        currency=CURRENCY,
        max_order_total=float(MAX_ORDER_TOTAL),
        categories=CATEGORIES,
        products=PRODUCTS,
    )


def validate_product_category(category_id: str) -> None:
    if category_id not in {category.id for category in CATEGORIES}:
        raise HTTPException(status_code=422, detail="Выберите существующую категорию.")


def persist_product(product: Product, *, is_custom: bool) -> None:
    try:
        save_product_setting(product, is_custom=is_custom)
        refresh_catalog()
    except sqlite3.Error as exc:
        logger.exception("Could not save product %s", product.id)
        raise HTTPException(status_code=500, detail="Не удалось сохранить товар.") from exc
    if CATALOG_ERROR:
        raise HTTPException(status_code=503, detail=CATALOG_ERROR)


@app.get("/api/admin/products")
def admin_products(
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    require_admin(authorization)
    if CATALOG_ERROR:
        raise HTTPException(status_code=503, detail=CATALOG_ERROR)
    return {
        "categories": [category.model_dump() for category in CATEGORIES],
        "products": [product.model_dump() for product in PRODUCTS],
    }


@app.post("/api/admin/products", status_code=201)
def create_product(
    payload: ProductCreate,
    authorization: Annotated[str | None, Header()] = None,
) -> Product:
    require_admin(authorization)
    validate_product_category(payload.category_id)
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Укажите название товара.")
    product_id = str(uuid.uuid4())
    category = next(category for category in CATEGORIES if category.id == payload.category_id)
    product = Product(
        id=product_id,
        name=name,
        category_id=payload.category_id,
        description=category.name,
        price=float(payload.price),
        available=payload.available,
        image=_image_for_product(name, category.name),
        unit=payload.unit,
        ingredients=(
            payload.ingredients.strip()
            if payload.ingredients is not None
            else default_ingredients(name)
        ),
        priority=payload.priority,
    )
    persist_product(product, is_custom=True)
    return product


@app.put("/api/admin/products/{product_id}")
def update_product(
    product_id: str,
    payload: ProductUpdate,
    authorization: Annotated[str | None, Header()] = None,
) -> Product:
    require_admin(authorization)
    existing = PRODUCT_BY_ID.get(product_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Товар не найден.")
    validate_product_category(payload.category_id)
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Укажите название товара.")
    category = next(category for category in CATEGORIES if category.id == payload.category_id)
    product = existing.model_copy(
        update={
            "name": name,
            "category_id": payload.category_id,
            "description": category.name,
            "price": float(payload.price),
            "available": payload.available,
            **(
                {"ingredients": payload.ingredients.strip()}
                if payload.ingredients is not None
                else {}
            ),
            **(
                {"priority": payload.priority}
                if "priority" in payload.model_fields_set
                else {}
            ),
        }
    )
    with closing(connect_db()) as connection:
        row = connection.execute(
            "SELECT is_custom FROM product_settings WHERE product_id = ?",
            (product_id,),
        ).fetchone()
    persist_product(product, is_custom=bool(row["is_custom"]) if row else False)
    return product


@app.post("/api/admin/products/{product_id}/image")
async def upload_product_image(
    product_id: str,
    authorization: Annotated[str | None, Header()] = None,
    image: UploadFile = File(...),
) -> Product:
    require_admin(authorization)
    existing = PRODUCT_BY_ID.get(product_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Товар не найден.")
    content = await image.read(MAX_PRODUCT_IMAGE_SIZE + 1)
    if len(content) > MAX_PRODUCT_IMAGE_SIZE:
        raise HTTPException(status_code=413, detail="Размер изображения не должен превышать 5 МБ.")
    image_types = (
        (content.startswith(b"\xff\xd8\xff"), "jpg"),
        (content.startswith(b"\x89PNG\r\n\x1a\n"), "png"),
        (len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP", "webp"),
    )
    extension = next((extension for is_valid, extension in image_types if is_valid), None)
    if extension is None:
        raise HTTPException(
            status_code=415,
            detail="Поддерживаются изображения JPEG, PNG и WebP.",
        )
    IMAGE_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.{extension}"
    image_path = IMAGE_UPLOAD_DIR / filename
    try:
        image_path.write_bytes(content)
    except OSError as exc:
        logger.exception("Could not save uploaded product image")
        raise HTTPException(status_code=500, detail="Не удалось сохранить изображение.") from exc

    product = existing.model_copy(update={"image": f"/static/images/uploads/{filename}"})
    with closing(connect_db()) as connection:
        row = connection.execute(
            "SELECT is_custom FROM product_settings WHERE product_id = ?",
            (product_id,),
        ).fetchone()
    try:
        persist_product(product, is_custom=bool(row["is_custom"]) if row else False)
    except HTTPException:
        image_path.unlink(missing_ok=True)
        raise
    previous_image = existing.image
    if previous_image.startswith("/static/images/uploads/"):
        previous_path = STATIC_DIR / previous_image.removeprefix("/static/")
        if previous_path.parent == IMAGE_UPLOAD_DIR and previous_path != image_path:
            previous_path.unlink(missing_ok=True)
    return product


@app.post("/api/orders", status_code=201)
def create_order(
    payload: OrderInput,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    if idempotency_key is None:
        raise HTTPException(status_code=400, detail="Отсутствует ключ запроса.")
    try:
        parsed_key = str(uuid.UUID(idempotency_key))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Некорректный ключ запроса.") from exc

    if payload.service_type is None:
        raise HTTPException(status_code=422, detail="Выберите: в зале или с собой.")
    normalized, total = resolve_order_items(payload)
    comment = payload.comment.strip()
    request_data = payload.model_dump(mode="json")
    for item in request_data["items"]:
        for field in ("portion_kind", "portion_value"):
            if item[field] is None:
                item.pop(field)
    if not comment:
        request_data.pop("comment", None)
    canonical_request = json.dumps(
        request_data, sort_keys=True, separators=(",", ":")
    )
    request_hash = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
    created_at = datetime.now(timezone.utc).isoformat()

    with closing(connect_db()) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM orders WHERE idempotency_key = ?", (parsed_key,)
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise HTTPException(
                        status_code=409,
                        detail="Ключ уже использован для другого состава заказа.",
                    )
                connection.commit()
                return public_order(existing)

            cursor = connection.execute(
                """
                INSERT INTO orders
                    (idempotency_key, request_hash, order_number, total,
                     items_json, created_at, mode, comment, service_type)
                VALUES (?, ?, 'pending', ?, ?, ?, 'local', ?, ?)
                """,
                (
                    parsed_key,
                    request_hash,
                    total,
                    json.dumps(normalized, ensure_ascii=False),
                    created_at,
                    comment,
                    payload.service_type,
                ),
            )
            order_number = f"P-{cursor.lastrowid:05d}"
            connection.execute(
                "UPDATE orders SET order_number = ? WHERE id = ?",
                (order_number, cursor.lastrowid),
            )
            connection.commit()
        except HTTPException:
            connection.rollback()
            raise
        except sqlite3.Error as exc:
            connection.rollback()
            logger.exception("Could not persist order")
            raise HTTPException(status_code=500, detail="Не удалось сохранить заказ.") from exc

        row = connection.execute(
            "SELECT * FROM orders WHERE idempotency_key = ?", (parsed_key,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=500, detail="Не удалось получить созданный заказ.")
        result = public_order(row)

    try:
        OrderPrinter.print_order(result)
        result["print_status"] = "printed"
        update_print_status(parsed_key, "printed")
    except PrinterError as exc:
        result["print_status"] = "failed"
        update_print_status(parsed_key, "failed")
        logger.warning("Print failed for order %s: %s", result["order_number"], exc)
    return result


def update_print_status(idempotency_key: str, status: str) -> None:
    with closing(connect_db()) as connection:
        connection.execute(
            "UPDATE orders SET print_status = ? WHERE idempotency_key = ?",
            (status, idempotency_key),
        )
        connection.commit()


def update_order_print_status(order_number: str, status: str) -> None:
    with closing(connect_db()) as connection:
        connection.execute(
            "UPDATE orders SET print_status = ? WHERE order_number = ?",
            (status, order_number),
        )
        connection.commit()


@app.get("/api/admin/status")
def admin_status(
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    require_admin(authorization)
    return {
        "printer": OrderPrinter.health(),
        "orders_saved": True,
    }


@app.get("/api/admin/printer")
def get_printer_settings(
    authorization: Annotated[str | None, Header()] = None,
) -> PrinterSettings:
    require_admin(authorization)
    return load_printer_settings()


@app.put("/api/admin/printer")
def update_printer_settings(
    payload: PrinterSettings,
    authorization: Annotated[str | None, Header()] = None,
) -> PrinterSettings:
    require_admin(authorization)
    try:
        save_printer_settings(payload)
    except sqlite3.Error as exc:
        logger.exception("Could not save printer settings")
        raise HTTPException(status_code=500, detail="Не удалось сохранить настройки.") from exc
    return payload


@app.post("/api/admin/printer/preview")
def preview_receipt(
    payload: PrinterSettings,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    require_admin(authorization)
    return {
        "chars_per_line": payload.chars_per_line,
        "lines": build_receipt(sample_order(), payload),
    }


@app.post("/api/admin/printer/check")
def check_printer(
    payload: PrinterSettings,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    require_admin(authorization)
    return OrderPrinter.health(payload)


@app.post("/api/admin/printer/test")
def print_test_receipt(
    payload: PrinterSettings,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    require_admin(authorization)
    try:
        OrderPrinter.print_order(sample_order(), payload)
    except PrinterError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"message": "Пробный чек отправлен на принтер."}


@app.post("/api/admin/print")
def reprint_order(
    payload: PrintInput,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    require_admin(authorization)
    with closing(connect_db()) as connection:
        row = connection.execute(
            "SELECT * FROM orders WHERE order_number = ?",
            (payload.order_ref,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Заказ не найден.")
    try:
        OrderPrinter.print_order(public_order(row))
    except PrinterError as exc:
        logger.warning("Reprint failed for order %s: %s", row["order_number"], exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    update_order_print_status(row["order_number"], "printed")
    return {
        "order_number": display_order_number(row["id"]),
        "order_ref": row["order_number"],
        "print_status": "printed",
        "comment_printed": bool(row["comment"]),
    }


@app.get("/api/admin/orders")
def admin_orders(
    authorization: Annotated[str | None, Header()] = None,
) -> list[dict[str, Any]]:
    require_admin(authorization)
    with closing(connect_db()) as connection:
        rows = connection.execute(
            "SELECT * FROM orders ORDER BY id DESC LIMIT 50"
        ).fetchall()
    return [public_order(row) for row in rows]


def report_period_bounds(
    start_date: date | None,
    end_date: date | None,
) -> tuple[date, date, str, str]:
    today = date.today()
    start = start_date or today
    end = end_date or today
    if start > end:
        raise HTTPException(
            status_code=422, detail="Дата начала периода позже даты окончания."
        )

    start_utc = datetime.combine(start, time.min).astimezone().astimezone(timezone.utc)
    end_utc = datetime.combine(end + timedelta(days=1), time.min).astimezone().astimezone(timezone.utc)
    return start, end, start_utc.isoformat(), end_utc.isoformat()


def build_report(
    start_date: date | None,
    end_date: date | None,
) -> tuple[dict[str, Any], list[sqlite3.Row]]:
    start, end, start_utc, end_utc = report_period_bounds(start_date, end_date)
    with closing(connect_db()) as connection:
        rows = connection.execute(
            """
            SELECT id, order_number, total, items_json, created_at, print_status, comment,
                   service_type
            FROM orders
            WHERE created_at >= ? AND created_at < ?
            ORDER BY created_at, id
            """,
            (start_utc, end_utc),
        ).fetchall()

    product_summary: dict[str, dict[str, Any]] = {}
    item_count = 0
    total_amount = Decimal("0")
    for row in rows:
        total_amount += Decimal(str(row["total"]))
        for item in json.loads(row["items_json"]):
            key = item["product_id"]
            summary = product_summary.setdefault(
                key,
                {
                    "product_id": key,
                    "name": item["name"],
                    "unit": item.get("unit", "шт."),
                    "quantity": 0,
                    "amount": Decimal("0"),
                },
            )
            summary["quantity"] += item["quantity"]
            summary["amount"] += Decimal(str(item["unit_price"])) * item["quantity"]
            if item.get("weight_grams") is not None:
                summary["weight_grams"] = (
                    summary.get("weight_grams", 0)
                    + item["weight_grams"] * item["quantity"]
                )
            item_count += item["quantity"]

    report = {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "orders_count": len(rows),
        "items_count": item_count,
        "total_amount": float(round(total_amount, 2)),
        "average_order_amount": (
            float(round(total_amount / len(rows), 2)) if rows else 0
        ),
        "products": sorted(
            (
                {**item, "amount": float(item["amount"])}
                for item in product_summary.values()
            ),
            key=lambda item: item["amount"],
            reverse=True,
        ),
    }
    return report, rows


@app.get("/api/admin/report")
def admin_report(
    authorization: Annotated[str | None, Header()] = None,
    start_date: date | None = None,
    end_date: date | None = None,
) -> dict[str, Any]:
    require_admin(authorization)
    report, _ = build_report(start_date, end_date)
    return report


@app.get("/api/admin/report.csv")
def download_admin_report(
    authorization: Annotated[str | None, Header()] = None,
    start_date: date | None = None,
    end_date: date | None = None,
) -> Response:
    require_admin(authorization)
    report, orders = build_report(start_date, end_date)
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["Период", report["start_date"], report["end_date"]])
    writer.writerow(["Количество заказов", report["orders_count"]])
    writer.writerow(["Количество позиций", report["items_count"]])
    writer.writerow(["Общая сумма", report["total_amount"]])
    writer.writerow([])
    writer.writerow(
        [
            "Номер заказа",
            "Внутренний номер",
            "Дата и время (UTC)",
            "В зале / с собой",
            "Товар",
            "Вариант",
            "Добавки",
            "Количество",
            "Единица",
            "Цена за единицу",
            "Сумма порции",
            "Вес порции (г)",
            "Сумма позиции",
            "Комментарий к заказу",
            "Оплата",
        ]
    )
    for order in orders:
        for item in json.loads(order["items_json"]):
            writer.writerow(
                [
                    display_order_number(order["id"]),
                    order["order_number"],
                    order["created_at"],
                    SERVICE_TYPE_LABELS.get(order["service_type"], ""),
                    item["name"],
                    item["variant"]["name"] if item["variant"] else "",
                    ", ".join(modifier["name"] for modifier in item["modifiers"]),
                    item["quantity"],
                    item.get("unit", "шт."),
                    item["unit_price"],
                    item.get("portion_amount") or "",
                    item.get("weight_grams") or "",
                    item["unit_price"] * item["quantity"],
                    order["comment"],
                    "не оплачено",
                ]
            )

    filename = f"paprika-orders-{report['start_date']}-{report['end_date']}.csv"
    return Response(
        content="\ufeff" + output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
