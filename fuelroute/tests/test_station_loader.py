from decimal import Decimal

import pytest

from fuelroute.models import FuelStation
from fuelroute.services.places import PlaceIndex
from fuelroute.services.station_loader import LoadReport, load_stations, parse_price_file
from fuelroute.services.text import normalize_place, normalize_state

CSV = """OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price
7,WOODSHED OF BIG CABIN,"I-44, EXIT 283 & US-69",Big Cabin,OK,307,3.00733333
20,PILOT TRAVEL CENTER #1243,"I-8, EXIT 119 & SR-85",Gila Bend,AZ,930,3.899
20,PILOT #1243,"I-8, EXIT 119 & SR-85",Gila Bend,AZ,930,3.799
21,LOVES #1,"I-70, EXIT 1",St. Louis                ,MO,100,3.10
22,MC STOP,"I-95, EXIT 9",Mc Lean,VA,100,3.20
23,NOWHERE STOP,"US-1",Atlantis,FL,100,3.30
24,CANADA STOP,"HWY 401",Toronto,ON,100,3.40
25,BAD ROW,"US-1",Austin,TX,100,not-a-price
"""

PLACES = PlaceIndex(
    [
        ("Big Cabin", "OK", "36.54", "-95.22", "0", "census"),
        ("Gila Bend", "AZ", "32.95", "-112.72", "0", "census"),
        ("Saint Louis", "MO", "38.63", "-90.24", "0", "census"),
        ("McLean", "VA", "38.93", "-77.18", "0", "census"),
    ]
)


@pytest.fixture
def price_file(tmp_path):
    path = tmp_path / "prices.csv"
    path.write_text(CSV, encoding="utf-8")
    return path


def test_parse_merges_duplicate_ids_keeping_the_lowest_price(price_file):
    report = LoadReport()
    stations = parse_price_file(price_file, report)
    assert report.rows_read == 8
    assert report.rows_invalid == 1
    assert len(stations) == 6
    pilot = stations[20]
    assert pilot.price == Decimal("3.7990")
    assert pilot.price_rows == 2
    assert stations[21].city == "St. Louis"  # whitespace cleaned


@pytest.mark.django_db
def test_load_geocodes_offline_and_reports_unmatched(price_file):
    report = load_stations(price_file, PLACES)
    assert report.non_us_skipped == 1
    assert report.geocoded == 4  # "St. Louis" -> Saint Louis, "Mc Lean" -> McLean
    assert report.unmatched == [("Atlantis", "FL")]
    assert FuelStation.objects.count() == 5
    assert FuelStation.objects.filter(latitude__isnull=True).count() == 1
    assert FuelStation.objects.get(opis_id=21).latitude == pytest.approx(38.63)


@pytest.mark.django_db
def test_load_is_idempotent(price_file):
    load_stations(price_file, PLACES)
    load_stations(price_file, PLACES)
    assert FuelStation.objects.count() == 5


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("St. Louis", "saint louis"),
        ("Ste Genevieve", "sainte genevieve"),
        ("Ft Worth", "fort worth"),
        ("O'Fallon", "ofallon"),
        ("S Coffeyville", "south coffeyville"),
        ("  Big   Cabin ", "big cabin"),
    ],
)
def test_normalize_place(raw, expected):
    assert normalize_place(raw) == expected


def test_normalize_state():
    assert normalize_state("tx") == "TX"
    assert normalize_state("Texas") == "TX"
    assert normalize_state("ON") is None
