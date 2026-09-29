"""Offline parsers for authenticated Moodle course pages."""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

from urfu_mcp.html_tree import _HTMLTree, _Node

from .errors import InvalidUpstreamResponse
from .public_models import CourseActivity, CourseContent, CourseSection, CourseSummary

MAX_HTML_BYTES = 2_000_000
COURSE_HREF = re.compile(
    r"(?:/course/view\.php\?id=|https://elearn\.urfu\.ru/course/view\.php\?id=)(\d{1,12})\Z"
)
_SECTION_ID = re.compile(r"section-[0-9]+\Z")
_MODTYPE = re.compile(r"modtype_([A-Za-z0-9_-]+)\Z")


def _single(nodes: list[_Node], *, what: str) -> _Node:
    if len(nodes) != 1:
        raise InvalidUpstreamResponse(f"eLearn {what} is unrecognized")
    return nodes[0]


def _tree(payload: str | bytes) -> _HTMLTree:
    if isinstance(payload, bytes):
        if len(payload) > MAX_HTML_BYTES:
            raise InvalidUpstreamResponse("eLearn HTML exceeds the size limit")
        try:
            html = payload.decode("utf-8")
        except UnicodeDecodeError:
            raise InvalidUpstreamResponse("eLearn HTML encoding is unsupported") from None
    elif isinstance(payload, str):
        if len(payload.encode("utf-8")) > MAX_HTML_BYTES:
            raise InvalidUpstreamResponse("eLearn HTML exceeds the size limit")
        html = payload
    else:
        raise InvalidUpstreamResponse("eLearn HTML type is unsupported")
    tree = _HTMLTree()
    tree.feed(html)
    tree.close()
    return tree


def _body(tree: _HTMLTree) -> _Node | None:
    bodies = [node for node in tree.root.descendants() if node.tag == "body"]
    if len(bodies) > 1:
        raise InvalidUpstreamResponse("eLearn HTML body is unrecognized")
    body = bodies[0] if bodies else None
    if body is not None and any("notloggedin" in node.classes() for node in (body, *body.descendants())):
        raise InvalidUpstreamResponse("eLearn page requires authentication")
    return body


def _ancestors(root: _Node, target: _Node) -> list[_Node]:
    def walk(node: _Node, stack: list[_Node]) -> list[_Node] | None:
        if node is target:
            return stack
        for child in node.children:
            result = walk(child, [*stack, node])
            if result is not None:
                return result
        return None

    return walk(root, []) or []


class MyCoursesParser:
    def parse(self, html: str | bytes) -> tuple[CourseSummary, ...]:
        if isinstance(html, str) and html.lstrip().startswith("["):
            return self._parse_ajax(html)
        tree = _tree(html)
        body = _body(tree)
        nodes = body.descendants() if body else tree.root.descendants()
        anchors = [n for n in nodes if n.tag == "a" and COURSE_HREF.fullmatch(n.attrs.get("href", ""))]
        if not anchors:
            raise InvalidUpstreamResponse("eLearn course list has no recognized courses")
        found: dict[int, CourseSummary] = {}
        for anchor in anchors:
            match = COURSE_HREF.fullmatch(anchor.attrs.get("href", ""))
            assert match is not None
            course_id = int(match.group(1))
            name = anchor.all_text().strip()
            if not name:
                raise InvalidUpstreamResponse("eLearn course name is missing")
            heading: _Node | None = None
            for ancestor in _ancestors(tree.root, anchor):
                if ancestor.tag in {"h2", "h3", "h4"}:
                    heading = ancestor
                    break
            group = heading.all_text().strip() if heading else ""
            item = CourseSummary(course_id=course_id, name=name, progress_group=group if group and group != name else None)
            previous = found.get(course_id)
            if previous is not None and previous.name != name:
                raise InvalidUpstreamResponse("eLearn course identifiers are inconsistent")
            if previous is None:
                found[course_id] = item
            if len(found) > 64:
                raise InvalidUpstreamResponse("eLearn course list exceeds the supported size")
        return tuple(found.values())

    def _parse_ajax(self, payload: str) -> tuple[CourseSummary, ...]:
        try:
            response = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            raise InvalidUpstreamResponse("eLearn course list response is malformed") from None
        if not isinstance(response, list) or len(response) != 1 or not isinstance(response[0], dict):
            raise InvalidUpstreamResponse("eLearn course list response is unrecognized")
        data = response[0].get("data")
        if not isinstance(data, dict) or not isinstance(data.get("courses"), list):
            raise InvalidUpstreamResponse("eLearn course list response is unrecognized")
        records = data["courses"]
        if not records or len(records) > 64:
            raise InvalidUpstreamResponse("eLearn course list has no recognized courses or exceeds the supported size")
        courses: list[CourseSummary] = []
        seen: set[int] = set()
        for record in records:
            if not isinstance(record, dict):
                raise InvalidUpstreamResponse("eLearn course record is unrecognized")
            course_id = record.get("id")
            name = record.get("fullname") or record.get("fullnamedisplay")
            short_name = record.get("shortname")
            if isinstance(course_id, bool) or not isinstance(course_id, int) or course_id <= 0:
                raise InvalidUpstreamResponse("eLearn course identifier is unrecognized")
            if not isinstance(name, str) or not name.strip():
                raise InvalidUpstreamResponse("eLearn course name is missing")
            if short_name is not None and not isinstance(short_name, str):
                raise InvalidUpstreamResponse("eLearn course short name is unrecognized")
            if course_id in seen:
                raise InvalidUpstreamResponse("eLearn course identifiers are inconsistent")
            seen.add(course_id)
            try:
                courses.append(CourseSummary(course_id=course_id, name=name.strip(), short_name=short_name or None))
            except ValueError:
                raise InvalidUpstreamResponse("eLearn course record is unrecognized") from None
        return tuple(courses)


def _restriction_text(value: str) -> bool:
    normalized = value.casefold()
    return any(word in normalized for word in ("restricted", "not available", "unavailable", "огранич", "недоступ"))


class CoursePageParser:
    def parse(self, html: str | bytes, *, course_id: int) -> CourseContent:
        tree = _tree(html)
        body = _body(tree)
        nodes = body.descendants() if body else tree.root.descendants()
        name_nodes = body.descendants() if body else tree.root.descendants()
        titles = [n.all_text().strip() for n in name_nodes if n.tag == "h1" and n.all_text().strip()]
        if not titles:
            titles = [n.all_text().strip() for n in tree.root.descendants() if n.tag == "title" and n.all_text().strip()]
        name = titles[0] if titles else None
        sections_nodes = [n for n in nodes if "section" in n.classes() or _SECTION_ID.fullmatch(n.attrs.get("id", ""))]
        sections: list[CourseSection] = []
        activity_count = 0
        for section in sections_nodes:
            inside = section.descendants()
            headings = [n for n in inside if n.tag in {"h2", "h3", "h4", "h5"}]
            named = [n for n in headings if "sectionname" in n.classes()]
            heading = named[0] if named else (headings[0] if headings else None)
            section_name = heading.all_text().strip() if heading else ""
            section_id = section.attrs.get("id")
            if section_id == "section-0" and not section_name:
                section_name = "Общая информация"
            activities = [n for n in inside if "activity" in n.classes()]
            output: list[CourseActivity] = []
            for activity in activities:
                descendants = [activity, *activity.descendants()]
                name_nodes = [n for n in descendants if "activityname" in n.classes()]
                if not name_nodes:
                    name_nodes = [n for n in descendants if "instancename" in n.classes()]
                anchors = [n for n in descendants if n.tag == "a"]
                activity_name = (name_nodes[0].all_text() if name_nodes else anchors[0].all_text() if anchors else "").strip()
                if not activity_name:
                    raise InvalidUpstreamResponse("eLearn activity name is missing")
                href = anchors[0].attrs.get("href") if anchors else None
                url = None
                if href:
                    try:
                        scheme = urlsplit(href).scheme.lower()
                    except ValueError:
                        scheme = ""
                    if scheme in {"http", "https"}:
                        url = href
                modtype = next((m.group(1) for n in descendants for c in n.classes() if (m := _MODTYPE.fullmatch(c))), None)
                restricted = any(n.classes() & {"restricted", "locked"} for n in descendants) or any("availability" in n.classes() and _restriction_text(n.all_text()) for n in descendants)
                output.append(CourseActivity(name=activity_name, modtype=modtype, url=url, restricted=restricted))
                activity_count += 1
                if activity_count > 200:
                    raise InvalidUpstreamResponse("eLearn course exceeds the activity limit")
            sections.append(CourseSection(name=section_name, section_id=section_id, activities=tuple(output)))
            if len(sections) > 100:
                raise InvalidUpstreamResponse("eLearn course exceeds the section limit")
        return CourseContent(course_id=course_id, name=name, sections=tuple(sections))


__all__ = ["COURSE_HREF", "MAX_HTML_BYTES", "CoursePageParser", "MyCoursesParser", "_HTMLTree", "_Node", "_single"]
