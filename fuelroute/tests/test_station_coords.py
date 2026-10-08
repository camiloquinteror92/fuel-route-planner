"""Exact station positions (data/station_coords.csv) in load_stations: an exact row beats
the city center, places a station whose city could not be placed, and is ignored when it
cannot be trusted. Without the file everything is as before. All offline."""

import csv
import io
import socket

import pytest
from django.conf import settings as project_settings
from django.core.management import call_command

from fuelroute.models import FuelStation
from fuelroute.services.places import get_place_index
from fuelroute.services.station_loader import (
    EXACT_MAX_MILES_FROM_CITY,
    EXACT_SOURCES,
    LoadReport,
    load_stations,
    read_station_coords,
)
from fuelroute.services.stations import get_station_arrays

from .test_station_loader import CSV, HOMONYM_CSV, HOMONYM_PLACES, PLACES, price_file  # noqa: F401  (a fixture)

HEADER = "opis_id,lat,lon,source,confidence,osm_type_id,matched_label,miles_from_city_center,reason\n"

# Against the stations of test_station_loader.CSV and the cities of its PLACES.
COORDS = HEADER + (
    # Big Cabin, OK (city 36.54, -95.22): the I-44 exit 283, 1.5 mi away.
    "7,36.561172,-95.217881,osm_exit,high,node/602949924,I-44 exit 283,1.75,exit match\n"
    # Gila Bend, AZ (city 32.95, -112.72): the Pilot itself.
    "20,32.930327,-112.673335,osm_fuel,high,node/12099611220,Pilot,3.48,name match\n"
    # St. Louis, MO (city 38.63, -90.24): 46 mi north, further than any match can be.
    "21,39.300000,-90.240000,osm_exit,high,node/1,I-70 exit 1,46.00,exit match\n"
    # Atlantis, FL: the city is in no place list, so it has no city center.
    "23,26.590000,-80.100000,osm_exit,medium,node/2,I-95 exit 61,0.00,exit match\n"
    # Toronto, ON (not loaded) and an id that is not in the price file.
    "24,43.650000,-79.380000,osm_fuel,high,node/3,Esso,0.50,name match\n"
    "999,35.000000,-97.000000,osm_fuel,high,node/4,Shell,0.50,name match\n"
    # Rows that cannot be trusted.
    "30,abc,-90.0,osm_fuel,high,node/5,x,0,x\n"
    "31,35.0,-90.0,nominatim,high,node/6,x,0,x\n"
    "32,95.0,-90.0,osm_exit,high,node/7,x,0,x\n"
    # McLean, VA twice, at two different places: neither is used.
    "22,38.930000,-77.180000,osm_fuel,high,node/8,Exxon,0.10,name match\n"
    "22,38.990000,-77.300000,osm_exit,high,node/9,I-495 exit 45,7.00,exit match\n"
)


def _write(tmp_path, text, name="station_coords.csv"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _table():
    return list(FuelStation.objects.order_by("opis_id").values_list("opis_id", "latitude", "longitude", "geocode_source"))


@pytest.fixture
def coords_file(tmp_path):
    return _write(tmp_path, COORDS)


@pytest.mark.django_db
def test_an_exact_row_beats_the_city_center(price_file, coords_file):
    load_stations(price_file, PLACES, coords_path=coords_file)
    stations = {s.opis_id: s for s in FuelStation.objects.all()}
    assert (stations[7].latitude, stations[7].longitude, stations[7].geocode_source) == (
        pytest.approx(36.561172), pytest.approx(-95.217881), "osm_exit",
    )
    assert (stations[20].latitude, stations[20].longitude, stations[20].geocode_source) == (
        pytest.approx(32.930327), pytest.approx(-112.673335), "osm_fuel",
    )
    # No (usable) row: the city center, as before.
    assert (stations[22].latitude, stations[22].longitude, stations[22].geocode_source) == (
        pytest.approx(38.93), pytest.approx(-77.18), "census",
    )
    # The planner sees the exact positions.
    arrays = get_station_arrays()
    at = dict(zip(arrays.opis_ids.tolist(), zip(arrays.lat.tolist(), arrays.lon.tolist())))
    assert at[7] == (pytest.approx(36.561172), pytest.approx(-95.217881))


@pytest.mark.django_db
def test_without_the_file_everything_is_as_before(price_file, tmp_path):
    before = load_stations(price_file, PLACES)  # no coords_path: what the tests above always did
    table = _table()
    report = load_stations(price_file, PLACES, coords_path=tmp_path / "missing.csv")
    assert _table() == table
    assert (report.geocoded, report.geocoded_by_source, report.unmatched) == (
        before.geocoded, before.geocoded_by_source, before.unmatched,
    )
    assert (report.coords_file, report.coords_rows, report.exact) == ("", 0, 0)
    assert {source for *_, source in table} == {"census", ""}


@pytest.mark.django_db
def test_a_station_without_a_city_is_placed_by_its_exact_row(price_file, coords_file):
    assert load_stations(price_file, PLACES).not_geocoded == 1  # Atlantis, FL: left out
    report = load_stations(price_file, PLACES, coords_path=coords_file)
    atlantis = FuelStation.objects.get(opis_id=23)
    assert (atlantis.latitude, atlantis.longitude, atlantis.geocode_source) == (
        pytest.approx(26.59), pytest.approx(-80.10), "osm_exit",
    )
    assert report.unmatched == [("Atlantis", "FL")]  # its city is still unknown...
    assert (report.exact_without_city, report.not_geocoded) == (1, 0)  # ...but it is on the map
    assert 23 in get_station_arrays().opis_ids.tolist()


@pytest.mark.django_db
def test_geocode_source_says_where_each_position_comes_from(price_file, coords_file):
    report = load_stations(price_file, PLACES, coords_path=coords_file)
    assert report.geocoded_by_source == {"osm_exit": 2, "osm_fuel": 1, "census": 2}
    assert (report.geocoded, report.exact) == (5, 3)
    assert dict(FuelStation.objects.values_list("opis_id", "geocode_source")) == {
        7: "osm_exit", 20: "osm_fuel", 21: "census", 22: "census", 23: "osm_exit",
    }
    field = FuelStation._meta.get_field("geocode_source")
    assert all(len(source) <= field.max_length for source in EXACT_SOURCES)


@pytest.mark.django_db
def test_rows_that_cannot_be_trusted_are_ignored_and_reported(price_file, coords_file):
    report = load_stations(price_file, PLACES, coords_path=coords_file)
    assert report.coords_file == str(coords_file)
    assert report.coords_rows == 6  # 7, 20, 21, 23, 24, 999
    assert report.coords_invalid == 5  # a bad number, an unknown source, a latitude of 95, McLean twice
    assert report.coords_unused == 2  # Toronto (not loaded) and 999 (not in the price file)
    # 46 mi from St. Louis: from another version of the files. It keeps its city center.
    assert report.coords_rejected == [21]
    st_louis = FuelStation.objects.get(opis_id=21)
    assert (st_louis.latitude, st_louis.geocode_source) == (pytest.approx(38.63), "census")
    assert EXACT_MAX_MILES_FROM_CITY == 30.0


@pytest.mark.django_db
def test_an_ambiguous_city_is_placed_only_near_one_of_its_homonyms(tmp_path):
    prices = _write(tmp_path, HOMONYM_CSV, "prices.csv")
    coords = _write(tmp_path, HEADER + (
        # Antioch, TN (two places 130 mi apart): 3 mi from the Nashville one.
        "3,36.030000,-86.630000,osm_exit,medium,node/1,I-24 exit 60,3.00,exit match\n"
        # Chesapeake, VA (ambiguous, see test_station_loader): 100 mi from both homonyms.
        "5,37.900000,-77.900000,osm_exit,medium,node/2,I-64 exit 290,100.00,exit match\n"
    ))
    report = load_stations(prices, HOMONYM_PLACES, coords_path=coords)
    antioch = FuelStation.objects.get(opis_id=3)
    assert (antioch.latitude, antioch.longitude, antioch.geocode_source) == (
        pytest.approx(36.03), pytest.approx(-86.63), "osm_exit",
    )
    assert FuelStation.objects.get(opis_id=5).latitude is None
    assert report.coords_rejected == [5]
    assert report.exact_without_city == 1


@pytest.mark.django_db
def test_a_file_without_the_needed_columns_is_an_error_and_changes_nothing(price_file, tmp_path):
    load_stations(price_file, PLACES)
    table = _table()
    broken = _write(tmp_path, "id,latitude,longitude\n7,36.5,-95.2\n")
    with pytest.raises(ValueError, match="missing column"):
        load_stations(price_file, PLACES, coords_path=broken)
    assert _table() == table


def test_read_station_coords_without_a_file():
    report = LoadReport()
    assert read_station_coords(None, report) == {}
    assert report.coords_file == ""


# --- the committed files, end to end -----------------------------------------------------------


@pytest.fixture
def no_sockets(monkeypatch):
    """Any attempt to open a connection fails the test (the loader reads files only)."""

    def refuse(*args, **kwargs):
        raise AssertionError("load_stations tried to reach the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


@pytest.mark.django_db
def test_the_committed_coords_file_fits_the_committed_price_file(no_sockets):
    config = project_settings.FUEL_PLANNER
    coords_path = config["STATION_COORDS_FILE"]
    with open(coords_path, encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    load_stations(config["FUEL_PRICES_FILE"], get_place_index())  # city centers only
    placed_before = set(FuelStation.objects.filter(latitude__isnull=False).values_list("opis_id", flat=True))
    report = load_stations(config["FUEL_PRICES_FILE"], get_place_index(), coords_path=coords_path)

    # Every row is valid, belongs to a US station of the price file and is used.
    assert (report.coords_rows, report.coords_invalid, report.coords_unused) == (len(rows), 0, 0)
    assert report.coords_rejected == []
    assert report.exact == len(rows)
    by_source = {source: sum(row["source"] == source for row in rows) for source in EXACT_SOURCES}
    assert {source: report.geocoded_by_source.get(source, 0) for source in EXACT_SOURCES} == by_source
    # Exact rows only add stations: every station placed before is still placed.
    placed = set(FuelStation.objects.filter(latitude__isnull=False).values_list("opis_id", flat=True))
    assert placed >= placed_before
    assert len(placed - placed_before) == report.exact_without_city
    assert report.not_geocoded == len(report.ambiguous) + len(report.unmatched) - report.exact_without_city
    assert FuelStation.objects.filter(geocode_source__in=EXACT_SOURCES).count() == len(rows)


@pytest.mark.django_db
def test_the_command_reports_exact_positions_and_can_ignore_them(price_file, coords_file):
    out = io.StringIO()
    call_command("load_stations", file=str(price_file), coords=str(coords_file), stdout=out)
    out = out.getvalue()
    assert "exact position:     3  (osm_exit 2, osm_fuel 1)" in out
    assert "ignored, too far from the station's city: 1 (opis_id 21)" in out
    assert "Left without coordinates: 0" in out
    assert FuelStation.objects.filter(geocode_source__in=EXACT_SOURCES).count() == 3

    out = io.StringIO()
    call_command("load_stations", file=str(price_file), no_coords=True, stdout=out)
    assert "exact position:     0" in out.getvalue()
    assert not FuelStation.objects.filter(geocode_source__in=EXACT_SOURCES).exists()


@pytest.mark.django_db
def test_a_row_without_a_city_reference_must_be_in_the_usa(price_file, tmp_path):
    # Atlantis, FL is in no place list: its row cannot be checked against a city, so it
    # must at least fall inside the US outline. Toronto does not.
    outside = _write(tmp_path, HEADER + "23,43.650000,-79.380000,osm_exit,high,node/2,x,0.00,x\n")
    report = load_stations(price_file, PLACES, coords_path=outside)
    assert report.coords_rejected == [23]
    assert FuelStation.objects.get(opis_id=23).latitude is None
    inside = _write(tmp_path, HEADER + "23,26.590000,-80.100000,osm_exit,high,node/2,x,0.00,x\n", "inside.csv")
    report = load_stations(price_file, PLACES, coords_path=inside)
    assert (report.coords_rejected, report.exact_without_city) == ([], 1)


@pytest.mark.django_db
def test_a_coords_file_named_on_the_command_line_must_exist(price_file, tmp_path):
    from django.core.management.base import CommandError

    load_stations(price_file, PLACES)
    table = _table()
    with pytest.raises(CommandError, match="No exact positions file"):
        call_command("load_stations", file=str(price_file), coords=str(tmp_path / "typo.csv"), stdout=io.StringIO())
    assert _table() == table  # nothing was loaded


@pytest.mark.django_db
def test_without_the_default_coords_file_the_command_warns_and_uses_city_centers(price_file, tmp_path, settings):
    settings.FUEL_PLANNER = {**settings.FUEL_PLANNER, "STATION_COORDS_FILE": tmp_path / "absent.csv"}
    out = io.StringIO()
    call_command("load_stations", file=str(price_file), stdout=out)
    assert "No exact positions file at" in out.getvalue()
    assert not FuelStation.objects.filter(geocode_source__in=EXACT_SOURCES).exists()
