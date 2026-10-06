"""Input validation for ``/api/route``.

Only checks that need no lookup (format, ranges, unknown parameters), so an
invalid request is answered in under a millisecond with 0 external calls.
Everything that needs geocoding (place not found, same place written two ways,
Alaska / Hawaii) is checked in ``services/planner.py``.

The ``help_text`` of each field is published by ``/api/about`` (the planner page
builds its parameter reference from it).
"""

from rest_framework import serializers

from .services.geocoding import has_letters, parse_coordinates
from .services.planner import START_EMPTY, START_FULL
from .services.usa import region_of

ALLOWED_PARAMS = ("start", "finish", "start_tank", "include")
# Optional extra blocks of the response, asked for with ?include=a,b.
INCLUDE_VALUES = ("candidates",)


class RouteRequestSerializer(serializers.Serializer):
    """``start`` / ``finish``: "City, ST" or "lat,lon"; ``start_tank``: empty | full."""

    start = serializers.CharField(
        max_length=200,
        trim_whitespace=True,
        help_text="Where the trip starts, inside the USA: 'City, ST' (e.g. 'Austin, TX') or 'lat,lon'.",
    )
    finish = serializers.CharField(
        max_length=200,
        trim_whitespace=True,
        help_text="Where the trip ends, inside the USA: 'City, ST' or 'lat,lon'.",
    )
    # Blank ("start_tank=" in a query string or "" in JSON) means the default.
    start_tank = serializers.ChoiceField(
        choices=[START_EMPTY, START_FULL],
        default=START_EMPTY,
        required=False,
        allow_blank=True,
        help_text=(
            "'empty' (default): the trip pays for every mile it drives. "
            "'full': the truck leaves with a full tank and only the fuel bought on the way is counted."
        ),
    )
    include = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=100,
        help_text=(
            "Optional extra blocks, comma separated. 'candidates': every station within the corridor of the "
            "route, with its price and mile marker (the planner page draws them). It does not change the plan "
            "and shares its cache, so it costs no external call after the same trip was planned."
        ),
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

    def validate_include(self, value: str) -> frozenset:
        items = [item.strip() for item in value.split(",") if item.strip()]
        unknown = sorted(set(items) - set(INCLUDE_VALUES))
        if unknown:
            raise serializers.ValidationError(
                f"Unknown value(s): {', '.join(unknown)}. Allowed: {', '.join(INCLUDE_VALUES)}."
            )
        return frozenset(items)
