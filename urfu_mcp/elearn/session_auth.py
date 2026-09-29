"""Restricted authenticated-session access for Moodle eLearn pages."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx

from urfu_mcp.html_tree import _HTMLTree

from .session_store import ELearnSessionStoreError

ELEAN_HOST = "elearn.urfu.ru"
ELEAN_ORIGIN = "https://elearn.urfu.ru"
ELEAN_MY_COURSES_PATH = "/my/courses.php"
MAX_PAGE_BYTES = 2_000_000
# The course list is rendered progressively by Moodle, so a valid first response
# can still be empty. Retry briefly instead of failing a working session.
_PROTECTED_PAGE_ATTEMPTS = 4
_PROTECTED_PAGE_RETRY_SECONDS = 1.5
_COURSE_PATH = re.compile(r"/course/view\.php\Z")
_COURSE_ID = re.compile(r"\d+\Z")


def _valid_session_url(url: httpx.URL) -> bool:
    """Allow protected Moodle paths only on the exact HTTPS origin."""
    return (
        url.scheme == "https" and url.host == ELEAN_HOST and url.port in (None, 443)
        and (url.path == ELEAN_MY_COURSES_PATH or url.path.startswith("/course/view.php"))
        and not url.username and not url.password and not url.fragment
    )


class _RestrictedTransport(httpx.AsyncBaseTransport):
    def __init__(self, transport: httpx.AsyncBaseTransport | None) -> None:
        self._transport = transport if transport is not None else httpx.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not _valid_session_url(request.url):
            raise ValueError("eLearn session requests must use the protected HTTPS origin")
        return await self._transport.handle_async_request(request)

    async def aclose(self) -> None:
        await self._transport.aclose()


def looks_like_protected_my_courses(html: str) -> bool:
    """Require a Moodle course link and reject the explicit anonymous marker."""
    tree = _HTMLTree()
    try:
        tree.feed(html)
    except ValueError:
        return False
    nodes = tree.root.descendants()
    if not any(node.tag == "html" for node in nodes):
        return False
    if any(node.tag == "body" and "notloggedin" in node.classes() for node in nodes):
        return False
    for node in nodes:
        if node.tag != "a":
            continue
        try:
            resolved = urlsplit(urljoin(ELEAN_ORIGIN + ELEAN_MY_COURSES_PATH, node.attrs.get("href", "")))
            ids = parse_qs(resolved.query).get("id", [])
            if (resolved.scheme == "https" and resolved.netloc == ELEAN_HOST
                    and _COURSE_PATH.fullmatch(resolved.path) and len(ids) == 1
                    and _COURSE_ID.fullmatch(ids[0])):
                return True
        except ValueError:
            continue
    return False


class StoredELearnSessionProvider:
    """Create owned clients from stored Moodle sessions and verify dashboard access."""

    def __init__(
        self,
        store: object,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._transport = transport
        self._now = now
        self._clients: list[httpx.AsyncClient] = []

    async def get_session(self, identity: str) -> httpx.AsyncClient:
        client: httpx.AsyncClient | None = None
        try:
            record = self._store.load(identity)  # type: ignore[attr-defined]
            if record is None:
                raise ELearnSessionStoreError("Stored eLearn session is unavailable")
            # Revalidate time at use, even if the supplied store is not our implementation.
            if record.expires_at <= self._now():
                raise ELearnSessionStoreError("Stored eLearn session is invalid or expired")
            cookies = httpx.Cookies()
            cookies.set("MoodleSession", record.moodle_session_id, domain=ELEAN_HOST, path="/")
            client = httpx.AsyncClient(
                base_url=ELEAN_ORIGIN,
                cookies=cookies,
                follow_redirects=False,
                timeout=15,
                transport=_RestrictedTransport(self._transport),
            )
            # Moodle renders the enrolled-course list progressively, so the
            # first response can be a valid but still-empty page. Retry a
            # bounded number of times before treating the session as invalid.
            verified = False
            for attempt in range(_PROTECTED_PAGE_ATTEMPTS):
                response = await client.get(ELEAN_MY_COURSES_PATH)
                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if (response.status_code == 200 and content_type == "text/html"
                        and len(response.content) <= MAX_PAGE_BYTES
                        and looks_like_protected_my_courses(response.text)):
                    verified = True
                    break
                if attempt < _PROTECTED_PAGE_ATTEMPTS - 1:
                    await asyncio.sleep(_PROTECTED_PAGE_RETRY_SECONDS)
            if not verified:
                await client.aclose()
                raise ELearnSessionStoreError("eLearn protected page validation failed")
            self._clients.append(client)
            return client
        except ELearnSessionStoreError:
            raise
        except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError):
            if client is not None:
                await client.aclose()
            raise ELearnSessionStoreError("Could not validate stored eLearn session") from None
        except BaseException:
            if client is not None:
                await client.aclose()
            raise

    async def aclose(self) -> None:
        """Close every client created by this provider."""
        clients, self._clients = self._clients, []
        for client in clients:
            await client.aclose()


__all__ = [
    "ELEAN_HOST",
    "ELEAN_MY_COURSES_PATH",
    "ELEAN_ORIGIN",
    "MAX_PAGE_BYTES",
    "StoredELearnSessionProvider",
    "_RestrictedTransport",
    "_valid_session_url",
    "looks_like_protected_my_courses",
]
