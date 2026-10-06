"""Loading the price file: parsing, duplicates and price policy, offline geocoding,
homonym resolution, and the data version that running servers watch."""

from decimal import Decimal

import pytest

from fuelroute.models import FuelStation
from fuelroute.services.places import PlaceIndex
from fuelroute.services.station_loader import LoadReport, highway_refs, load_stations, parse_price_file
from fuelroute.services.stations import data_version, get_station_arrays
from fuelroute.services.text import normalize_place, normalize_state

CSV = """OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price
7,WOODSHED OF BIG CABIN,"I-44, EXIT 283 & US-69",Big Cabin,OK,307,3.00733333
20,PILOT TRAVEL CENTER #1243,"I-8, EXIT 119 & SR-85",Gila Bend,AZ,930,3.899
20,PILOT #1243,"I-8, EXIT 119 & SR-85",Gila Bend,AZ,930,3.799
20,PILOT #1243,"I-8, EXIT 119 & SR-85",Gila Bend,AZ,930,4.299
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


def test_parse_keeps_every_quote_of_a_duplicated_station(price_file):
    report = LoadReport()
    stations = parse_price_file(price_file, report)
    assert report.rows_read == 9
    assert report.rows_invalid == 1
    assert len(stations) == 6
    pilot = stations[20]
    assert pilot.prices == [Decimal("3.8990"), Decimal("3.7990"), Decimal("4.2990")]
    assert pilot.price("median") == Decimal("3.8990")
    assert pilot.price("min") == Decimal("3.7990")
    assert stations[7].prices == [Decimal("3.0073")]  # 8 decimals in the file -> 4
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
def test_load_stores_the_policy_price_and_the_spread(price_file):
    load_stations(price_file, PLACES)  # median by default
    pilot = FuelStation.objects.get(opis_id=20)
    assert (pilot.price, pilot.price_min, pilot.price_max, pilot.price_rows) == (
        Decimal("3.8990"), Decimal("3.7990"), Decimal("4.2990"), 3,
    )
    load_stations(price_file, PLACES, price_policy="min")
    assert FuelStation.objects.get(opis_id=20).price == Decimal("3.7990")


@pytest.mark.django_db
def test_load_is_idempotent(price_file):
    load_stations(price_file, PLACES)
    load_stations(price_file, PLACES)
    assert FuelStation.objects.count() == 5


@pytest.mark.django_db
def test_reload_changes_the_data_version_and_running_processes_notice(price_file):
    load_stations(price_file, PLACES)
    before = data_version()
    arrays = get_station_arrays()
    assert sorted(arrays.opis_ids.tolist()) == [7, 20, 21, 22]  # keyed by the stable OPIS id
    load_stations(price_file, PLACES)  # new rows, new primary keys
    assert data_version() != before
    # A server process that still holds the old arrays reloads them on its next use
    # (the planner passes the current version), without a restart.
    assert get_station_arrays(data_version()) is not arrays


# --- homonyms -------------------------------------------------------------------------------

HOMONYM_CSV = """OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price
1,ANCHOR,"I-24, EXIT 64",La Vergne,TN,1,3.00
2,TA ANTIOCH,"I-24, EXIT 62",Antioch,TN,1,3.00
3,NO CLUE,"CR-12",Antioch,TN,1,3.00
4,BIG CITY STOP,"CR-5",Chesapeake,VA,1,3.00
5,CONFLICT,"I-64, EXIT 290",Chesapeake,VA,1,3.00
6,ANCHOR 2,"I-64, EXIT 292",Hampton,VA,1,3.00
"""

HOMONYM_PLACES = PlaceIndex(
    [
        # Two places named Antioch in Tennessee, 130 miles apart, both population 0.
        ("Antioch", "TN", "35.79", "-88.97", "0", "geonames"),
        ("Antioch", "TN", "36.06", "-86.67", "0", "geonames"),
        ("La Vergne", "TN", "36.02", "-86.56", "38000", "census"),
        # Chesapeake: a city of 235k and a hamlet with the same name.
        ("Chesapeake", "VA", "36.68", "-76.30", "235429", "census"),
        ("Chesapeake", "VA", "37.10", "-76.35", "0", "geonames"),  # 29 mi north, next to Hampton
        ("Hampton", "VA", "37.10", "-76.34", "137000", "census"),
    ]
)


@pytest.mark.django_db
def test_homonyms_are_resolved_from_the_address_or_left_out(tmp_path):
    path = tmp_path / "prices.csv"
    path.write_text(HOMONYM_CSV, encoding="utf-8")
    report = load_stations(path, HOMONYM_PLACES)
    located = {s.opis_id: (s.latitude, s.longitude) for s in FuelStation.objects.all()}

    # Exit 62 is two exits from the La Vergne station on I-24: the Nashville Antioch.
    assert located[2] == (pytest.approx(36.06), pytest.approx(-86.67))
    # Same city name, no highway in common with any other station, no population
    # clue: not guessed (it could be 130 miles off), reported as ambiguous.
    assert located[3] == (None, None)
    # One candidate is a city of 235k, the other a hamlet: the city.
    assert located[4] == (pytest.approx(36.68), pytest.approx(-76.30))
    # The exit clue (next to Hampton) and the population clue (the city) disagree:
    # left out rather than guessed.
    assert located[5] == (None, None)
    assert report.homonyms_resolved == 2
    assert sorted(report.ambiguous) == [("Antioch", "TN"), ("Chesapeake", "VA")]


def test_highway_refs():
    assert highway_refs("I-24, EXIT 62 & US-41") == ({"I-24", "US-41"}, ("I-24", 62))
    assert highway_refs("SR-54/SR-55") == ({"SR-54", "SR-55"}, None)
    assert highway_refs("I-29 & I-80, EXIT 3") == ({"I-29", "I-80"}, ("I-29", 3))
    assert highway_refs("HWY 401") == ({"SR-401"}, None)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("St. Louis", "saint louis"),
        ("Ste Genevieve", "sainte genevieve"),
        ("Ft Worth", "fort worth"),
        ("O'Fallon", "ofallon"),
        ("S Coffeyville", "south coffeyville"),
        ("  Big   Cabin ", "big cabin"),
        ("The Bronx", "bronx"),
    ],
)
def test_normalize_place(raw, expected):
    assert normalize_place(raw) == expected


def test_normalize_state():
    assert normalize_state("tx") == "TX"
    assert normalize_state("Texas") == "TX"
    assert normalize_state("D.C.") == "DC"
    assert normalize_state("ON") is None
