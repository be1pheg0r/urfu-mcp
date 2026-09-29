"""Resolve eLearn course selectors only against the authenticated course list."""

from __future__ import annotations

import os
import unicodedata
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Protocol, cast
from urllib.parse import unquote, urljoin, urlsplit
from zoneinfo import ZoneInfo

import httpx

from .courses_parser import CoursePageParser, MyCoursesParser, _HTMLTree
from .courses_source import MAX_FILE_BYTES, ELearnPageSource
from .errors import (
    AmbiguousCourse,
    CourseNotFound,
    InvalidInput,
    InvalidUpstreamResponse,
    UpstreamUnavailable,
)
from .public_models import CourseContent, CourseFile, CourseSummary


def default_download_directory() -> Path:
    """Return the per-user eLearn download directory used when config has no override."""
    return Path.home() / ".urfu-mcp" / "files"


def _prepare_download_directory(destination_dir: Path | str | None) -> Path:
    requested = Path(destination_dir).expanduser() if destination_dir is not None else default_download_directory()
    destination = requested.resolve(strict=False)
    for ancestor in (destination, *destination.parents):
        git_entry = ancestor / ".git"
        if git_entry.exists() or git_entry.is_symlink():
            raise InvalidInput("Downloads cannot be saved inside a Git working tree")
    try:
        destination.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(destination, 0o700)
        return destination.resolve(strict=True)
    except OSError:
        raise UpstreamUnavailable("The eLearn download directory could not be created securely") from None


class ELearnFileSource(Protocol):
    async def fetch_module(self, client: httpx.AsyncClient, url: str) -> str | None: ...
    async def fetch_folder(self, client: httpx.AsyncClient, url: str) -> str: ...
    async def fetch_file_metadata(self, client: httpx.AsyncClient, url: str) -> dict[str, str | int | None]: ...
    async def fetch_file(self, client: httpx.AsyncClient, url: str, max_bytes: int) -> bytes: ...


class ELearnCourseReaderPort(Protocol):
    """Structural reader contract used at the MCP boundary."""

    async def read_courses(self, client: httpx.AsyncClient) -> tuple[CourseSummary, ...]: ...

    async def read_course(self, client: httpx.AsyncClient, selector: str) -> CourseContent: ...

    async def read_files(self, client: httpx.AsyncClient, selector: str) -> tuple[CourseFile, ...]: ...

    async def download_file(
        self, client: httpx.AsyncClient, selector: str, file_name: str, destination_dir: Path | str | None = None
    ) -> tuple[CourseFile, Path]: ...


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
        return await self._read_selected_course(client, selected)

    async def _read_selected_course(self, client: httpx.AsyncClient, selected: CourseSummary) -> CourseContent:
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

    async def read_files(self, client: httpx.AsyncClient, selector: str) -> tuple[CourseFile, ...]:
        courses = await self.read_courses(client)
        selected = select_course(courses, selector)
        content = await self._read_selected_course(client, selected)
        file_source = cast(ELearnFileSource, self._source)
        files: list[CourseFile] = []
        seen: set[str] = set()
        try:
            for section in content.sections:
                for item in section.materials:
                    if item.restricted or item.url is None or item.modtype not in {"resource", "folder"}:
                        continue
                    if item.modtype == "resource":
                        direct_url = await file_source.fetch_module(client, item.url)
                        candidates: list[tuple[str, str]] = []
                        if direct_url:
                            candidates.append((direct_url, item.name))
                    else:
                        html = await file_source.fetch_folder(client, item.url)
                        candidates = [(url, name) for url, name in _folder_file_links(html, item.url) if url is not None]
                    for direct_url, display_name in candidates:
                        if direct_url in seen:
                            continue
                        seen.add(direct_url)
                        parsed = urlsplit(direct_url)
                        file_name = unquote(parsed.path.rsplit("/", maxsplit=1)[-1])
                        if not file_name or file_name in {".", ".."} or "/" in file_name or "\\" in file_name:
                            raise InvalidUpstreamResponse("eLearn file name is unrecognized")
                        metadata = await file_source.fetch_file_metadata(client, direct_url)
                        raw_size = metadata.get("size_bytes")
                        size_bytes = raw_size if isinstance(raw_size, int) and not isinstance(raw_size, bool) else None
                        modified = _iso_date(metadata.get("modified_date"))
                        try:
                            files.append(CourseFile(
                                display_name=display_name,
                                file_name=file_name,
                                mime_type=_optional_string(metadata.get("mime_type")),
                                size_bytes=size_bytes,
                                modified_date=modified,
                                download_url=direct_url,
                                section_name=section.name,
                            ))
                        except ValueError:
                            raise InvalidUpstreamResponse("eLearn file metadata is unrecognized") from None
                        if len(files) > 500:
                            raise InvalidUpstreamResponse("eLearn course exceeds the file limit")
            return tuple(files)
        except InvalidUpstreamResponse:
            raise
        except Exception:  # noqa: BLE001 - sanitize private source/parser failures
            raise UpstreamUnavailable("The eLearn course files are unavailable") from None

    async def download_file(
        self, client: httpx.AsyncClient, selector: str, file_name: str, destination_dir: Path | str | None = None
    ) -> tuple[CourseFile, Path]:
        if not _safe_file_name(file_name):
            raise InvalidInput("file_name must be a safe file name")
        root = _prepare_download_directory(destination_dir)
        files = await self.read_files(client, selector)
        selected = next((item for item in files if item.file_name == file_name), None)
        if selected is None:
            raise CourseNotFound("No listed file matches the requested file name")
        source = cast(ELearnFileSource, self._source)
        try:
            content = await source.fetch_file(client, selected.download_url, MAX_FILE_BYTES)
            target = root / file_name
            if target.parent != root:
                raise InvalidInput("file_name must be a safe file name")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
            descriptor = os.open(target, flags, 0o600)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(content)
            except Exception:
                target.unlink(missing_ok=True)
                raise
            return selected, target.resolve(strict=True)
        except (InvalidInput, CourseNotFound, InvalidUpstreamResponse, UpstreamUnavailable):
            raise
        except FileExistsError:
            raise InvalidInput("A file with this name already exists in the destination") from None
        except Exception:  # noqa: BLE001 - sanitize network and filesystem failures
            raise UpstreamUnavailable("The eLearn file could not be saved safely") from None


def _safe_file_name(value: str) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value not in {".", ".."}
        and not Path(value).is_absolute()
        and "/" not in value
        and "\\" not in value
        and not any(unicodedata.category(character) == "Cc" for character in value)
    )


def _folder_file_links(html: str, base_url: str) -> list[tuple[str | None, str]]:
    tree = _HTMLTree()
    tree.feed(html)
    tree.close()
    bodies = [node for node in tree.root.descendants() if node.tag == "body"]
    if len(bodies) != 1 or any("notloggedin" in node.classes() for node in bodies[0].descendants()):
        raise InvalidUpstreamResponse("eLearn folder page is unrecognized")
    result: list[tuple[str | None, str]] = []
    for node in bodies[0].descendants():
        href = node.attrs.get("href", "")
        parsed = urlsplit(urljoin(base_url, href)) if href else None
        if node.tag != "a" or parsed is None or parsed.path.startswith("/mod/folder/view.php"):
            continue
        if parsed.path.startswith("/pluginfile.php/"):
            name = node.all_text().strip()
            if not name:
                raise InvalidUpstreamResponse("eLearn folder file name is missing")
            result.append((parsed.geturl(), name[:500]))
            if len(result) > 500:
                raise InvalidUpstreamResponse("eLearn folder exceeds the file limit")
    if not result:
        raise InvalidUpstreamResponse("eLearn folder contains no recognized file links")
    return result


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _iso_date(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        return parsed.astimezone(UTC).date().isoformat()
    except (TypeError, ValueError, OverflowError):
        raise InvalidUpstreamResponse("eLearn file modification date is unrecognized") from None


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
