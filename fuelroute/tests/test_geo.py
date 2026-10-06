"""Geometry helpers, polyline decoding, the US mask and the corridor search."""

import numpy as np
import pytest

from fuelroute.services.geo import cumulative_miles, haversine_miles, resample, simplify
from fuelroute.services.osrm import decode_polyline
from fuelroute.services.stations import StationArrays, stations_along_route
from fuelroute.services.usa import region_of

from .conftest import encode_polyline


def test_decode_polyline_spec_example():
    # Example from the Google polyline spec (precision 5; OSRM polyline6 is the same with 6).
    points = decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@", precision=5)
    expected = np.array([[38.5, -120.2], [40.7, -120.95], [43.252, -126.453]])
    assert np.allclose(points, expected)


def test_decode_polyline_round_trip_on_a_long_random_line():
    rng = np.random.default_rng(3)
    points = np.column_stack((30 + np.cumsum(rng.normal(0, 0.01, 5000)), -100 + np.cumsum(rng.normal(0, 0.01, 5000))))
    points = np.round(points, 6)
    assert np.allclose(decode_polyline(encode_polyline(points)), points, atol=1e-9)


@pytest.mark.parametrize("bad", ["_p~iF~ps|U_", "abc\x10"])
def test_decode_polyline_rejects_broken_input(bad):
    with pytest.raises(ValueError):
        decode_polyline(bad)


def test_haversine_known_distance():
    # New York -> Los Angeles is ~2,445 miles great-circle.
    assert haversine_miles(40.7128, -74.0060, 34.0522, -118.2437) == pytest.approx(2445, abs=5)


def test_resample_keeps_endpoints_and_spacing():
    line = np.array([[35.0, -100.0], [35.0, -99.0], [35.0, -98.0]])
    samples, miles = resample(line, cumulative_miles(line), 1.0)
    assert miles[0] == 0 and miles[-1] == pytest.approx(cumulative_miles(line)[-1])
    assert np.allclose(np.diff(miles)[:-1], 1.0)
    assert samples[-1].tolist() == pytest.approx([35.0, -98.0])


def test_simplify_drops_collinear_points_only():
    line = np.array([[0.0, 0.0], [0.0, 1.0], [0.0, 2.0], [1.0, 2.0]])
    assert simplify(line).tolist() == [[0.0, 0.0], [0.0, 2.0], [1.0, 2.0]]


@pytest.mark.parametrize(
    ("name", "lat", "lon", "region"),
    [
        ("Austin", 30.27, -97.74, "lower48"),
        ("Key West", 24.5551, -81.78, "lower48"),
        ("Point Roberts WA", 48.985, -123.078, "lower48"),
        ("San Ysidro (border)", 32.5556, -117.047, "lower48"),
        ("Anchorage", 61.2181, -149.9003, "alaska"),
        ("Honolulu", 21.3069, -157.8583, "hawaii"),
        ("Toronto", 43.6532, -79.3832, None),
        ("Montreal", 45.5019, -73.5674, None),
        ("Vancouver", 49.2827, -123.1207, None),
        ("Monterrey", 25.6866, -100.3161, None),
        ("Tijuana", 32.5149, -117.0382, None),
        ("Whitehorse", 60.7212, -135.0568, None),
        ("Nassau", 25.0443, -77.3504, None),
        ("Gulf of Mexico", 27.0, -90.0, None),
        ("Atlantic", 35.0, -70.0, None),
        ("Lake Michigan", 43.5, -87.0, None),
        ("Paris", 48.85, 2.35, None),
    ],
)
def test_us_mask(name, lat, lon, region):
    assert region_of(lat, lon) == region, name


def _wiggly_route():
    lon = np.linspace(-110, -85, 400)
    lat = 36 + 2 * np.sin(np.linspace(0, 6, 400))
    line = np.column_stack((lat, lon))
    return resample(line, cumulative_miles(line), 1.0)


def _random_stations(count=4000, seed=7):
    rng = np.random.default_rng(seed)
    return StationArrays(
        opis_ids=np.arange(count),
        lat=rng.uniform(30, 42, count),
        lon=rng.uniform(-112, -83, count),
        price=rng.uniform(3, 4, count),
    )


def test_corridor_search_matches_brute_force():
    samples, miles = _wiggly_route()
    stations = _random_stations()
    found = {s.opis_id: s for s in stations_along_route(samples, miles, 10.0, stations)}

    distances = haversine_miles(
        stations.lat[:, None], stations.lon[:, None], samples[None, :, 0], samples[None, :, 1]
    )
    expected = set(np.nonzero(distances.min(axis=1) <= 10.0)[0].tolist())
    assert set(found) == expected
    for station_id in list(expected)[:50]:
        assert found[station_id].mile == pytest.approx(miles[distances[station_id].argmin()], abs=1.0)


def test_corridor_drops_stations_seen_only_from_outside_the_usa():
    samples, miles = _wiggly_route()
    stations = _random_stations()
    inside = miles < miles[-1] / 2  # pretend the second half of the route is in Canada
    found = stations_along_route(samples, miles, 10.0, stations, sample_in_usa=inside)
    every = stations_along_route(samples, miles, 10.0, stations)
    assert found and len(found) < len(every)
    assert all(s.mile < miles[-1] / 2 for s in found)
