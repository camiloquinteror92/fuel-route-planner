"""Parse the OPIS fuel price CSV, merge duplicate stations and geocode them offline."""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.db import transaction

from ..models import FuelStation
from .places import PlaceIndex
from .stations import reset_station_arrays
from .text import US_STATES

logger = logging.getLogger(__name__)


@dataclass
class StationRow:
    opis_id: int
    name: str
    address: str
    city: str
    state: str
    rack_id: int | None
    price: Decimal
    price_rows: int = 1


@dataclass
class LoadReport:
    rows_read: int = 0
    rows_invalid: int = 0
    unique_stations: int = 0
    non_us_skipped: int = 0
    geocoded: int = 0
    geocoded_by_source: dict[str, int] = field(default_factory=dict)
    unmatched: list[tuple[str, str]] = field(default_factory=list)


def _clean(value: str | None) -> str:
    return " ".join((value or "").split())


def parse_price_file(path: Path, report: LoadReport) -> dict[int, StationRow]:
    """Read the CSV and keep one row per OPIS ID.

    The file has the same truck stop several times (same ID, same address, slightly
    different name or price: one row per rack/price quote). We keep the LOWEST
    retail price: it is a price that station actually offers.
    """
    stations: dict[int, StationRow] = {}
    with open(path, encoding="utf-8-sig", newline="") as handle:
        for raw in csv.DictReader(handle):
            report.rows_read += 1
            try:
                opis_id = int(_clean(raw["OPIS Truckstop ID"]))
                price = Decimal(_clean(raw["Retail Price"]))
            except (InvalidOperation, ValueError, KeyError):
                report.rows_invalid += 1
                continue
            if price <= 0:
                report.rows_invalid += 1
                continue
            rack = _clean(raw.get("Rack ID"))
            row = StationRow(
                opis_id=opis_id,
                name=_clean(raw["Truckstop Name"]),
                address=_clean(raw["Address"]),
                city=_clean(raw["City"]),
                state=_clean(raw["State"]).upper(),
                rack_id=int(rack) if rack.isdigit() else None,
                price=price.quantize(Decimal("0.0001")),
            )
            existing = stations.get(opis_id)
            if existing is None:
                stations[opis_id] = row
            else:
                existing.price_rows += 1
                if row.price < existing.price:
                    row.price_rows = existing.price_rows
                    stations[opis_id] = row
    report.unique_stations = len(stations)
    return stations


@transaction.atomic
def load_stations(path: Path, places: PlaceIndex) -> LoadReport:
    """Replace the FuelStation table with the content of the price file."""
    report = LoadReport()
    stations = parse_price_file(path, report)

    objects = []
    for row in stations.values():
        if row.state not in US_STATES:
            # The file also lists Canadian truck stops (ON, AB, BC...). The API only
            # routes inside the USA, so they are not useful.
            report.non_us_skipped += 1
            continue
        place = places.lookup(row.city, row.state)
        if place:
            report.geocoded += 1
            report.geocoded_by_source[place.source] = report.geocoded_by_source.get(place.source, 0) + 1
        else:
            report.unmatched.append((row.city, row.state))
        objects.append(
            FuelStation(
                opis_id=row.opis_id,
                name=row.name,
                address=row.address,
                city=row.city,
                state=row.state,
                rack_id=row.rack_id,
                price=row.price,
                price_rows=row.price_rows,
                latitude=place.latitude if place else None,
                longitude=place.longitude if place else None,
                geocode_source=place.source if place else "",
            )
        )

    FuelStation.objects.all().delete()
    FuelStation.objects.bulk_create(objects, batch_size=1000)
    transaction.on_commit(reset_station_arrays)
    logger.info(
        "event=stations_loaded unique=%s geocoded=%s unmatched=%s non_us=%s",
        report.unique_stations, report.geocoded, len(report.unmatched), report.non_us_skipped,
    )
    return report
