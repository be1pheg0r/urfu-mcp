"""Resolve eLearn course selectors only against the authenticated course list."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

import httpx

from .courses_parser import CoursePageParser, MyCoursesParser
from .courses_source import ELearnPageSource
from .errors import (
    AmbiguousCourse,
    CourseNotFound,
    InvalidUpstreamResponse,
    UpstreamUnavailable,
)
from .public_models import CourseContent, CourseSummary


class ELearnCourseReaderPort(Protocol):
    """Structural reader contract used at the MCP boundary."""

    async def read_courses(self, client: httpx.AsyncClient) -> tuple[CourseSummary, ...]: ...

    async def read_course(self, client: httpx.AsyncClient, selector: str) -> CourseContent: ...


class ELearnCourseReader:
    def __init__(self, source: ELearnPageSource) -> None:
        self._source = source
        self._courses_parser = MyCoursesParser()
        self._course_parser = CoursePageParser()

    async def read_courses(self, client: httpx.AsyncClient) -> tuple[CourseSummary, ...]:
        try:
            payload = await self._source.fetch_courses(client)
        except InvalidUpstreamResponse:
            raise
        except Exception:  # noqa: BLE001 - sanitize private source/parser failures
            raise UpstreamUnavailable("The eLearn course list is unavailable") from None
        try:
            return self._courses_parser.parse(payload)
        except InvalidUpstreamResponse:
            raise
        except Exception:  # noqa: BLE001 - sanitize private source/parser failures
            raise InvalidUpstreamResponse("The eLearn course list could not be parsed safely") from None

    async def read_course(self, client: httpx.AsyncClient, selector: str) -> CourseContent:
        courses = await self.read_courses(client)
        selected = select_course(courses, selector)
        try:
            payload = await self._source.fetch_course(client, selected.course_id)
        except InvalidUpstreamResponse:
            raise
        except Exception:  # noqa: BLE001 - sanitize private source/parser failures
            raise UpstreamUnavailable("The eLearn course page is unavailable") from None
        try:
            content = self._course_parser.parse(payload, course_id=selected.course_id)
        except InvalidUpstreamResponse:
            raise
        except Exception:  # noqa: BLE001 - sanitize private source/parser failures
            raise InvalidUpstreamResponse("The eLearn course page could not be parsed safely") from None
        if content.course_id != selected.course_id:
            raise InvalidUpstreamResponse("The eLearn course page does not match the selected course")
        return content


def normalize_course_name(value: str) -> str:
    return " ".join(value.split()).casefold()


def select_course(courses: tuple[CourseSummary, ...], selector: str) -> CourseSummary:
    if not isinstance(selector, str) or not selector.strip():
        raise CourseNotFound("No eLearn course matches the requested selector")
    if selector.isascii() and selector.isdecimal() and 1 <= len(selector) <= 12:
        course_id = int(selector)
        matches = [course for course in courses if course.course_id == course_id]
        if not matches:
            raise CourseNotFound("No eLearn course matches the requested selector")
        return matches[0]
    wanted = normalize_course_name(selector)
    matches = [course for course in courses if normalize_course_name(course.name) == wanted]
    if not matches:
        raise CourseNotFound("No eLearn course matches the requested selector")
    if len(matches) > 1:
        raise AmbiguousCourse("More than one eLearn course matches the requested name")
    return matches[0]


def local_today() -> date:
    return datetime.now(UTC).astimezone(ZoneInfo("Asia/Yekaterinburg")).date()


__all__ = [
    "ELearnCourseReader",
    "ELearnCourseReaderPort",
    "normalize_course_name",
    "select_course",
]
