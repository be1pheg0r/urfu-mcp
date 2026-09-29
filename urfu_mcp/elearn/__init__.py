"""eLearn session integration contracts and safe errors."""

from .errors import (
    AmbiguousCourse,
    CourseNotFound,
    ELearnError,
    IntegrationUnavailable,
    InvalidInput,
    InvalidUpstreamResponse,
    NotAuthenticated,
    SessionUnavailable,
    UpstreamUnavailable,
)

__all__ = [
    "AmbiguousCourse", "CourseNotFound", "ELearnError", "IntegrationUnavailable",
    "InvalidInput", "InvalidUpstreamResponse", "NotAuthenticated",
    "SessionUnavailable", "UpstreamUnavailable",
]
