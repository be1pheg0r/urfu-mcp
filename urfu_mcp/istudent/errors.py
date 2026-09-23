"""Safe typed errors for iStudent-facing MCP operations."""


class BRSError(ValueError):
    """Base for fixed, user-safe BRS errors; never include source payloads."""


class InvalidInput(BRSError):
    """A requested BRS selector is blank or invalid."""


class NotAuthenticated(BRSError):
    """No trusted local identity is available for this call."""


class SessionUnavailable(BRSError):
    """An iStudent-specific session is missing or could not be acquired."""


class IntegrationUnavailable(SessionUnavailable):
    """No evidence-backed iStudent auth/source integration is configured."""


class SubjectNotFound(BRSError):
    """No source-confirmed subject matches the selector."""


class AmbiguousSubject(BRSError):
    """More than one source-confirmed subject matches the selector."""


class InvalidUpstreamResponse(BRSError):
    """The parser cannot establish trustworthy normalized source data."""


class UpstreamUnavailable(BRSError):
    """The source failed without exposing transport details or response data."""
