"""Safe typed errors for eLearn-facing MCP operations."""

from __future__ import annotations


class ELearnError(ValueError):
    """Base for safe eLearn errors."""


class InvalidInput(ELearnError):
    """An eLearn selector is invalid."""


class NotAuthenticated(ELearnError):
    """No trusted local identity is available."""


class SessionUnavailable(ELearnError):
    """An eLearn session is missing or unavailable."""


class IntegrationUnavailable(SessionUnavailable):
    """No verified eLearn integration is configured."""


class CourseNotFound(ELearnError):
    """No course matches the selector."""


class AmbiguousCourse(ELearnError):
    """More than one course matches the selector."""


class InvalidUpstreamResponse(ELearnError):
    """The source response is not trustworthy."""


class UpstreamUnavailable(ELearnError):
    """The source is unavailable."""


__all__ = [
    "AmbiguousCourse",
    "CourseNotFound",
    "ELearnError",
    "IntegrationUnavailable",
    "InvalidInput",
    "InvalidUpstreamResponse",
    "NotAuthenticated",
    "SessionUnavailable",
    "UpstreamUnavailable",
]
