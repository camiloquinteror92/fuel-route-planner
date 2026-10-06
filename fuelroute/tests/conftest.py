import pytest
from django.core.cache import cache

from fuelroute.services.stations import reset_station_arrays


@pytest.fixture(autouse=True)
def clean_state():
    cache.clear()
    reset_station_arrays()
    yield
    cache.clear()
    reset_station_arrays()
