"""GET /api/places: the type-ahead of US places, from the offline index the planner uses.

Prefix of the normalized name (accents, "St." = "Saint", case), an optional state after
a comma, most populated first, one row per (name, state), 0 external calls.
"""

import statistics
import time

import pytest
from rest_framework.test import APIClient

from fuelroute.services.geocoding import geocode
from fuelroute.services.http import ExternalApiClient
from fuelroute.services.places import PlaceIndex, get_place_index, parse_place_query

ROWS = [  # (name, state, lat, lon, population, source): grouped by name+state, best first
    ("Chicago", "IL", 41.88, -87.63, 2_700_000, "census"),
    ("Chicago Heights", "IL", 41.51, -87.64, 30_000, "census"),
    ("Chico", "CA", 39.73, -121.84, 120_000, "census"),
    ("Chico", "TX", 33.30, -97.80, 1_000, "geonames"),
    ("Chillicothe", "OH", 39.33, -82.98, 22_000, "census"),
    ("San Jose", "CA", 37.34, -121.89, 1_000_000, "census"),
    ("San Jose", "CA", 34.00, -118.00, 50, "geonames"),  # a homonym: never suggested apart
    ("Saint Louis", "MO", 38.63, -90.20, 300_000, "census"),
    ("Saint Louis Park", "MN", 44.95, -93.35, 50_000, "census"),
    ("Anchorage", "AK", 61.22, -149.90, 290_000, "census"),
    ("San Juan", "PR", 18.47, -66.11, 340_000, "census"),  # a territory: "City, PR" does not geocode
]


@pytest.fixture
def index():
    return PlaceIndex(ROWS)


def labels(rows):
    return [row["label"] for row in rows]


def test_prefix_search_is_ordered_by_population(index):
    rows, matches = index.search("chi")
    assert labels(rows) == ["Chicago, IL", "Chico, CA", "Chicago Heights, IL", "Chillicothe, OH", "Chico, TX"]
    assert matches == 5
    first = rows[0]
    assert first == {
        "label": "Chicago, IL", "city": "Chicago", "state": "IL", "lat": 41.88, "lon": -87.63,
        "population": 2_700_000, "plannable": True,
    }
    rows, matches = index.search("chi", limit=2)
    assert labels(rows) == ["Chicago, IL", "Chico, CA"] and matches == 5


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("chi, il", ["Chicago, IL", "Chicago Heights, IL"]),
        ("Chi, Illinois", ["Chicago, IL", "Chicago Heights, IL"]),
        ("chi, i", ["Chicago, IL", "Chicago Heights, IL"]),  # the start of a state
        ("chico, tx", ["Chico, TX"]),
        ("chi,", ["Chicago, IL", "Chico, CA", "Chicago Heights, IL", "Chillicothe, OH", "Chico, TX"]),
        ("chi, zz", []),  # not a US state
        ("chi, on", []),  # a Canadian province
    ],
)
def test_state_after_a_comma_filters(index, query, expected):
    assert labels(index.search(query)[0]) == expected


@pytest.mark.parametrize("query", ["SAN JOSÉ", "san jose", "  San Jos", "san j"])
def test_accents_and_case_are_normalized(index, query):
    rows, matches = index.search(query)
    assert labels(rows) == ["San Jose, CA"] and matches == 1  # the homonym is the same suggestion
    assert (rows[0]["lat"], rows[0]["lon"]) == (37.34, -121.89)  # the place lookup() picks


@pytest.mark.parametrize("query", ["st lo", "St. Louis", "saint l", "ST LOUIS"])
def test_saint_is_found_however_it_is_written(index, query):
    assert labels(index.search(query)[0])[0] == "Saint Louis, MO"


def test_a_space_after_a_word_ends_the_word(index):
    assert labels(index.search("saint louis")[0]) == ["Saint Louis, MO", "Saint Louis Park, MN"]
    assert labels(index.search("saint louis ")[0]) == ["Saint Louis Park, MN"]


@pytest.mark.parametrize("query", ["", " ", "c", "C,", "4", "40.7,-74.0", "é", "St"])
def test_short_query_returns_nothing(index, query):
    if query == "St":  # two letters typed, "saint" after normalizing: a real prefix
        assert index.search(query)[0]
        return
    assert index.search(query) == ([], 0)
    assert parse_place_query(query) is None


def test_alaska_is_suggested_but_flagged_and_territories_are_not(index):
    (anchorage,) = index.search("anch")[0]
    assert anchorage["label"] == "Anchorage, AK" and anchorage["plannable"] is False
    assert index.search("san ju")[0] == []


@pytest.mark.django_db
def test_every_label_plans_to_the_same_point():
    """Each suggestion, typed back as start / finish, is geocoded offline to exactly its point."""
    index = get_place_index()
    client = ExternalApiClient()
    queries = ["new", "chi", "st lo", "san", "fort w", "mc", "o fal", "las v", "albuquerque", "wash, dc", "kan"]
    checked = 0
    for query in queries:
        for row in index.search(query, limit=20)[0]:
            if not row["plannable"]:
                continue
            location = geocode(row["label"], client, field="start")
            assert (round(location.latitude, 6), round(location.longitude, 6)) == (row["lat"], row["lon"]), row
            assert location.geocoder == "offline"
            checked += 1
    assert checked > 100 and client.call_count == 0


@pytest.mark.django_db
def test_search_is_fast_on_the_real_index():
    """Well under the 20 ms budget of the type-ahead (the index is in memory)."""
    index = get_place_index()
    index.search("warm up", 1)  # the warm-up builds the prefix index at start-up
    timings = []
    for query in ["sa", "new ", "chi, il", "san jose", "st lo", "fort", "a", "springf", "mount v", "w"] * 5:
        started = time.perf_counter()
        index.search(query, 8)
        timings.append((time.perf_counter() - started) * 1000)
    assert statistics.median(timings) < 5
    assert max(timings) < 20


# --- the endpoint -----------------------------------------------------------------------------------


@pytest.mark.django_db
def test_places_endpoint_answers_suggestions_without_external_calls():
    response = APIClient().get("/api/places", {"q": "chi, il", "limit": 3})
    assert response.status_code == 200
    body = response.json()
    assert [row["label"] for row in body["results"]][0] == "Chicago, IL"
    assert len(body["results"]) == 3
    assert set(body["results"][0]) == {"label", "city", "state", "lat", "lon", "population", "plannable"}
    assert body["meta"]["external_api_calls"] == 0 and body["meta"]["matches"] >= 3
    assert body["meta"]["took_ms"] < 20
    populations = [row["population"] for row in body["results"]]
    assert populations == sorted(populations, reverse=True)
    assert APIClient().get("/api/places/", {"q": "c"}).json()["results"] == []
    assert APIClient().get("/api/places").json()["results"] == []  # no q: nothing typed yet


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("params", "field"),
    [({"q": "chi", "limit": 0}, "limit"), ({"q": "chi", "limit": 21}, "limit"), ({"q": "chi", "limit": "x"}, "limit"),
     ({"q": "chi", "state": "IL"}, "non_field_errors"), ({"q": "x" * 101}, "q")],
)
def test_places_endpoint_rejects_bad_parameters(params, field):
    response = APIClient().get("/api/places", params)
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "invalid_request" and field in body["detail"]
    assert body["meta"]["external_api_calls"] == 0


@pytest.mark.django_db
def test_places_are_not_rate_limited_like_routes(settings, monkeypatch):
    monkeypatch.setitem(settings.FUEL_PLANNER, "RATE_LIMIT_PER_MINUTE", 1)
    client = APIClient()
    assert all(client.get("/api/places", {"q": "dal"}).status_code == 200 for _ in range(5))
