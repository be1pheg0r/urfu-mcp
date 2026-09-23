from collections.abc import Sequence

from .models import PersonCandidate


class SfeduGatewayError(Exception):
    """Base class for safe, typed SfeduSchedule gateway failures."""


class NotAuthenticated(SfeduGatewayError):
    """The supplied token was rejected or has expired."""


class Forbidden(SfeduGatewayError):
    """The upstream refused access to the requested resource."""


class RateLimited(SfeduGatewayError):
    """The upstream rate limit was reached."""

    def __init__(self, retry_after: str | None = None) -> None:
        super().__init__("SfeduSchedule rate limit exceeded")
        self.retry_after = retry_after


class UpstreamUnavailable(SfeduGatewayError):
    """The upstream could not be reached or returned a server error."""


class InvalidUpstreamResponse(SfeduGatewayError, ValueError):
    """The upstream response was too large or was not valid JSON data."""


class IncompleteResult(InvalidUpstreamResponse):
    """The upstream response is valid but demonstrably truncated."""


class AmbiguousPerson(SfeduGatewayError):
    """A search matched multiple people and requires caller disambiguation."""

    def __init__(self, candidates: Sequence[PersonCandidate]) -> None:
        super().__init__("Person search is ambiguous; select one returned person ID")
        self.candidates = candidates


class PersonNotFound(SfeduGatewayError):
    """A complete person search returned no candidates."""
