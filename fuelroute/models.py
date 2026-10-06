from django.db import models


class FuelStation(models.Model):
    """A truck stop from the OPIS price file, geocoded once at load time."""

    opis_id = models.PositiveIntegerField(unique=True)
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=128)
    state = models.CharField(max_length=2)
    rack_id = models.PositiveIntegerField(null=True, blank=True)
    # Lowest retail price seen for this station in the file (USD per gallon).
    price = models.DecimalField(max_digits=7, decimal_places=4)
    # How many rows the file had for this station (duplicates are merged).
    price_rows = models.PositiveSmallIntegerField(default=1)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    # "census" or "geonames": which offline dataset gave the coordinates.
    geocode_source = models.CharField(max_length=16, blank=True)

    class Meta:
        ordering = ["opis_id"]
        indexes = [
            models.Index(fields=["latitude", "longitude"], name="station_lat_lon_idx"),
            models.Index(fields=["state", "city"], name="station_state_city_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.city}, {self.state}) ${self.price}"
