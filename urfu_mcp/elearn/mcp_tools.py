"""Register safe authenticated eLearn course tools."""

from __future__ import annotations

from typing import cast

import httpx
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .auth import ELearnIdentityProvider, ELearnSessionProvider
from .errors import (
    ELearnError,
    IntegrationUnavailable,
    NotAuthenticated,
    SessionUnavailable,
)
from .public_models import public_course_content_payload, public_courses_payload
from .reader import ELearnCourseReaderPort, local_today


def register_elearn_tools(
    server: MCPServer,
    *,
    identity_provider: ELearnIdentityProvider,
    session_provider: ELearnSessionProvider,
    reader: ELearnCourseReaderPort | None,
) -> None:
    async def context() -> tuple[str, object]:
        try:
            identity = await identity_provider.current_person()
        except Exception:  # noqa: BLE001 - sanitize private provider/reader failures
            raise NotAuthenticated("Could not resolve the current eLearn identity") from None
        if not isinstance(identity, str) or not identity.strip():
            raise NotAuthenticated("No authenticated current user is available")
        try:
            session = await session_provider.get_session(identity)
        except IntegrationUnavailable:
            raise
        except Exception:  # noqa: BLE001 - sanitize private provider/reader failures
            raise SessionUnavailable("Could not obtain an eLearn session") from None
        if session is None:
            raise SessionUnavailable("No eLearn session is available")
        if reader is None:
            raise IntegrationUnavailable("eLearn is unavailable until its source mapping is verified")
        return identity, session

    async def retrieve_courses_list() -> dict[str, object]:
        try:
            _, session = await context()
            courses = await reader.read_courses(cast(httpx.AsyncClient, session))  # type: ignore[union-attr]
            return public_courses_payload(courses, as_of=local_today())
        except ELearnError as error:
            raise ToolError(str(error)) from None
        except Exception:  # noqa: BLE001 - sanitize private provider/reader failures
            raise ToolError("The eLearn request could not be completed safely") from None

    async def retrieve_course_content(course: str) -> dict[str, object]:
        try:
            if not isinstance(course, str) or not course.strip():
                raise ToolError("course must not be blank")
            _, session = await context()
            content = await reader.read_course(cast(httpx.AsyncClient, session), course)  # type: ignore[union-attr]
            return public_course_content_payload(content, selector=course, as_of=local_today())
        except ELearnError as error:
            raise ToolError(str(error)) from None
        except ToolError:
            raise
        except Exception:  # noqa: BLE001 - sanitize private provider/reader failures
            raise ToolError("The eLearn request could not be completed safely") from None

    server.add_tool(
        retrieve_courses_list,
        name="retrieve_courses_list",
        description=("Return the authenticated user's enrolled eLearn (Moodle) courses with course_id, name and progress group. "
                     "Use course_id as the input for retrieve_course_content."),
        structured_output=True,
    )
    server.add_tool(
        retrieve_course_content,
        name="retrieve_course_content",
        description=("Retrieve one enrolled course's sections and activities. The course argument accepts either the numeric "
                     "course_id from retrieve_courses_list or the exact course name; ambiguous names fail."),
        structured_output=True,
    )


__all__ = ["register_elearn_tools"]
