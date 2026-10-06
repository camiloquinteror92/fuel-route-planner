"""Domain errors. Each one knows the HTTP status the API should answer with."""


class PlannerError(Exception):
    status_code = 400
    code = "invalid_request"

    def __init__(self, message: str, **details):
        super().__init__(message)
        self.message = message
        self.details = details

    def as_dict(self) -> dict:
        body = {"error": self.code, "detail": self.message}
        if self.details:
            body.update(self.details)
        return body


class LocationNotFound(PlannerError):
    status_code = 400
    code = "location_not_found"


class LocationOutsideUSA(PlannerError):
    status_code = 400
    code = "location_outside_usa"


class NoRouteFound(PlannerError):
    status_code = 422
    code = "no_route"


class NoReachableStation(PlannerError):
    status_code = 422
    code = "no_reachable_fuel_station"


class ExternalServiceError(PlannerError):
    status_code = 502
    code = "upstream_unavailable"
