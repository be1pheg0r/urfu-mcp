"""Bounded read-only fetches for authenticated Moodle pages."""

from __future__ import annotations

import json
import re
from typing import Protocol

import httpx

from .courses_parser import MAX_HTML_BYTES
from .errors import InvalidUpstreamResponse

ELEAN_ORIGIN = "https://elearn.urfu.ru"
MY_COURSES_PATH = "/my/courses.php"
COURSE_PATH = "/course/view.php"
AJAX_PATH = "/lib/ajax/service.php"
AJAX_METHOD = "core_course_get_enrolled_courses_by_timeline_classification"
MAX_COURSES = 64
_MESSAGE = "eLearn page is unavailable or unrecognized"
_SESSKEY = re.compile(r'"sesskey"\s*:\s*"([^"\\]{1,128})"')


class ELearnPageSource(Protocol):
    async def fetch_courses(self, client: httpx.AsyncClient) -> str: ...

    async def fetch_course(self, client: httpx.AsyncClient, course_id: int) -> str: ...


class ELearnCoursesSource:
    async def _get(self, client: httpx.AsyncClient, url: str) -> str:
        try:
            async with client.stream(
                "GET", url, follow_redirects=False, timeout=10.0,
                headers={"User-Agent": "Mozilla/5.0 (compatible; urfu-mcp)", "Accept": "text/html"},
            ) as response:
                content_type = response.headers.get("content-type", "").split(";", maxsplit=1)[0].strip().lower()
                if response.status_code != 200 or content_type not in {"text/html", "application/xhtml+xml"}:
                    raise InvalidUpstreamResponse(_MESSAGE)
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > MAX_HTML_BYTES:
                        raise InvalidUpstreamResponse(_MESSAGE)
                    body.extend(chunk)
            return bytes(body).decode("utf-8")
        except InvalidUpstreamResponse:
            raise
        except Exception:  # noqa: BLE001 - sanitize untrusted upstream failures
            raise InvalidUpstreamResponse(_MESSAGE) from None

    async def fetch_courses(self, client: httpx.AsyncClient) -> str:
        page = await self._get(client, ELEAN_ORIGIN + MY_COURSES_PATH)
        match = _SESSKEY.search(page)
        if match is None:
            raise InvalidUpstreamResponse(_MESSAGE)
        sesskey = match.group(1)
        url = (
            f"{ELEAN_ORIGIN}{AJAX_PATH}?sesskey={sesskey}"
            f"&info={AJAX_METHOD}"
        )
        request_payload = [{
            "index": 0,
            "methodname": AJAX_METHOD,
            "args": {"classification": "all", "limit": 100, "offset": 0, "sort": "startdate"},
        }]
        try:
            async with client.stream(
                "POST", url, json=request_payload, follow_redirects=False, timeout=10.0,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; urfu-mcp)",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
            ) as response:
                content_type = response.headers.get("content-type", "").split(";", maxsplit=1)[0].strip().lower()
                if response.status_code != 200 or content_type != "application/json":
                    raise InvalidUpstreamResponse(_MESSAGE)
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > MAX_HTML_BYTES:
                        raise InvalidUpstreamResponse(_MESSAGE)
                    body.extend(chunk)
            decoded = bytes(body).decode("utf-8")
            json.loads(decoded)
            return decoded
        except InvalidUpstreamResponse:
            raise
        except Exception:  # noqa: BLE001 - sanitize untrusted upstream failures
            raise InvalidUpstreamResponse(_MESSAGE) from None

    async def fetch_course(self, client: httpx.AsyncClient, course_id: int) -> str:
        if isinstance(course_id, bool) or not isinstance(course_id, int) or course_id <= 0:
            raise InvalidUpstreamResponse(_MESSAGE)
        return await self._get(client, ELEAN_ORIGIN + COURSE_PATH + "?id=" + str(course_id))


__all__ = ["COURSE_PATH", "ELEAN_ORIGIN", "MAX_COURSES", "MY_COURSES_PATH", "ELearnCoursesSource", "ELearnPageSource"]
