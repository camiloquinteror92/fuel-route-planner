"""Turning user input into coordinates: formats, offline index, homonyms, Nominatim."""

import time

import pytest

from fuelroute.services.errors import ExternalServiceError, LocationNotFound, LocationOutsideUSA, NoFuelDataInRegion
from fuelroute.services.geocoding import geocode, parse_coordinates, split_city_state
from fuelroute.services.http import ExternalApiClient
from fuelroute.services.text import foreign_region


class NoNetworkClient:
    """Fails the test if geocoding tries to call Nominatim."""

    calls: list = []
    call_count = 0

    def get_json(self, *args, **kwargs):
        raise AssertionError("unexpected external call")


class FakeNominatim:
    def __init__(self, payload, status=200):
        self.payload, self.status = payload, status
        self.calls = []

    def get_json(self, service, url, params=None, min_interval=0.0):
        self.calls.append(service)
        return self.status, self.payload


def test_parse_coordinates():
    assert parse_coordinates("40.7128,-74.0060") == (40.7128, -74.006)
    assert parse_coordinates(" 40.7 , -74 ") == (40.7, -74.0)
    assert parse_coordinates("40.7128 -74.0060") == (40.7128, -74.006)  # space-separated
    assert parse_coordinates("Austin, TX") is None
    assert parse_coordinates("Austin TX 78701") is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Austin, TX", ("Austin", "TX")),
        ("Austin TX", ("Austin", "TX")),
        ("austin, texas", ("austin", "TX")),
        ("Austin, TX, USA", ("Austin", "TX")),
        ("Albany New York", ("Albany", "NY")),
        ("Charlotte North Carolina", ("Charlotte", "NC")),
        ("Charleston West Virginia", ("Charleston", "WV")),  # not ("Charleston West", "VA")
        ("New York New York", ("New York", "NY")),
        ("Washington, D.C.", ("Washington", "DC")),
        ("Washington DC", ("Washington", "DC")),
        ("Columbus", None),  # not "Colomb" + "US"
        ("Toronto, ON", None),
    ],
)
def test_split_city_state(text, expected):
    assert split_city_state(text) == expected


@pytest.mark.parametrize(
    "text",
    ["Austin, TX", "austin, texas", "Austin TX", "Austin, TX, USA", "Austin Texas"],
)
def test_city_state_is_geocoded_offline(text):
    location = geocode(text, NoNetworkClient(), field="start")
    assert location.geocoder == "offline"
    assert location.latitude == pytest.approx(30.3, abs=0.2)
    assert location.longitude == pytest.approx(-97.75, abs=0.2)


@pytest.mark.parametrize(
    ("text", "lat", "lon"),
    [
        # Homonyms in the same state resolve to the populated / incorporated place.
        ("Mountain View, CA", 37.39, -122.08),  # the city, not the 0.3 sq mi CDP 70 mi north
        ("Marietta, OK", 33.94, -97.12),  # Love County, on I-35
        ("Wilmington, IL", 41.32, -88.16),
        ("Bel Air, MD", 39.54, -76.35),
        ("Bronx, NY", 40.85, -73.87),  # the dataset name is "The Bronx"
        ("Albany New York", 42.67, -73.80),
        ("Washington, D.C.", 38.90, -77.02),
        ("Saint Louis, MO", 38.64, -90.25),
        ("St. Louis, MO", 38.64, -90.25),
    ],
)
def test_offline_lookups(text, lat, lon):
    location = geocode(text, NoNetworkClient(), field="start")
    assert (location.latitude, location.longitude) == (pytest.approx(lat, abs=0.05), pytest.approx(lon, abs=0.05))


def test_region_is_attached():
    assert geocode("Anchorage, AK", NoNetworkClient(), field="start").region == "alaska"
    assert geocode("Honolulu, HI", NoNetworkClient(), field="start").region == "hawaii"
    assert geocode("Austin, TX", NoNetworkClient(), field="start").region == "lower48"


def test_free_text_falls_back_to_nominatim_once_and_is_cached():
    client = FakeNominatim([{"lat": "40.7484", "lon": "-73.9857", "display_name": "Empire State Building"}])
    first = geocode("Empire State Building", client, field="start")
    second = geocode("Empire State Building", client, field="start")
    assert first.geocoder == "nominatim"
    assert second.latitude == first.latitude
    assert client.calls == ["nominatim"]


def test_unknown_place_raises_and_the_miss_is_cached_briefly():
    client = FakeNominatim([])
    for _ in range(2):
        with pytest.raises(LocationNotFound):
            geocode("Nowhere at all", client, field="start")
    assert client.calls == ["nominatim"]


def test_nominatim_error_status_is_an_upstream_error_and_not_cached():
    # Regression: a 403 (blocked) used to be answered as "location not found" and
    # remembered for 24 h.
    with pytest.raises(ExternalServiceError):
        geocode("1600 Pennsylvania Ave, Washington", FakeNominatim([], status=403), field="start")
    # Nothing was cached: the next request asks Nominatim again and succeeds.
    ok = FakeNominatim([{"lat": "38.8977", "lon": "-77.0365", "display_name": "White House"}])
    assert geocode("1600 Pennsylvania Ave, Washington", ok, field="start").geocoder == "nominatim"
    assert ok.calls == ["nominatim"]


def test_text_without_letters_is_not_sent_to_nominatim():
    with pytest.raises(LocationNotFound):
        geocode("???", NoNetworkClient(), field="start")


def test_coordinates_outside_usa_raise():
    for text in ("48.85,2.35", "43.6532,-79.3832", "25.6866,-100.3161"):  # Paris, Toronto, Monterrey
        with pytest.raises(LocationOutsideUSA):
            geocode(text, NoNetworkClient(), field="finish")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("ON", ("canada", "Ontario")),
        ("bc", ("canada", "British Columbia")),
        ("Quebec", ("canada", "Quebec")),
        ("NL", ("canada", "Newfoundland and Labrador")),  # also Nuevo Leon: outside the USA either way
        ("Nuevo León", ("mexico", "Nuevo Leon")),
        ("CDMX", ("mexico", "Ciudad de Mexico")),
        ("México", ("mexico", "Mexico")),
        ("PR", ("us_territory", "Puerto Rico")),
        ("TX", None),
        ("MO", None),  # Missouri, never Morelos
        ("Texas", None),
        ("Springfield", None),
    ],
)
def test_foreign_region(value, expected):
    assert foreign_region(value) == expected


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("Toronto, ON", LocationOutsideUSA),
        ("Calgary AB", LocationOutsideUSA),
        ("Guadalajara, Jalisco", LocationOutsideUSA),
        ("Ponce, PR", NoFuelDataInRegion),
        ("Austin, TZ", LocationNotFound),
    ],
)
def test_regions_outside_the_states_are_rejected_without_a_lookup(text, error):
    with pytest.raises(error) as raised:
        geocode(text, NoNetworkClient(), field="finish")
    assert raised.value.details["field"] == "finish"


def test_free_text_that_only_looks_foreign_still_reaches_the_geocoder():
    # No comma and not an upper-case code: "Lake Ontario" is free text, not "Lake" in Ontario.
    client = FakeNominatim([{"lat": "43.16", "lon": "-77.61", "category": "natural", "display_name": "Lake Ontario"}])
    assert geocode("Lake Ontario", client, field="start").geocoder == "nominatim"
    assert geocode("Mexico, MO", NoNetworkClient(), field="start").geocoder == "offline"  # Mexico, Missouri


def test_nominatim_calls_are_spaced(upstream, settings, monkeypatch):
    # Nominatim's policy: at most 1 request per second. Two different free-text
    # inputs in a row must not hit it back to back.
    monkeypatch.setitem(settings.FUEL_PLANNER, "NOMINATIM_MIN_INTERVAL_SECONDS", 0.3)
    upstream.respond((200, [{"lat": "40.7", "lon": "-74.0", "display_name": "x"}]))
    client = ExternalApiClient()
    started = time.perf_counter()
    geocode("Empire State Building", client, field="start")
    geocode("Statue of Liberty", client, field="finish")
    assert time.perf_counter() - started >= 0.3
    assert client.calls == ["nominatim", "nominatim"]
