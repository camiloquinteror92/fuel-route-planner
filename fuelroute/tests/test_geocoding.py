import pytest

from fuelroute.services.errors import LocationNotFound, LocationOutsideUSA
from fuelroute.services.geocoding import geocode, is_in_usa, parse_coordinates


class NoNetworkClient:
    """Fails the test if geocoding tries to call Nominatim."""

    calls: list = []
    call_count = 0

    def get_json(self, *args, **kwargs):
        raise AssertionError("unexpected external call")


class FakeNominatim:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get_json(self, service, url, params=None):
        self.calls.append(service)
        return 200, self.payload


def test_parse_coordinates():
    assert parse_coordinates("40.7128,-74.0060") == (40.7128, -74.006)
    assert parse_coordinates(" 40.7 , -74 ") == (40.7, -74.0)
    assert parse_coordinates("Austin, TX") is None


def test_usa_bounding_boxes():
    assert is_in_usa(30.27, -97.74)  # Austin
    assert is_in_usa(61.2, -149.9)  # Anchorage
    assert is_in_usa(21.3, -157.8)  # Honolulu
    assert not is_in_usa(48.85, 2.35)  # Paris
    assert not is_in_usa(19.43, -99.13)  # Mexico City


@pytest.mark.parametrize("text", ["Austin, TX", "austin, texas", "Austin TX", "Austin, TX, USA"])
def test_city_state_is_geocoded_offline(text):
    location = geocode(text, NoNetworkClient(), field="start")
    assert location.geocoder == "offline"
    assert location.latitude == pytest.approx(30.3, abs=0.2)
    assert location.longitude == pytest.approx(-97.75, abs=0.2)


def test_free_text_falls_back_to_nominatim_once_and_is_cached():
    client = FakeNominatim([{"lat": "40.7484", "lon": "-73.9857", "display_name": "Empire State Building"}])
    first = geocode("Empire State Building", client, field="start")
    second = geocode("Empire State Building", client, field="start")
    assert first.geocoder == "nominatim"
    assert second.latitude == first.latitude
    assert client.calls == ["nominatim"]


def test_unknown_place_raises():
    with pytest.raises(LocationNotFound):
        geocode("Nowhere at all", FakeNominatim([]), field="start")


def test_coordinates_outside_usa_raise():
    with pytest.raises(LocationOutsideUSA):
        geocode("48.85,2.35", NoNetworkClient(), field="finish")
