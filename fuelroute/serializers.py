"""Input validation for ``/api/route`` and ``/api/route/map``.

Only checks that need no lookup (format, ranges, unknown parameters), so an
invalid request is answered in under a millisecond with 0 external calls.
Everything that needs geocoding (place not found, same place written two ways,
Alaska / Hawaii) is checked in ``services/planner.py``.
"""

from rest_framework import serializers

from .services.geocoding import has_letters, parse_coordinates
from .services.planner import START_EMPTY, START_FULL
from .services.usa import region_of

ALLOWED_PARAMS = ("start", "finish", "start_tank")


class RouteRequestSerializer(serializers.Serializer):
    """``start`` / ``finish``: "City, ST" or "lat,lon"; ``start_tank``: empty | full."""

    start = serializers.CharField(max_length=200, trim_whitespace=True)
    finish = serializers.CharField(max_length=200, trim_whitespace=True)
    # Blank ("start_tank=" in a query string or "" in JSON) means the default.
    start_tank = serializers.ChoiceField(
        choices=[START_EMPTY, START_FULL], default=START_EMPTY, required=False, allow_blank=True
    )

    def to_internal_value(self, data):
        # A typo like "start_tnak=full" would otherwise be ignored silently and the
        # trip planned with the default tank. Reject it and say what is allowed.
        if hasattr(data, "keys"):
            unknown = sorted(set(data.keys()) - set(ALLOWED_PARAMS))
            if unknown:
                raise serializers.ValidationError(
                    {"non_field_errors": [f"Unknown parameter(s): {', '.join(unknown)}. Allowed: {', '.join(ALLOWED_PARAMS)}."]}
                )
        return super().to_internal_value(data)

    def _validate_location(self, value: str) -> str:
        coordinates = parse_coordinates(value)
        if coordinates:
            latitude, longitude = coordinates
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                raise serializers.ValidationError("Coordinates must be 'lat,lon' with valid ranges.")
            if region_of(latitude, longitude) is None:
                raise serializers.ValidationError("Location must be inside the USA.")
        elif len(value) < 3 or not has_letters(value):
            # "???", "40.7" or a bare number would otherwise be sent to the geocoder.
            raise serializers.ValidationError("Use 'City, ST' (e.g. 'Austin, TX') or 'lat,lon'.")
        return value

    def validate_start(self, value: str) -> str:
        return self._validate_location(value)

    def validate_finish(self, value: str) -> str:
        return self._validate_location(value)

    def validate_start_tank(self, value: str) -> str:
        return value or START_EMPTY
