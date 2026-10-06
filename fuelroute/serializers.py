from rest_framework import serializers

from .services.geocoding import is_in_usa, parse_coordinates
from .services.planner import START_EMPTY, START_FULL


class RouteRequestSerializer(serializers.Serializer):
    start = serializers.CharField(max_length=200, trim_whitespace=True)
    finish = serializers.CharField(max_length=200, trim_whitespace=True)
    start_tank = serializers.ChoiceField(choices=[START_EMPTY, START_FULL], default=START_EMPTY, required=False)

    def _validate_location(self, value: str) -> str:
        coordinates = parse_coordinates(value)
        if coordinates:
            latitude, longitude = coordinates
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                raise serializers.ValidationError("Coordinates must be 'lat,lon' with valid ranges.")
            if not is_in_usa(latitude, longitude):
                raise serializers.ValidationError("Location must be inside the USA.")
        elif len(value) < 3:
            raise serializers.ValidationError("Use 'City, ST' (e.g. 'Austin, TX') or 'lat,lon'.")
        return value

    def validate_start(self, value: str) -> str:
        return self._validate_location(value)

    def validate_finish(self, value: str) -> str:
        return self._validate_location(value)

    def validate(self, attrs):
        if attrs["start"].strip().lower() == attrs["finish"].strip().lower():
            raise serializers.ValidationError("start and finish must be different locations.")
        return attrs
