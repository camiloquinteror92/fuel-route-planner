"""Input validation for ``/api/route``.

Only checks that need no lookup (format, ranges, unknown parameters), so an
invalid request is answered in under a millisecond with 0 external calls.
Everything that needs geocoding (place not found, same place written two ways,
Alaska / Hawaii) is checked in ``services/planner.py``.

The ``help_text`` of each field is published by ``/api/about``, with the default and
the allowed range of the truck settings.

Truck settings (all optional): ``mpg``, ``max_range_miles``, ``corridor_miles``,
``price_policy``, ``consolidate`` and ``safety_reserve_gal``. Missing, blank or
null means the default (``settings.FUEL_PLANNER``); out of range is a 400 that says
the range.
"""

from django.conf import settings
from rest_framework import serializers

from .services.geocoding import has_letters, parse_coordinates
from .services.planner import START_EMPTY, START_FULL, WHAT_IF_RANGES, PlanSettings
from .services.stations import PRICE_POLICIES
from .services.usa import region_of

WHAT_IF_PARAMS = ("mpg", "max_range_miles", "corridor_miles", "price_policy", "consolidate", "safety_reserve_gal")
ALLOWED_PARAMS = ("start", "finish", "start_tank", "include", *WHAT_IF_PARAMS)
# Optional extra blocks of the response, asked for with ?include=a,b.
INCLUDE_VALUES = ("details", "candidates")


class SettingFloatField(serializers.FloatField):
    """Optional number: missing, blank ("mpg=") or null means the default (None)."""

    def __init__(self, low: float | None = None, high: float | None = None, **kwargs):
        kwargs.setdefault("required", False)
        kwargs.setdefault("allow_null", True)
        messages = {}
        if low is not None and high is not None:
            messages = {
                "min_value": f"Must be between {low:g} and {high:g}.",
                "max_value": f"Must be between {low:g} and {high:g}.",
            }
        elif low is not None:
            messages = {"min_value": f"Must be at least {low:g}."}
        super().__init__(min_value=low, max_value=high, error_messages=messages, **kwargs)

    def run_validation(self, data=serializers.empty):
        if isinstance(data, str) and not data.strip():
            data = None
        return super().run_validation(data)


def _range_field(name: str, help_text: str) -> SettingFloatField:
    low, high = WHAT_IF_RANGES[name]
    return SettingFloatField(low, high, help_text=help_text)


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
            "Optional extra blocks, comma separated. 'details': how the plan was computed and the other "
            "strategies it was compared with. 'candidates': every station near the route, with its price and "
            "mile marker. Neither changes the plan; both share its cache, so they cost no external call after "
            "the same trip was planned."
        ),
    )
    # --- truck settings: change the plan, never the route (0 external calls once routed) ---
    mpg = _range_field(
        "mpg", "Truck setting: miles per gallon. A thirstier truck buys more fuel."
    )
    max_range_miles = _range_field(
        "max_range_miles",
        "Truck setting: how far a full tank goes, in miles. The tank size is this range divided by mpg.",
    )
    corridor_miles = _range_field(
        "corridor_miles",
        "Truck setting: how far from the route a station may be, in miles. Wider means more stations to choose "
        "from.",
    )
    price_policy = serializers.ChoiceField(
        choices=list(PRICE_POLICIES),
        required=False,
        allow_blank=True,
        allow_null=True,
        help_text=(
            "Truck setting: which price of a station to use when the file lists it several times. 'median' (default) "
            "is the price stored for it: the median of its quotes (the average of the two when there are two), "
            "unless load_stations ran with PRICE_POLICY=min; 'min' its cheapest quote (best case); 'max' its "
            "dearest (worst case)."
        ),
    )
    consolidate = serializers.BooleanField(
        required=False,
        allow_null=True,
        help_text=(
            "Truck setting: false (default) keeps the cheapest plan, small stops included; true merges a stop "
            f"that buys less than {settings.FUEL_PLANNER['MIN_STOP_GALLONS']:g} gallons into a neighbour when that "
            f"costs at most ${settings.FUEL_PLANNER['MAX_CONSOLIDATION_COST']:.2f} more."
        ),
    )
    safety_reserve_gal = SettingFloatField(
        0.0,
        help_text=(
            "Truck setting: gallons that must always be left in the tank when the truck reaches any stop and the "
            "destination. From 0 (default) to less than the tank. If no plan can keep it, the answer is a 422."
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

    def validate_price_policy(self, value):
        return value or None

    def validate(self, attrs: dict) -> dict:
        """The safety reserve must fit in the tank of THIS request (range / mpg)."""
        defaults = PlanSettings.defaults()
        mpg = attrs.get("mpg") or defaults.mpg
        max_range = attrs.get("max_range_miles") or defaults.max_range_miles
        tank = max_range / mpg
        reserve = attrs.get("safety_reserve_gal")
        if reserve is not None and reserve >= tank:
            raise serializers.ValidationError(
                {
                    "safety_reserve_gal": [
                        f"Must be less than the tank: {tank:g} gal (max_range_miles {max_range:g} / mpg {mpg:g})."
                    ]
                }
            )
        return attrs

    def validate_include(self, value: str) -> frozenset:
        items = [item.strip() for item in value.split(",") if item.strip()]
        unknown = sorted(set(items) - set(INCLUDE_VALUES))
        if unknown:
            raise serializers.ValidationError(
                f"Unknown value(s): {', '.join(unknown)}. Allowed: {', '.join(INCLUDE_VALUES)}."
            )
        return frozenset(items)


class PlacesRequestSerializer(serializers.Serializer):
    """``GET /api/places``: ``q`` (what is being typed) and ``limit``. Nothing else is accepted."""

    q = serializers.CharField(
        required=False,
        default="",
        max_length=100,
        allow_blank=True,
        trim_whitespace=False,  # a space typed after a word narrows the search ("new ")
        help_text="The start of a US place name, optionally with a state: 'chi', 'chi, il', 'san jose'.",
    )
    limit = serializers.IntegerField(
        required=False,
        default=8,
        min_value=1,
        max_value=20,
        error_messages={"min_value": "Must be between 1 and 20.", "max_value": "Must be between 1 and 20."},
        help_text="How many places to return (1 to 20, default 8).",
    )

    def to_internal_value(self, data):
        if hasattr(data, "keys"):
            unknown = sorted(set(data.keys()) - {"q", "limit"})
            if unknown:
                raise serializers.ValidationError(
                    {"non_field_errors": [f"Unknown parameter(s): {', '.join(unknown)}. Allowed: q, limit."]}
                )
        return super().to_internal_value(data)
