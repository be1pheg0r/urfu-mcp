"""Protected BRS source port; no HTTP route is guessed here."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote, urljoin, urlsplit

import httpx

from .brs_parser import MAX_HTML_BYTES, _HTMLTree, _Node, _single
from .errors import InvalidUpstreamResponse

_BASE = "https://istudent.urfu.ru/s/http-urfu-ru-ru-students-study-brs"
_DETAIL = _BASE + "/discipline"
_PERIOD = re.compile(r"(\d{4})/(\d{4}) — (Осенний|Весенний)\Z")
_MAX_SUBJECTS = 64


@dataclass(frozen=True)
class BRSPeriodRequest:
    year: str
    semester: str

    @property
    def label(self) -> str:
        return f"{self.year} — {self.semester}"

    @classmethod
    def parse(cls, value: str) -> BRSPeriodRequest:
        match = _PERIOD.fullmatch(value) if isinstance(value, str) else None
        if not match or int(match.group(2)) != int(match.group(1)) + 1:
            raise ValueError("period must be YYYY/YYYY — Осенний or Весенний")
        return cls(f"{match.group(1)}/{match.group(2)}", match.group(3))


@dataclass(frozen=True)
class BRSPeriodPages:
    overview: bytes
    details: tuple[bytes, ...]


def _verified_option_url(option: _Node) -> str:
    value = option.attrs.get("value", "")
    try:
        # The site emits same-origin path-relative option values. Resolve against
        # the fixed page, then validate the complete URL before returning it.
        url = httpx.URL(urljoin(_BASE, value))
    except (TypeError, ValueError):
        raise InvalidUpstreamResponse("BRS period option URL is untrusted") from None
    if (
        url.scheme != "https" or url.host != "istudent.urfu.ru"
        or url.port not in (None, 443) or url.path != urlsplit(_BASE).path
        or url.username or url.password or url.fragment
    ):
        raise InvalidUpstreamResponse("BRS period option URL is untrusted")
    keys = [key for key, _ in url.params.multi_items()]
    if len(keys) != len(set(keys)) or not {"studentUid", "groupId", "year"} <= set(keys) or set(keys) - {"studentUid", "groupId", "year", "semester"}:
        raise InvalidUpstreamResponse("BRS period option parameters are untrusted")
    return str(url)


def _select(tree: _HTMLTree, selector: str, label: str) -> tuple[bool, str]:
    select = _single([n for n in tree.root.descendants() if n.tag == "select" and n.attrs.get("id") == selector])
    matches = [n for n in select.children if n.tag == "option" and n.all_text() == label]
    chosen = _single(matches)
    return "selected" in chosen.attrs, _verified_option_url(chosen)


class IStudentBRSPageSource:
    """Read-only bounded fetch through an already authenticated per-identity HTTP client.

    The session provider owns login, identity binding, cookie lifetime and closure.
    No browser cookie or Modeus token is extracted by this source.
    """

    async def _get(self, client: httpx.AsyncClient, url: str) -> bytes:
        headers = {"Accept": "text/html", "User-Agent": "Mozilla/5.0 (compatible; urfu-mcp)"}
        # The protected AJAX detail route requires the site's observed XHR context.
        # Scope these headers to the fixed detail endpoint only; do not leak them
        # to period navigation or arbitrary URLs.
        if urlsplit(url).scheme == "https" and urlsplit(url).netloc == "istudent.urfu.ru" and urlsplit(url).path == urlsplit(_DETAIL).path:
            headers.update({"X-Requested-With": "XMLHttpRequest", "Referer": _BASE})
        async with client.stream("GET", url, follow_redirects=False, timeout=10.0,
                                 headers=headers) as response:
            if response.status_code != 200 or response.headers.get("content-type", "").split(";")[0].strip().lower() not in {"text/html", "application/xhtml+xml"}:
                raise InvalidUpstreamResponse("BRS page is unavailable or unrecognized")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > MAX_HTML_BYTES:
                    raise InvalidUpstreamResponse("BRS page exceeds the size limit")
                body.extend(chunk)
            return bytes(body)

    async def fetch_period(self, identity: str, session: object, period: BRSPeriodRequest) -> BRSPeriodPages:
        del identity  # the provider must bind this session to the resolved identity
        if not isinstance(session, httpx.AsyncClient):
            raise InvalidUpstreamResponse("BRS requires an iStudent HTTP session")
        if not isinstance(period, BRSPeriodRequest):
            raise InvalidUpstreamResponse("BRS period is required")
        page = await self._get(session, _BASE)
        for selector, label in (("year-select", period.year), ("semester-select", period.semester)):
            tree = _HTMLTree()
            tree.feed(page.decode("utf-8"))
            selected, url = _select(tree, selector, label)
            if not selected:
                page = await self._get(session, url)
        tree = _HTMLTree()
        tree.feed(page.decode("utf-8"))
        for selector, label in (("year-select", period.year), ("semester-select", period.semester)):
            selected, _ = _select(tree, selector, label)
            if not selected:
                raise InvalidUpstreamResponse("BRS selected period disagrees with request")
        outers = [n for n in tree.root.descendants() if "discipline-outer-container" in n.classes()]
        if not 0 < len(outers) <= _MAX_SUBJECTS:
            raise InvalidUpstreamResponse("BRS subject count is unverified")
        ids = []
        for outer in outers:
            row = _single([n for n in outer.children if "discipline" in n.classes()])
            value = row.attrs.get("data-id", "")
            if not re.fullmatch(r"\d{1,16}", value) or value in ids:
                raise InvalidUpstreamResponse("BRS discipline identifier is untrusted")
            ids.append(value)
        details = []
        for value in ids:
            url = f"{_DETAIL}?disciplineId={value}&backlink={quote(quote(_BASE, safe=''), safe='')}"
            details.append(await self._get(session, url))
        return BRSPeriodPages(page, tuple(details))


class BRSPageSource(Protocol):
    """Fetch one identity's protected page through its iStudent session."""

    async def fetch(self, identity: str, session: object) -> str | bytes: ...
