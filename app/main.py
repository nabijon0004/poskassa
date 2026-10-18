from __future__ import annotations

import codecs
import hashlib
import json
import logging
import os
import re
import sqlite3
import textwrap
import uuid
from contextlib import asynccontextmanager, closing
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from posixpath import normpath
from typing import Annotated, Any, AsyncIterator, Literal
from unicodedata import normalize
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator


ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"
CATALOG_PATH = ROOT / "tovar.xlsx"
LOGO_PATH = ROOT / "logo.jpg"
DB_PATH = Path(os.getenv("KIOSK_DB_PATH", str(ROOT / "data" / "kiosk.sqlite3")))
CURRENCY = os.getenv("KIOSK_CURRENCY", "с")
# Larger orders must be placed at the cash desk.
MAX_ORDER_TOTAL = Decimal(os.getenv("KIOSK_MAX_ORDER_TOTAL", "1000"))
# Customer-facing order numbers run 1..ORDER_NUMBER_MAX and then start again from 1.
ORDER_NUMBER_MAX = int(os.getenv("KIOSK_ORDER_NUMBER_MAX", "50"))
# Title printed at the top of customer receipts from the cash register.
RECEIPT_TITLE = os.getenv("KIOSK_RECEIPT_TITLE", "PAPRIKA")
PRINTER_HOST = os.getenv("KIOSK_PRINTER_HOST", "").strip()
PRINTER_PORT = int(os.getenv("KIOSK_PRINTER_PORT", "9100"))
PRINTER_TIMEOUT = float(os.getenv("KIOSK_PRINTER_TIMEOUT", "3"))
PRINTER_CODEPAGE = int(os.getenv("KIOSK_PRINTER_CODEPAGE", "17"))
PRINTER_ENCODING = os.getenv("KIOSK_PRINTER_ENCODING", "cp866")
IMAGE_UPLOAD_DIR = STATIC_DIR / "images" / "uploads"
MAX_PRODUCT_IMAGE_SIZE = 5 * 1024 * 1024

# Kiosks, registers and queue screens send the key of their device (see app/stores.py).
DeviceKey = Annotated[str | None, Header(alias="X-Device-Key")]

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
PAYMENT_METHOD_LABELS = {"cash": "Наличные", "card": "Карта"}
PAYMENT_STATUS_LABELS = {
    "unpaid": "не оплачено",
    "paid": "оплачено",
    "refunded": "возврат",
    "cancelled": "отменён",
}


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


@dataclass
class Catalog:
    """The menu of one store: the shared menu from tovar.xlsx plus the store's changes."""

    categories: list[Category]
    products: list[Product]

    def __post_init__(self) -> None:
        self.product_by_id = {product.id: product for product in self.products}
        self.variant_by_id = {
            product.id: {variant.id: variant for variant in product.variants} for product in self.products
        }
        self.modifier_by_id = {
            product.id: {modifier.id: modifier for modifier in product.modifiers} for product in self.products
        }
        self.category_by_id = {category.id: category for category in self.categories}


# The shared menu read from tovar.xlsx, and the per-store menus built from it.
BASE_CATEGORIES: list[Category] = []
BASE_PRODUCTS: list[Product] = []
CATALOG_ERROR: str | None = None
_STORE_CATALOGS: dict[int, Catalog] = {}

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
    """Reload the shared menu from tovar.xlsx; store menus are rebuilt on next use."""
    global BASE_CATEGORIES, BASE_PRODUCTS, CATALOG_ERROR
    try:
        categories, products = load_catalog()
    except (OSError, ValueError) as exc:
        BASE_CATEGORIES = []
        BASE_PRODUCTS = []
        CATALOG_ERROR = str(exc)
        logger.error("Could not load product catalog: %s", exc)
    else:
        BASE_CATEGORIES = categories
        BASE_PRODUCTS = products
        CATALOG_ERROR = None
    _STORE_CATALOGS.clear()


def invalidate_catalog(store_id: int) -> None:
    _STORE_CATALOGS.pop(store_id, None)


def catalog_for(store_id: int) -> Catalog:
    """The store's menu: the shared menu with the store's saved product changes applied."""
    cached = _STORE_CATALOGS.get(store_id)
    if cached is not None:
        return cached
    if CATALOG_ERROR or not BASE_PRODUCTS:
        raise HTTPException(
            status_code=503,
            detail=CATALOG_ERROR or "Каталог товаров из файла tovar.xlsx недоступен.",
        )
    categories = [category.model_copy() for category in BASE_CATEGORIES]
    products = [product.model_copy(deep=True) for product in BASE_PRODUCTS]
    with closing(connect_db()) as connection:
        saved_products = connection.execute(
            "SELECT * FROM product_settings WHERE store_id = ?", (store_id,)
        ).fetchall()

    product_by_id = {product.id: product for product in products}
    category_names = {category.id: category.name for category in categories}
    ingredient_overrides = {
        saved["product_id"]: saved["ingredients"]
        for saved in saved_products
        if saved["ingredients"] is not None
    }
    for saved in saved_products:
        if saved["category_id"] not in category_names:
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
                description=category_names[saved["category_id"]],
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
        product.description = category_names[saved["category_id"]]
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
    catalog = Catalog(categories, products)
    _STORE_CATALOGS[store_id] = catalog
    return catalog


def save_product_setting(store_id: int, product: Product, *, is_custom: bool) -> None:
    # NULL keeps following the default composition for the (possibly renamed) product.
    ingredients = product.ingredients.strip()
    ingredients_override = (
        None if ingredients == default_ingredients(product.name) else ingredients
    )
    with closing(connect_db()) as connection:
        connection.execute(
            """
            INSERT INTO product_settings
                (store_id, product_id, name, category_id, price, available, image, unit, is_custom,
                 ingredients, priority)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(store_id, product_id) DO UPDATE SET
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
                store_id,
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
    invalidate_catalog(store_id)


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


def printer_settings_key(store_id: int) -> str:
    return f"printer:{store_id}"


def load_printer_settings(store_id: int) -> PrinterSettings:
    """The store's settings saved in the admin panel, or the environment defaults."""
    with closing(connect_db()) as connection:
        row = connection.execute(
            "SELECT value FROM app_settings WHERE key = ?", (printer_settings_key(store_id),)
        ).fetchone()
    if row is None:
        return default_printer_settings()
    try:
        return PrinterSettings.model_validate_json(row["value"])
    except ValueError:
        logger.warning("Ignoring invalid saved printer settings")
        return default_printer_settings()


def save_printer_settings(store_id: int, settings: PrinterSettings) -> None:
    with closing(connect_db()) as connection:
        connection.execute(
            """
            INSERT INTO app_settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (printer_settings_key(store_id), settings.model_dump_json()),
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


def receipt_item_lines(
    items: list[dict[str, Any]], columns: int, *, show_prices: bool, height: int = 1
) -> list[dict[str, Any]]:
    """Item rows shared by the kitchen and customer receipts."""
    lines: list[dict[str, Any]] = []
    for item in items:
        title = f"{item['quantity']} x {item['name']}"
        price = (
            format_receipt_money(item["unit_price"] * item["quantity"]) if show_prices else ""
        )
        name_width = columns - len(price) - 1 if price else columns
        name_lines = textwrap.wrap(
            title, name_width, subsequent_indent="    ", break_long_words=True
        ) or [title]
        first = f"{name_lines[0]:<{name_width}} {price}" if price else name_lines[0]
        lines.append(_receipt_line(first, bold=True, height=height))
        for extra in name_lines[1:]:
            lines.append(_receipt_line(extra, bold=True, height=height))
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
                lines.append(_receipt_line("    " + chunk, height=height))
    return lines


def _amount_line(label: str, amount: float, columns: int, *, bold: bool = False) -> dict[str, Any]:
    value = format_receipt_money(amount)
    return _receipt_line(f"{label:<{columns - len(value) - 1}} {value}", bold=bold)


def build_customer_receipt(
    order: dict[str, Any], settings: PrinterSettings, *, cashier_name: str = ""
) -> list[dict[str, Any]]:
    """Non-fiscal sales receipt for the customer, printed by the cash register."""
    columns = settings.chars_per_line
    separator = _receipt_line("-" * columns)
    created = datetime.fromisoformat(order.get("paid_at") or order["created_at"]).astimezone()
    lines = [
        _receipt_line(RECEIPT_TITLE[: columns // 2], align="center", bold=True, width=2, height=2),
        _receipt_line("ТОВАРНЫЙ ЧЕК", align="center", bold=True),
        _receipt_line(f"Заказ № {order['order_number']}", align="center", bold=True, width=2, height=2),
        _receipt_line(created.strftime("%d.%m.%Y %H:%M"), align="center"),
    ]
    if cashier_name:
        lines.append(_receipt_line(f"Кассир: {cashier_name}"[:columns], align="center"))
    service_label = SERVICE_TYPE_LABELS.get(order.get("service_type") or "")
    if service_label:
        lines.append(_receipt_line(service_label, align="center"))
    lines.append(separator)
    lines.extend(receipt_item_lines(order["items"], columns, show_prices=True))
    lines.append(separator)
    discount = order.get("discount") or 0
    if discount:
        lines.append(_amount_line("Подытог", order.get("subtotal") or order["total"], columns))
        lines.append(_amount_line("Скидка", -discount, columns))
    lines.append(_amount_line("ИТОГО", order["total"], columns, bold=True))
    method = order.get("payment_method")
    if method == "cash":
        lines.append(_amount_line("Наличные", order.get("amount_received") or order["total"], columns))
        lines.append(_amount_line("Сдача", order.get("change_due") or 0, columns))
    elif method == "card":
        lines.append(_amount_line("Оплата картой", order["total"], columns))
    lines.append(separator)
    lines.append(_receipt_line("Спасибо за покупку!", align="center", bold=True))
    lines.append(_receipt_line("Нефискальный документ", align="center"))
    return lines


def single_copy(settings: PrinterSettings) -> PrinterSettings:
    return settings.model_copy(
        update={"receipt": settings.receipt.model_copy(update={"copies": 1})}
    )


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
        lines.extend(
            receipt_item_lines(
                order["items"], columns, show_prices=receipt.show_prices, height=item_height
            )
        )
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
    """Puts documents into the store's print queue.

    The cloud server cannot reach printers in the stores' local networks: the cash
    register program in the store takes the jobs and sends them to the printer.
    """

    @staticmethod
    def print_order(
        order: dict[str, Any], store_id: int, settings: PrinterSettings | None = None
    ) -> None:
        settings = settings or load_printer_settings(store_id)
        if not re.fullmatch(r"[A-Z0-9-]{1,40}", order["order_number"]):
            raise PrinterError("Недопустимый формат номера заказа.")
        OrderPrinter.send(
            store_id,
            render_escpos(build_receipt(order, settings), settings),
            settings,
            kind="kitchen",
            title=f"Заказ № {order['order_number']}",
            order_ref=order.get("order_ref"),
        )

    @staticmethod
    def print_lines(
        lines: list[dict[str, Any]],
        store_id: int,
        *,
        title: str,
        settings: PrinterSettings | None = None,
    ) -> None:
        """Print one copy of a document other than the kitchen ticket."""
        settings = single_copy(settings or load_printer_settings(store_id))
        OrderPrinter.send(store_id, render_escpos(lines, settings), settings, kind="document", title=title)

    @staticmethod
    def send(
        store_id: int,
        data: bytes,
        settings: PrinterSettings,
        *,
        kind: str,
        title: str,
        order_ref: str | None = None,
    ) -> None:
        if not settings.host:
            raise PrinterError("Принтер не настроен.")
        printing.enqueue_job(store_id, data, kind=kind, title=title, order_ref=order_ref)


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
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        # Data created before stores existed belongs to the first store.
        default_store_id = stores.initialize_store_tables(connection)
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
        # Payment data filled in by the cash register (kiosk orders start unpaid).
        payment_columns = {
            "source": "TEXT NOT NULL DEFAULT 'kiosk'",
            "payment_status": "TEXT NOT NULL DEFAULT 'unpaid'",
            "payment_method": "TEXT NOT NULL DEFAULT ''",
            "subtotal": "REAL",
            "discount": "REAL NOT NULL DEFAULT 0",
            "amount_received": "REAL",
            "change_due": "REAL",
            "paid_at": "TEXT",
            "shift_id": "INTEGER",
            "cashier_id": "INTEGER",
            "refunded_at": "TEXT",
            "refund_shift_id": "INTEGER",
            "cancelled_at": "TEXT",
            # Queue screen: 'preparing' -> 'ready' (set by the cashier); '' = not for the kitchen.
            "kitchen_status": "TEXT NOT NULL DEFAULT ''",
            "ready_at": "TEXT",
            # Store, the kiosk or register that created the order, and the register that took payment.
            "store_id": f"INTEGER NOT NULL DEFAULT {default_store_id}",
            "device_id": "INTEGER",
            "register_id": "INTEGER",
            # Per-store order counter; the customer number is derived from it.
            "store_seq": "INTEGER",
            # Number printed by a register that sold the order without internet (О12).
            "offline_number": "TEXT",
        }
        for name, definition in payment_columns.items():
            if name not in columns:
                connection.execute(f"ALTER TABLE orders ADD COLUMN {name} {definition}")
        connection.execute("UPDATE orders SET mode = 'local' WHERE mode != 'local'")
        connection.execute("UPDATE orders SET store_seq = id WHERE store_seq IS NULL")
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS orders_store_seq ON orders (store_id, store_seq)"
        )
        connection.execute("CREATE INDEX IF NOT EXISTS orders_store_created ON orders (store_id, created_at)")
        migrate_product_settings(connection, default_store_id)
        if connection.execute(
            "SELECT 1 FROM app_settings WHERE key = ?", (printer_settings_key(default_store_id),)
        ).fetchone() is None:
            connection.execute(
                "UPDATE app_settings SET key = ? WHERE key = 'printer'",
                (printer_settings_key(default_store_id),),
            )
        drop_cloud_sync_tables(connection)

        from app.admin import initialize_admin_tables
        from app.pos import initialize_pos_tables

        initialize_pos_tables(connection, default_store_id)
        printing.initialize_print_tables(connection)
        initialize_admin_tables(connection)
        connection.commit()


def migrate_product_settings(connection: sqlite3.Connection, default_store_id: int) -> None:
    """Product changes are kept per store: (store_id, product_id)."""
    create = """
        CREATE TABLE IF NOT EXISTS {name} (
            store_id INTEGER NOT NULL,
            product_id TEXT NOT NULL,
            name TEXT NOT NULL,
            category_id TEXT NOT NULL,
            price TEXT NOT NULL,
            available INTEGER NOT NULL,
            image TEXT NOT NULL,
            unit TEXT NOT NULL,
            is_custom INTEGER NOT NULL DEFAULT 0,
            ingredients TEXT,
            priority INTEGER,
            PRIMARY KEY (store_id, product_id)
        )
    """
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(product_settings)")}
    if not columns:
        connection.execute(create.format(name="product_settings"))
        return
    if "store_id" in columns:
        return
    for name in ("ingredients", "priority"):
        if name not in columns:
            connection.execute(
                f"ALTER TABLE product_settings ADD COLUMN {name} {'TEXT' if name == 'ingredients' else 'INTEGER'}"
            )
    connection.execute(create.format(name="product_settings_by_store"))
    connection.execute(
        """
        INSERT INTO product_settings_by_store
            (store_id, product_id, name, category_id, price, available, image, unit, is_custom,
             ingredients, priority)
        SELECT ?, product_id, name, category_id, price, available, image, unit, is_custom,
               ingredients, priority
        FROM product_settings
        """,
        (default_store_id,),
    )
    connection.execute("DROP TABLE product_settings")
    connection.execute("ALTER TABLE product_settings_by_store RENAME TO product_settings")


def drop_cloud_sync_tables(connection: sqlite3.Connection) -> None:
    """The offline sync to a central server was replaced by one shared server."""
    for table in ("orders", "shifts"):
        for event in ("insert", "update"):
            connection.execute(f"DROP TRIGGER IF EXISTS sync_{table}_{event}")
    connection.execute("DROP TABLE IF EXISTS sync_outbox")
    connection.execute("DROP TABLE IF EXISTS synced_records")
    connection.execute("DELETE FROM app_settings WHERE key = 'sync_backfilled'")


def resolve_order_items(
    payload: Any, catalog: Catalog, *, enforce_total_limit: bool = True, require_available: bool = True
) -> tuple[list[dict[str, Any]], float]:
    """Price order lines with the store's menu; the kiosk limit does not apply at the register."""
    normalized: list[dict[str, Any]] = []
    total = Decimal("0")
    for line in payload.items:
        product = catalog.product_by_id.get(line.product_id)
        # A sale made offline is recorded even if the product was hidden meanwhile.
        if product is None or (require_available and not product.available):
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
            variant = catalog.variant_by_id[product.id].get(line.variant_id)
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
            modifier = catalog.modifier_by_id[product.id].get(modifier_id)
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
        if enforce_total_limit and total > MAX_ORDER_TOTAL:
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


def display_order_number(store_seq: int) -> str:
    """Customer number: each store counts 1..ORDER_NUMBER_MAX and then starts again."""
    return str((store_seq - 1) % ORDER_NUMBER_MAX + 1)


def public_order(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "order_number": display_order_number(row["store_seq"]),
        "store_id": row["store_id"],
        "order_ref": row["order_number"],
        "total": row["total"],
        "items": json.loads(row["items_json"]),
        "comment": row["comment"],
        "service_type": row["service_type"] or None,
        "created_at": row["created_at"],
        "source": row["source"],
        "payment_status": row["payment_status"],
        "payment_method": row["payment_method"] or None,
        "subtotal": row["subtotal"] if row["subtotal"] is not None else row["total"],
        "discount": row["discount"],
        "amount_received": row["amount_received"],
        "change_due": row["change_due"],
        "paid_at": row["paid_at"],
        "print_status": row["print_status"],
        "kitchen_status": row["kitchen_status"] or None,
        "ready_at": row["ready_at"],
        "offline_number": row["offline_number"],
    }


def store_order(
    idempotency_key: str, request_hash: str, fields: dict[str, Any]
) -> tuple[sqlite3.Row, bool]:
    """Insert an order once per idempotency key; returns (row, created)."""
    with closing(connect_db()) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM orders WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise HTTPException(
                        status_code=409,
                        detail="Ключ уже использован для другого состава заказа.",
                    )
                connection.commit()
                return existing, False
            store_seq = connection.execute(
                "SELECT COALESCE(MAX(store_seq), 0) + 1 FROM orders WHERE store_id = ?",
                (fields["store_id"],),
            ).fetchone()[0]
            names = ["idempotency_key", "request_hash", "order_number", "mode", "store_seq", *fields]
            values = [idempotency_key, request_hash, "pending", "local", store_seq, *fields.values()]
            cursor = connection.execute(
                f"INSERT INTO orders ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})",
                values,
            )
            connection.execute(
                "UPDATE orders SET order_number = ? WHERE id = ?",
                (f"P-{cursor.lastrowid:05d}", cursor.lastrowid),
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
            "SELECT * FROM orders WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=500, detail="Не удалось получить созданный заказ.")
    return row, True


def parse_idempotency_key(idempotency_key: str | None) -> str:
    if idempotency_key is None:
        raise HTTPException(status_code=400, detail="Отсутствует ключ запроса.")
    try:
        return str(uuid.UUID(idempotency_key))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Некорректный ключ запроса.") from exc


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    initialize_db()
    refresh_catalog()
    from app import admin, queue_screen

    admin.create_owner_from_environment()
    queue_server = queue_screen.start_server()
    yield
    if queue_server is not None:
        await queue_screen.stop_server(*queue_server)


app = FastAPI(
    title="Paprika Self-Service Kiosk",
    version="1.0.0",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def revalidate_app_files(request: Request, call_next):
    """Make browsers check pages, scripts and styles on every load so updates show at once."""
    response = await call_next(request)
    path = request.url.path
    if path in {"/", "/pos", "/admin"} or path.endswith((".html", ".js", ".css")):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/", include_in_schema=False)
def kiosk_home() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/admin", include_in_schema=False)
def admin_home() -> FileResponse:
    return FileResponse(STATIC_DIR / "admin" / "index.html")


@app.get("/pos", include_in_schema=False)
def pos_home() -> FileResponse:
    return FileResponse(STATIC_DIR / "pos" / "index.html")


@app.get("/pos-sw.js", include_in_schema=False)
def pos_service_worker() -> FileResponse:
    # Keeps the register page working without internet (static/pos/sw.js).
    return FileResponse(
        STATIC_DIR / "pos" / "sw.js",
        media_type="text/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/assets/logo.jpg", include_in_schema=False)
def company_logo() -> FileResponse:
    if not LOGO_PATH.is_file():
        raise HTTPException(status_code=404, detail="Файл logo.jpg не найден.")
    return FileResponse(LOGO_PATH, media_type="image/jpeg")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "paprika-kiosk"}


def menu_response(store_id: int) -> MenuResponse:
    catalog = catalog_for(store_id)
    return MenuResponse(
        currency=CURRENCY,
        max_order_total=float(MAX_ORDER_TOTAL),
        categories=catalog.categories,
        products=catalog.products,
    )


@app.get("/api/menu", response_model=MenuResponse)
def get_menu(x_device_key: DeviceKey = None) -> MenuResponse:
    device = stores.require_device(x_device_key, "kiosk")
    return menu_response(device.store_id)


@app.get("/api/device")
def get_device(x_device_key: DeviceKey = None) -> dict[str, Any]:
    """Which store and device this browser is activated as."""
    device = stores.require_device(x_device_key)
    return {
        "id": device.id,
        "kind": device.kind,
        "name": device.name,
        "store_id": device.store_id,
        "store_name": device.store_name,
    }


@app.post("/api/orders", status_code=201)
def create_order(
    payload: OrderInput,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    x_device_key: DeviceKey = None,
) -> dict[str, Any]:
    device = stores.require_device(x_device_key, "kiosk")
    parsed_key = parse_idempotency_key(idempotency_key)
    if payload.service_type is None:
        raise HTTPException(status_code=422, detail="Выберите: в зале или с собой.")
    normalized, total = resolve_order_items(payload, catalog_for(device.store_id))
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
    row, created = store_order(
        parsed_key,
        request_hash,
        {
            "total": total,
            "items_json": json.dumps(normalized, ensure_ascii=False),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "comment": comment,
            "service_type": payload.service_type,
            "source": "kiosk",
            "kitchen_status": "preparing",
            "store_id": device.store_id,
            "device_id": device.id,
        },
    )
    result = public_order(row)
    if not created:
        return result

    try:
        OrderPrinter.print_order(result, device.store_id)
        result["print_status"] = "queued"
        update_print_status(parsed_key, "queued")
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


# The modules below use the helpers above.
from app import printing, stores  # noqa: E402
from app.admin import router as admin_router  # noqa: E402
from app.offline import router as offline_router  # noqa: E402
from app.pos import router as pos_router  # noqa: E402

app.include_router(pos_router)
app.include_router(offline_router)
app.include_router(admin_router)
app.include_router(printing.router)
