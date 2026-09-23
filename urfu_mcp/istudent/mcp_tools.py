"""Register the single safe iStudent BRS MCP tool."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .auth import IStudentIdentityProvider, IStudentSessionProvider
from .brs import BRSReader, select_subjects
from .errors import (
    BRSError,
    IntegrationUnavailable,
    InvalidInput,
    NotAuthenticated,
    SessionUnavailable,
)
from .public_models import public_brs_payload

Clock = Callable[[], datetime]
_TIMEZONE_NAME = "Asia/Yekaterinburg"


def _current_local_date(clock: Clock) -> date:
    """Convert an injected aware instant to the configured local calendar date."""
    now = clock()
    if now.tzinfo is None or now.utcoffset() is None:
        raise BRSError("The BRS clock must return a timezone-aware instant")
    return now.astimezone(ZoneInfo(_TIMEZONE_NAME)).date()


def register_brs_tool(
    server: MCPServer,
    *,
    identity_provider: IStudentIdentityProvider,
    session_provider: IStudentSessionProvider,
    reader: BRSReader | None,
    clock: Clock | None = None,
) -> None:
    """Register `retrieve_brs(subject_name)` with identity/session fail-closed checks."""
    current_time = clock or (lambda: datetime.now(UTC))

    async def retrieve_brs(subject_name: str) -> dict[str, object]:
        """Return BRS rows by exact normalized subject name or the reserved `all`."""
        try:
            return await _retrieve_brs(subject_name)
        except BRSError as error:
            # Convert domain failures into deliberate MCP tool errors so the SDK
            # returns safe actionable text instead of logging an exception trace.
            raise ToolError(str(error)) from None
        except Exception:  # noqa: BLE001 - prevent raw context/traceback crossing MCP
            # Unexpected provider/model errors may carry private context. Keep the
            # public and logged failure fixed and omit the original traceback.
            raise ToolError("The BRS request could not be completed safely") from None

    async def _retrieve_brs(subject_name: str) -> dict[str, object]:
        selector = subject_name.strip()
        if not selector:
            raise InvalidInput("subject_name must not be blank")

        try:
            identity = await identity_provider.current_person()
        except Exception:  # noqa: BLE001 - identity providers may include private details
            raise NotAuthenticated("Could not resolve the current iStudent identity") from None
        if not isinstance(identity, str) or not identity.strip():
            raise NotAuthenticated("No authenticated current user is available")

        try:
            session = await session_provider.get_session(identity)
        except IntegrationUnavailable:
            raise IntegrationUnavailable(
                "iStudent BRS is unavailable until its authentication contract is verified"
            ) from None
        except Exception:  # noqa: BLE001 - session providers may include private details
            raise SessionUnavailable("Could not obtain an iStudent session") from None
        if session is None:
            raise SessionUnavailable("No iStudent session is available")
        if reader is None:
            raise IntegrationUnavailable(
                "iStudent BRS is unavailable until its source mapping is verified"
            )

        as_of = _current_local_date(current_time)
        result = await reader.read(identity, session, as_of=as_of)
        selected = select_subjects(result, selector)
        return public_brs_payload(
            result,
            selector=selector,
            selected_subjects=selected,
            as_of=as_of,
        )

    server.add_tool(
        retrieve_brs,
        name="retrieve_brs",
        description=(
            "Retrieve the authenticated user's BRS subject by exact normalized name, "
            "or use subject_name='all' for every source-confirmed subject."
        ),
        structured_output=True,
    )


__all__ = ["register_brs_tool"]
