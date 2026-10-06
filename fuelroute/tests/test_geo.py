import numpy as np
import pytest

from fuelroute.services.geo import cumulative_miles, haversine_miles, resample, simplify
from fuelroute.services.osrm import decode_polyline
from fuelroute.services.stations import StationArrays, stations_along_route


def test_decode_polyline():
    # Example from the Google polyline spec (precision 5; OSRM polyline6 is the same with 6).
    points = decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@", precision=5)
    expected = np.array([[38.5, -120.2], [40.7, -120.95], [43.252, -126.453]])
    assert np.allclose(points, expected)


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


def test_corridor_search_matches_brute_force():
    rng = np.random.default_rng(7)
    # A wiggly 1,500-mile route.
    lon = np.linspace(-110, -85, 400)
    lat = 36 + 2 * np.sin(np.linspace(0, 6, 400))
    line = np.column_stack((lat, lon))
    samples, miles = resample(line, cumulative_miles(line), 1.0)
    count = 4000
    stations = StationArrays(
        ids=np.arange(count),
        lat=rng.uniform(30, 42, count),
        lon=rng.uniform(-112, -83, count),
        price=rng.uniform(3, 4, count),
    )
    found = {s.station_id: s for s in stations_along_route(samples, miles, 10.0, stations)}

    distances = haversine_miles(
        stations.lat[:, None], stations.lon[:, None], samples[None, :, 0], samples[None, :, 1]
    )
    expected = set(np.nonzero(distances.min(axis=1) <= 10.0)[0].tolist())
    assert set(found) == expected
    for station_id in list(expected)[:50]:
        assert found[station_id].mile == pytest.approx(miles[distances[station_id].argmin()], abs=1.0)
