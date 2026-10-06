"""The only table: the truck stops of the OPIS price file, geocoded once at load time.

Filled by ``manage.py load_stations`` (``services/station_loader.py``). At request
time the planner never queries it row by row: ``services/stations.py`` keeps an
in-memory numpy copy of (opis_id, lat, lon, price) and the planner only fetches the
few stations it chose, by ``opis_id``.
"""

from django.db import models


class FuelStation(models.Model):
    """A truck stop from the OPIS price file."""

    # Stable business key. The table is reloaded with new primary keys every time
    # load_stations runs, so everything outside the DB refers to stations by opis_id.
    opis_id = models.PositiveIntegerField(unique=True)
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=128)
    state = models.CharField(max_length=2)
    rack_id = models.PositiveIntegerField(null=True, blank=True)
    # Price the planner uses (USD per gallon): the median of the station's quotes in
    # the file by default (PRICE_POLICY), stored with 4 decimals.
    price = models.DecimalField(max_digits=7, decimal_places=4)
    # Spread of the quotes, so a merged station shows how much its prices disagree.
    price_min = models.DecimalField(max_digits=7, decimal_places=4)
    price_max = models.DecimalField(max_digits=7, decimal_places=4)
    # How many rows (price quotes) the file had for this station.
    price_rows = models.PositiveSmallIntegerField(default=1)
    # City-level coordinates. NULL when the city was not found or is ambiguous; such
    # stations stay in the table but the planner ignores them.
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    # "census" or "geonames": which offline dataset gave the coordinates.
    geocode_source = models.CharField(max_length=16, blank=True)

    class Meta:
        ordering = ["opis_id"]

    def __str__(self) -> str:
        return f"{self.name} ({self.city}, {self.state}) ${self.price}"
