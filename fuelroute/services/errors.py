"""Domain errors. Each one carries the HTTP status and the ``error`` code of the response.

Every error response of the API has the same shape:
``{"error": "<code>", "detail": "<human message>", ...extra fields}``
(``views.py`` adds ``meta`` with the external calls made before the error).
"""


class PlannerError(Exception):
    """Invalid input: a missing or malformed parameter, or an unknown one (nothing is planned)."""

    status_code = 400
    code = "invalid_request"

    def __init__(self, message: str, **details):
        super().__init__(message)
        self.message = message
        self.details = details

    def as_dict(self) -> dict:
        body = {"error": self.code, "detail": self.message}
        body.update(self.details)
        return body

    @property
    def headers(self) -> dict:
        return {}


class LocationNotFound(PlannerError):
    """Not a known US place: unknown to the index and Nominatim, a state code that does not exist, or a street."""

    status_code = 400
    code = "location_not_found"


class LocationOutsideUSA(PlannerError):
    """Outside the USA: "City, XX" with a Canadian province or a Mexican state ("Toronto, ON"), or text found abroad."""

    status_code = 400
    code = "location_outside_usa"


class SameLocation(PlannerError):
    """start and finish resolve to the same place ("Austin, TX" vs "Austin, Texas")."""

    status_code = 400
    code = "same_location"


class NoFuelDataInRegion(PlannerError):
    """Alaska, Hawaii or a US territory: valid US places, but the price file has no station there."""

    status_code = 422
    code = "no_fuel_data_in_region"


class LocationNotNearRoad(PlannerError):
    """OSRM had to move a point several miles to reach a road (sea, lake, wilderness)."""

    status_code = 422
    code = "location_not_near_road"


class NoRouteFound(PlannerError):
    """The routing service found no drivable route between the two points."""

    status_code = 422
    code = "no_route"


class NoFuelDataOnRoute(PlannerError):
    """No station of the price file within the corridor of a trip that needs fuel."""

    status_code = 422
    code = "no_fuel_data_on_route"


class NoReachableStation(PlannerError):
    """Some stretch of the route without stations is longer than the truck's range."""

    status_code = 422
    code = "no_reachable_fuel_station"


class ExternalServiceError(PlannerError):
    """The routing / geocoding service failed or was unreachable, after one retry."""

    status_code = 502
    code = "upstream_unavailable"


class UpstreamBusy(PlannerError):
    """The external service rate limited us (429) or is overloaded (503)."""

    status_code = 503
    code = "upstream_busy"

    def __init__(self, message: str, retry_after: str = "5", **details):
        super().__init__(message, **details)
        self.retry_after = str(retry_after)

    @property
    def headers(self) -> dict:
        return {"Retry-After": self.retry_after}


class StationDataNotLoaded(PlannerError):
    """The station table is empty: run `python manage.py load_stations`."""

    status_code = 503
    code = "station_data_not_loaded"


class StationDataChanged(PlannerError):
    """load_stations replaced the table while a request was being planned (rare)."""

    status_code = 503
    code = "station_data_changed"

