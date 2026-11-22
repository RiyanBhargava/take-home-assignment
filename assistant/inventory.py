import hashlib
import re
import unicodedata
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from urllib.parse import urlsplit

from openpyxl import load_workbook

from assistant.models import Filters, InventoryResponse, Listing

_AMOUNT = r"(?:\d{1,3}(?:[, ]\d{3})+|\d+)(?:\.\d{2})?"
_AED = rf"(?:AED\s*(?P<before>{_AMOUNT})|(?P<after>{_AMOUNT})\s*AED)\b"
_PRICE = re.compile(
    rf"^\s*{_AED}\s*(?:/-)?\s*$|"
    rf"(?:\b(?:selling price|asking price|price reduced|price|payment|summer offer)\s*[:#\u2013-]?\s*"
    rf"(?P<label>AED\s*{_AMOUNT}|{_AMOUNT}\s*AED)\b)|"
    rf"(?P<cash>AED\s*{_AMOUNT}|{_AMOUNT}\s*AED)\s+(?:in\s+)?cash\b|"
    rf"(?P<finance>AED\s*{_AMOUNT}|{_AMOUNT}\s*AED)\s+(?:or|/)\s+(?:AED\s+)?\d",
    re.I,
)
_INSTALLMENT = re.compile(r"^\s*(?:/|\bper\b)?\s*(?:months?\b|monthly\b|p[./ ]?m\b)", re.I)
_DISTANCE = r"(?P<value>\d[\d, ]*)(?=\s*(?:kms?|kilomet(?:er|re)s?)\b)"
_MILEAGE = re.compile(
    rf"\b(?:mileage|odometer|kilomet(?:er|re)s)\s*[:\u2013-]?\s*{_DISTANCE}", re.I
)
_MILEAGE_LINE = re.compile(
    rf"^[\s•*\u2013-]*{_DISTANCE}\s*(?:kms?|kilomet(?:er|re)s?)[!\s.]*$", re.I
)
_MILEAGE_SHORT = re.compile(r"\b(?:mileage|odometer)\s*[:\u2013-]?\s*(\d{1,3})k\s*km\b", re.I)
_COLOR = r"white|black|silver|grey|gray|blue|red|green|brown|beige|gold|yellow|orange|purple|bronze"
_BODY = re.compile(
    r"\b(?:suv|crossover|sedan|saloon|hatchback|coupe|convertible|cabriolet|pickup|wagon|minivan|van)\b"
)
_ALIASES = {
    "mercedes": "mercedes benz",
    "vw": "volkswagen",
    "gray": "grey",
    "saloon": "sedan",
    "cabriolet": "convertible",
    "pick up": "pickup",
    "four wheel drive": "4wd",
    "four wheel": "4wd",
    "reverse camera": "reversing camera",
    "backup camera": "reversing camera",
    "rear view camera": "reversing camera",
    "rear camera": "reversing camera",
    "pano roof": "panoramic sunroof",
    "panoramic roof": "panoramic sunroof",
}
_MANAGED_MARKERS = (
    "buy this car with confidence from dubizzle cars",
    "we manage the interaction between the buyer and seller",
    "dubizzle managed car",
)


def normalize(value: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return " ".join(re.findall(r"[a-z0-9]+", ascii_text.lower()))


def phrase(text: str, value: str) -> bool:
    return bool(value) and f" {value} " in f" {text} "


def canonical(value: str) -> str:
    normalized = normalize(value)
    return _ALIASES.get(normalized, normalized)


def _cell(value: object) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip() if value is not None else ""


def _plain(value: str) -> str:
    value = re.sub(r"(?i)<br\s*/?>|</p>|</li>", "\n", unescape(value))
    return re.sub(r"<[^>]*>", "", value).replace("\ufffd", "")


def _integer(value: str, upper: int) -> int | None:
    number = value.replace(",", "").replace(" ", "")
    if not re.fullmatch(r"\d+(?:\.0+)?", number):
        return None
    result = int(number.split(".")[0])
    return result if 0 <= result <= upper else None


def _price(lines: list[str]) -> int | None:
    prices = set()
    title = re.search(rf"\bfor\s+({_AMOUNT})\s*AED\s*$", lines[0], re.I)
    if title:
        value = _integer(title[1], 100_000_000)
        if value is not None:
            prices.add(value)
    for line in (part for source in lines for part in source.split("|")):
        for match in _PRICE.finditer(line):
            if _INSTALLMENT.match(line[match.end() :]):
                continue
            amount = re.search(_AMOUNT, match.group()).group()
            value = _integer(amount, 100_000_000)
            if value is not None:
                prices.add(value)
    return next(iter(prices)) if len(prices) == 1 else None


def _mileage(title: str, lines: list[str]) -> int | None:
    for line in lines:
        match = _MILEAGE.search(line) or _MILEAGE_LINE.fullmatch(line)
        if match:
            return _integer(match["value"], 2_000_000)
        shorthand = _MILEAGE_SHORT.search(line)
        if shorthand:
            return int(shorthand[1]) * 1000
    return 0 if re.search(r"\b0\s*kms?\b", title, re.I) else None


def _warranty(lines: list[str]) -> str | None:
    for line in lines:
        for part in re.split(r"\||[.!?]\s+", line):
            if re.search(r"\bwarrant(?:y|ies)\b", part, re.I):
                # Avoid displaying boilerplate paragraphs and unbounded seller copy.
                part = part.strip(" \t•-")
                if 7 <= len(part) <= 150 and not re.search(r"https?://|\d{8,}", part):
                    return part
    return None


def _color(lines: list[str]) -> str | None:
    for line in lines:
        match = re.search(
            rf"\b(?:exterior(?: colou?r)?|colou?r)\s*[:\u2013-]?\s*({_COLOR})\b|"
            rf"\b({_COLOR})\s+(?:exterior|colou?r|with .{{0,24}}interior)\b",
            line,
            re.I,
        )
        if match:
            return canonical(match[1] or match[2]).title()
    return None


def _photo(value: str) -> str | None:
    try:
        url = urlsplit(value)
    except ValueError:
        return None
    return value if url.scheme == "https" and url.hostname == "dbz-images.dubizzle.com" else None


def _feature_match(text: str, feature: str) -> bool:
    target = canonical(feature)
    variants = [target, *(key for key, value in _ALIASES.items() if value == target)]
    return any(
        phrase(text, variant)
        and not any(phrase(text, f"{prefix} {variant}") for prefix in ("no", "without", "lacks"))
        for variant in variants
    )


@dataclass(frozen=True)
class InventoryRecord:
    listing: Listing
    text: str


class Inventory:
    def __init__(self, records: list[InventoryRecord]):
        self.records = {row.listing.id: row for row in records}
        self.makes = sorted({row.listing.make for row in records})

    @classmethod
    def load(cls, path: Path, sheet_name: str) -> "Inventory":
        if not path.is_file():
            raise ValueError(f"Inventory file {path} is missing. Set INVENTORY_PATH in .env.")
        book = load_workbook(path, read_only=True, data_only=True)
        try:
            if sheet_name not in book.sheetnames:
                raise ValueError(
                    f"Inventory sheet {sheet_name!r} is missing. Found {book.sheetnames}."
                )
            rows = book[sheet_name].values
            columns = {
                normalize(_cell(value)).replace(" ", "_"): i
                for i, value in enumerate(next(rows, ()))
                if value is not None
            }
            if missing := {"make", "model", "year", "title", "description"} - columns.keys():
                raise ValueError(f"Missing inventory columns: {', '.join(sorted(missing))}.")
            records = []
            for row in rows:
                values = {key: _cell(row[i]) for key, i in columns.items() if i < len(row)}
                if not any(values.values()):
                    continue
                records.append(_record(values, sheet_name))
        finally:
            book.close()
        if not records:
            raise ValueError(f"Inventory sheet {sheet_name!r} contains no listings.")
        if len({record.listing.id for record in records}) != len(records):
            raise ValueError(f"Inventory sheet {sheet_name!r} contains duplicate listing IDs.")
        return cls(records)

    def get(self, listing_id: str | None) -> Listing | None:
        if record := self.records.get(listing_id or ""):
            return record.listing
        return None

    def matching_features(self, listing_id: str, features: list[str]) -> list[str]:
        return [
            feature
            for feature in features
            if _feature_match(self.records[listing_id].text, feature)
        ]

    def search(self, filters: Filters, limit: int = 6, offset: int = 0) -> InventoryResponse:
        if filters.currency and filters.currency.upper() != "AED":
            raise ValueError("The inventory uses AED. Please provide a budget in AED.")
        matches = [record.listing for record in self.records.values() if _matches(record, filters)]
        if filters.sort == "price_asc":
            matches.sort(key=lambda car: (car.price_aed is None, car.price_aed or 0))
        elif filters.sort == "year_desc":
            matches.sort(key=lambda car: -car.year)
        elif filters.sort == "mileage_asc":
            matches.sort(key=lambda car: (car.mileage_km is None, car.mileage_km or 0))
        return InventoryResponse(total=len(matches), listings=matches[offset : offset + limit])


def _record(values: dict[str, str], sheet: str) -> InventoryRecord:
    make, model, year = (values.get(key, "") for key in ("make", "model", "year"))
    year_value = _integer(year, 2100)
    if not make or not model or year_value is None or year_value < 1886:
        raise ValueError(
            f"Invalid make, model or year in inventory row {values.get('title', '')!r}."
        )
    title = " ".join(_plain(values.get("title", "")).split())
    description = _plain(values.get("description", ""))
    full_text = normalize(description)
    description = re.split(
        r"(?i)\bwhat (?:is dubizzle cars|to expect from dubizzle cars)\b|\bwhy choose\b",
        description,
        maxsplit=1,
    )[0]
    lines = [title, *description.splitlines()]
    text = normalize(" ".join(lines))
    body = _BODY.search(text)
    managed_value = normalize(values.get("managed", values.get("dubizzle_managed", "")))
    managed = (
        managed_value in {"yes", "true", "1"}
        if managed_value
        else any(phrase(full_text, marker) for marker in _MANAGED_MARKERS)
    )
    identity = (
        values.get("listing_id") or values.get("photo_url") or f"{year}|{make}|{model}|{title}"
    )
    # Keep listing references stable across restarts and distinct across workbook sheets.
    listing_id = hashlib.sha256(f"{sheet}|{identity}".encode()).hexdigest()[:12]
    structured_price = _integer(values.get("price_aed") or values.get("price", ""), 100_000_000)
    return InventoryRecord(
        Listing(
            id=listing_id,
            make={"bmw": "BMW", "byd": "BYD", "gwm": "GWM", "jac": "JAC"}.get(
                make.lower(), make.title()
            ),
            model=model.upper()
            if normalize(model) in {"cr v", "hr v", "cx 5", "f430"}
            else model.title(),
            trim=values.get("trim") or None,
            year=year_value,
            title=title,
            photo_url=_photo(values.get("photo_url", "")),
            price_aed=structured_price if structured_price is not None else _price(lines),
            mileage_km=_mileage(title, lines),
            warranty=_warranty(lines),
            color=_color(lines),
            body=canonical(body[0]).upper()
            if body and body[0] == "suv"
            else canonical(body[0])
            if body
            else None,
            managed=managed,
        ),
        text,
    )


def _matches(record: InventoryRecord, filters: Filters) -> bool:
    car = record.listing
    for expected, actual in (
        (filters.make, car.make),
        (filters.model, car.model),
        (filters.trim, car.trim),
        (filters.color, car.color),
        (filters.body, car.body),
    ):
        if expected and not phrase(canonical(actual or ""), canonical(expected)):
            return False
    for bound, value, lower in (
        (filters.min_year, car.year, True),
        (filters.max_year, car.year, False),
        (filters.min_price, car.price_aed, True),
        (filters.max_price, car.price_aed, False),
        (filters.max_mileage_km, car.mileage_km, False),
    ):
        if bound is not None and (value is None or (value < bound if lower else value > bound)):
            return False
    if filters.managed is not None and car.managed != filters.managed:
        return False
    return all(_feature_match(record.text, feature) for feature in filters.features or [])
