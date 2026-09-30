"""Securely register the Modeus schedule tools on an MCP server."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Protocol

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .authorization import ResolvedPersonAuthorizer
from .errors import (
    AmbiguousPerson,
    Forbidden,
    NotAuthenticated,
    PersonNotFound,
    SfeduGatewayError,
)
from .intervals import DateInterval, local_date_interval
from .models import ScheduleResult
from .reader import ScheduleReader


class _ToolFailure(RuntimeError):
    """Internal marker so a safe message can be re-raised as an MCP ToolError."""


def _as_tool_error(exc: BaseException) -> ToolError:
    """Convert an expected domain failure into a message the model can read.

    The MCP SDK reports any non-ToolError exception as a generic
    "Error executing tool <name>" and drops the original text, so a mistyped
    person name would reach the model as an unexplained crash. Only known
    domain errors are converted; their messages are written to be safe.
    """
    if isinstance(exc, PersonNotFound):
        return ToolError("No person matched that name. Check the spelling, or search a "
                         "surname/first-name fragment.")
    if isinstance(exc, NotAuthenticated):
        return ToolError("Not signed in to Modeus, or the session expired. Run "
                         "`urfu-mcp auth` and try again.")
    if isinstance(exc, Forbidden):
        return ToolError(str(exc))
    if isinstance(exc, SfeduGatewayError):
        return ToolError(f"Modeus could not complete the request: {exc}")
    if isinstance(exc, ValueError):
        return ToolError(str(exc))
    return ToolError("The request could not be completed.")


class CurrentIdentityProvider(Protocol):
    """Return an authenticated, trusted current-person UUID, if available."""

    async def current_person(self) -> str | None: ...


class ScheduleTokenSupplier(Protocol):
    """Supply the Modeus token associated with an authenticated identity."""

    async def get_token(self, identity: str) -> str: ...


class PersonResolver(Protocol):
    """Search and resolve an explicit selector; never pick an implicit match."""

    async def resolve(
        self,
        selector: str,
        *,
        identity: str,
        token: str,
        selected_person_id: str | None = None,
    ) -> str: ...


class PersonAuthorizer(Protocol):
    """Decide whether an identity may read one selected person's schedule."""

    async def authorize(self, identity: str, person_id: str) -> bool: ...


class ScheduleReaderProtocol(Protocol):
    """Reader contract accepted by tool registration."""

    async def read(
        self,
        subjects: Sequence[str],
        interval: DateInterval,
        *,
        token: str,
    ) -> list[ScheduleResult]: ...


def _interval(
    date_value: str | None,
    period_start: str | None,
    period_end: str | None,
    timezone_name: str,
) -> DateInterval:
    if date_value is not None:
        if period_start is not None or period_end is not None:
            raise ValueError("provide a date or period, not both")
        selected = date.fromisoformat(date_value)
        return local_date_interval(selected, selected, timezone_name)
    if period_start is None or period_end is None:
        raise ValueError("provide a date or both period_start and period_end")
    return local_date_interval(
        date.fromisoformat(period_start), date.fromisoformat(period_end), timezone_name
    )


def _schedule_payload(schedules: list[ScheduleResult], *, multiple: bool) -> dict[str, object]:
    serialized: list[dict[str, object]] = []
    for result in schedules:
        item: dict[str, object] = result.model_dump(mode="json")
        item["from"] = item.pop("from_")
        serialized.append(item)
    if multiple:
        return {"schedules": serialized}
    return serialized[0]


def register_schedule_tools(
    server: MCPServer,
    *,
    reader: ScheduleReader | ScheduleReaderProtocol,
    identity_provider: CurrentIdentityProvider,
    token_supplier: ScheduleTokenSupplier,
    person_resolver: PersonResolver,
    person_authorizer: PersonAuthorizer | None = None,
    timezone_name: str = "Asia/Yekaterinburg",
) -> None:
    """Register the current-user and explicitly authorized person tools.

    The resolver is mandatory. By default, any non-nil person UUID returned by
    its complete search may be selected for reading; applications can inject a
    narrower policy if explicitly required. Identity/token failures stop before
    any schedule read.
    """
    if person_authorizer is None:
        person_authorizer = ResolvedPersonAuthorizer()

    async def identity_and_token() -> tuple[str, str]:
        identity = await identity_provider.current_person()
        if not identity:
            raise NotAuthenticated("No authenticated current user")
        token = await token_supplier.get_token(identity)
        if not token:
            raise NotAuthenticated("No Modeus token is available for the current user")
        return identity, token

    async def retrieve_user_schedule(
        date: str | None = None,
        period_start: str | None = None,
        period_end: str | None = None,
    ) -> dict[str, object]:
        """Retrieve the authenticated current user's complete schedule."""
        try:
            identity, token = await identity_and_token()
            interval = _interval(date, period_start, period_end, timezone_name)
            schedules = await reader.read([identity], interval, token=token)
        except (SfeduGatewayError, ValueError) as exc:
            raise _as_tool_error(exc) from exc
        return _schedule_payload(schedules, multiple=False)

    async def retrieve_person_schedule(
        date: str | None = None,
        period_start: str | None = None,
        period_end: str | None = None,
        person: str | None = None,
        persons: list[str] | None = None,
        person_selection: str | None = None,
        person_selections: list[str] | None = None,
    ) -> dict[str, object]:
        """Retrieve schedules for people explicitly resolved from a complete search."""
        try:
            return await _resolve_and_read(
                date, period_start, period_end, person, persons,
                person_selection, person_selections,
            )
        except (SfeduGatewayError, ValueError) as exc:
            raise _as_tool_error(exc) from exc

    async def _resolve_and_read(
        date: str | None,
        period_start: str | None,
        period_end: str | None,
        person: str | None,
        persons: list[str] | None,
        person_selection: str | None,
        person_selections: list[str] | None,
    ) -> dict[str, object]:
        identity, token = await identity_and_token()
        interval = _interval(date, period_start, period_end, timezone_name)
        if (person is None) == (persons is None):
            raise ValueError("provide exactly one of person or persons")
        selectors = [person] if person is not None else persons
        if not selectors or any(not selector.strip() for selector in selectors):
            raise ValueError("person selector must not be empty")
        if len(set(selectors)) != len(selectors):
            raise ValueError("person selectors must be unique")
        selections: list[str | None]
        if person is not None:
            if person_selections is not None:
                raise ValueError("use person_selection with person")
            selections = [person_selection]
        else:
            if person_selection is not None:
                raise ValueError("use person_selections with persons")
            if person_selections is not None and len(person_selections) != len(selectors):
                raise ValueError("person_selections must align with persons")
            selections = []
            if person_selections is None:
                selections.extend(None for _ in selectors)
            else:
                selections.extend(person_selections)
        if any(selection is not None and not selection.strip() for selection in selections):
            raise ValueError("selected person IDs must not be empty")

        resolved: list[str] = []
        for index, (selector, selected_person_id) in enumerate(zip(selectors, selections)):
            try:
                person_id = await person_resolver.resolve(
                    selector,
                    identity=identity,
                    token=token,
                    selected_person_id=selected_person_id,
                )
            except AmbiguousPerson as error:
                visible_candidates = []
                for candidate in error.candidates:
                    if await person_authorizer.authorize(identity, candidate.person_id):
                        visible_candidates.append(candidate.model_dump(mode="json"))
                if not visible_candidates:
                    raise Forbidden("No authorized person matches the selector") from None
                return {
                    "status": "selection_required",
                    "selector": selector,
                    "selection_index": index,
                    "resolved_person_ids": resolved,
                    "candidates": visible_candidates,
                }
            if not person_id:
                raise Forbidden("Person selector did not resolve to an authorized identity")
            if not await person_authorizer.authorize(identity, person_id):
                raise Forbidden("Not authorized to read the selected person's schedule")
            resolved.append(person_id)
        schedules = await reader.read(resolved, interval, token=token)
        return _schedule_payload(schedules, multiple=True)

    server.add_tool(
        retrieve_user_schedule,
        name="retrieve_user_schedule",
        description="Retrieve the authenticated current user's Modeus schedule for a date or inclusive date period.",
        structured_output=True,
    )
    server.add_tool(
        retrieve_person_schedule,
        name="retrieve_person_schedule",
        description="Retrieve schedules for an explicitly selected person or people found by a complete search.",
        structured_output=True,
    )


__all__ = [
    "CurrentIdentityProvider",
    "PersonAuthorizer",
    "PersonResolver",
    "ScheduleTokenSupplier",
    "register_schedule_tools",
]
