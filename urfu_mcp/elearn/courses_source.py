"""Bounded read-only fetches for authenticated Moodle pages."""

from __future__ import annotations

import json
import re
from typing import Protocol
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit

import httpx

from .courses_parser import MAX_HTML_BYTES
from .errors import InvalidUpstreamResponse

ELEAN_ORIGIN = "https://elearn.urfu.ru"
MY_COURSES_PATH = "/my/courses.php"
COURSE_PATH = "/course/view.php"
AJAX_PATH = "/lib/ajax/service.php"
AJAX_METHOD = "core_course_get_enrolled_courses_by_timeline_classification"
MAX_COURSES = 64
MAX_FILE_BYTES = 50_000_000
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

    @staticmethod
    def _safe_url(url: str, *, path: str) -> str:
        try:
            parsed = urlsplit(urljoin(ELEAN_ORIGIN + "/", url))
            decoded_parts = [unquote(part) for part in parsed.path.split("/")]
            if (
                parsed.scheme != "https" or parsed.netloc != "elearn.urfu.ru"
                or parsed.username is not None or parsed.password is not None
                or parsed.fragment or parsed.path != path
                or any(part in {".", ".."} or "/" in part or "\\" in part for part in decoded_parts)
            ):
                raise InvalidUpstreamResponse(_MESSAGE)
            query = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
            if len(query) != 1 or query[0][0] != "id" or not query[0][1].isascii() or not query[0][1].isdecimal():
                raise InvalidUpstreamResponse(_MESSAGE)
            return parsed.geturl()
        except (TypeError, ValueError):
            raise InvalidUpstreamResponse(_MESSAGE) from None

    @staticmethod
    def _safe_file_url(url: str) -> str:
        try:
            parsed = urlsplit(urljoin(ELEAN_ORIGIN + "/", url))
            decoded_parts = [unquote(part) for part in parsed.path.split("/")]
            if (
                parsed.scheme != "https" or parsed.netloc != "elearn.urfu.ru"
                or parsed.username is not None or parsed.password is not None or parsed.fragment
                or not parsed.path.startswith("/pluginfile.php/")
                or any(part in {".", ".."} or "/" in part or "\\" in part for part in decoded_parts)
                or len(parsed.path) > 2000
            ):
                raise InvalidUpstreamResponse(_MESSAGE)
            query = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
            if query not in ([], [("forcedownload", "1")], [("forcedownload", "0")]):
                raise InvalidUpstreamResponse(_MESSAGE)
            return parsed.geturl()
        except (TypeError, ValueError):
            raise InvalidUpstreamResponse(_MESSAGE) from None

    async def fetch_module(self, client: httpx.AsyncClient, url: str) -> str | None:
        safe_url = self._safe_url(url, path="/mod/resource/view.php")
        try:
            async with client.stream("GET", safe_url, follow_redirects=False, timeout=10.0,
                                    headers={"User-Agent": "Mozilla/5.0 (compatible; urfu-mcp)", "Accept": "text/html"}) as response:
                if response.status_code != 303:
                    raise InvalidUpstreamResponse(_MESSAGE)
                location = response.headers.get("location")
                if location is None:
                    raise InvalidUpstreamResponse(_MESSAGE)
                return self._safe_file_url(urljoin(safe_url, location))
        except InvalidUpstreamResponse:
            raise
        except Exception:  # noqa: BLE001 - sanitize upstream failures
            raise InvalidUpstreamResponse(_MESSAGE) from None

    async def fetch_folder(self, client: httpx.AsyncClient, url: str) -> str:
        safe_url = self._safe_url(url, path="/mod/folder/view.php")
        return await self._get(client, safe_url)

    async def fetch_file(self, client: httpx.AsyncClient, url: str, max_bytes: int = MAX_FILE_BYTES) -> bytes:
        safe_url = self._safe_file_url(url)
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 0 <= max_bytes <= MAX_FILE_BYTES:
            raise InvalidUpstreamResponse(_MESSAGE)
        try:
            async with client.stream("GET", safe_url, follow_redirects=False, timeout=30.0,
                                    headers={"User-Agent": "Mozilla/5.0 (compatible; urfu-mcp)", "Accept": "*/*"}) as response:
                if response.status_code != 200:
                    raise InvalidUpstreamResponse(_MESSAGE)
                raw_size = response.headers.get("content-length")
                if raw_size is not None and raw_size.isascii() and raw_size.isdecimal() and int(raw_size) > max_bytes:
                    raise InvalidUpstreamResponse(_MESSAGE)
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > max_bytes:
                        raise InvalidUpstreamResponse(_MESSAGE)
                    body.extend(chunk)
                return bytes(body)
        except InvalidUpstreamResponse:
            raise
        except Exception:  # noqa: BLE001 - sanitize untrusted upstream failures
            raise InvalidUpstreamResponse(_MESSAGE) from None

    async def fetch_file_metadata(self, client: httpx.AsyncClient, url: str) -> dict[str, str | int | None]:
        safe_url = self._safe_file_url(url)
        try:
            async with client.stream("GET", safe_url, follow_redirects=False, timeout=10.0,
                                    headers={"User-Agent": "Mozilla/5.0 (compatible; urfu-mcp)", "Accept": "*/*"}) as response:
                if response.status_code != 200:
                    raise InvalidUpstreamResponse(_MESSAGE)
                mime_type = response.headers.get("content-type", "").split(";", maxsplit=1)[0].strip().lower() or None
                raw_size = response.headers.get("content-length")
                size = int(raw_size) if raw_size is not None and raw_size.isascii() and raw_size.isdecimal() else None
                if size is not None and size > 2_000_000_000:
                    raise InvalidUpstreamResponse(_MESSAGE)
                modified = response.headers.get("last-modified")
                return {"mime_type": mime_type, "size_bytes": size, "modified_date": modified}
        except InvalidUpstreamResponse:
            raise
        except Exception:  # noqa: BLE001 - sanitize untrusted upstream failures
            raise InvalidUpstreamResponse(_MESSAGE) from None


__all__ = ["COURSE_PATH", "ELEAN_ORIGIN", "MAX_COURSES", "MY_COURSES_PATH", "ELearnCoursesSource", "ELearnPageSource"]
