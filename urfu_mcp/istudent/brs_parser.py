"""Offline parser for the observed protected BRS HTML; no session/source wiring."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from html.parser import HTMLParser
from typing import Protocol

from .errors import InvalidUpstreamResponse
from .public_models import (
    BRSCompleteness,
    BRSPeriod,
    BRSPointsStatus,
    BRSResult,
    BRSSubject,
)

MAX_HTML_BYTES = 2_000_000
_VOID = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
_SCORE = re.compile(r"\d+(?:[.,]\d+)?\Z")
_YEAR = re.compile(r"\d{4}/\d{4}\Z")


class BRSPageParser(Protocol):
    """Normalize a verified source payload; implementations own schema evidence."""

    def parse(self, payload: str | bytes, *, as_of: date) -> BRSResult: ...


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[_Node] = field(default_factory=list)
    own_text: list[str] = field(default_factory=list)
    closed: bool = False

    def classes(self) -> set[str]:
        return set(self.attrs.get("class", "").split())

    def text(self) -> str:
        return " ".join(" ".join(self.own_text).split())

    def all_text(self) -> str:
        return " ".join(
            " ".join(
                self.own_text + [child.all_text() for child in self.children]
            ).split()
        )

    def descendants(self) -> list[_Node]:
        return [
            node for child in self.children for node in (child, *child.descendants())
        ]


class _HTMLTree(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("document")
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag, {key: value or "" for key, value in attrs})
        self.stack[-1].children.append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.stack[-1].children.append(
            _Node(tag, {key: value or "" for key, value in attrs}, closed=True)
        )

    def handle_endtag(self, tag: str) -> None:
        if len(self.stack) > 1 and self.stack[-1].tag == tag:
            self.stack.pop().closed = True

    def handle_data(self, data: str) -> None:
        self.stack[-1].own_text.append(data)


def _single(nodes: list[_Node]) -> _Node:
    if len(nodes) != 1 or not nodes[0].closed:
        raise InvalidUpstreamResponse("BRS HTML structure is unrecognized")
    return nodes[0]


def _selected(select: _Node) -> str:
    option = _single(
        [n for n in select.descendants() if n.tag == "option" and "selected" in n.attrs]
    )
    result = option.all_text()
    if not result:
        raise InvalidUpstreamResponse("BRS HTML period is missing")
    return result


class IStudentBRSHTMLParser:
    """Map observed numeric final-score cells; unknown point meanings fail closed."""

    def parse(self, payload: str | bytes, *, as_of: date) -> BRSResult:
        # The source still owns selection; only reject contradictions we can prove.
        if isinstance(payload, bytes):
            if len(payload) > MAX_HTML_BYTES:
                raise InvalidUpstreamResponse("BRS HTML exceeds the size limit")
            try:
                html = payload.decode("utf-8")
            except UnicodeDecodeError:
                raise InvalidUpstreamResponse(
                    "BRS HTML encoding is unsupported"
                ) from None
        elif isinstance(payload, str):
            if len(payload.encode("utf-8")) > MAX_HTML_BYTES:
                raise InvalidUpstreamResponse("BRS HTML exceeds the size limit")
            html = payload
        else:
            raise InvalidUpstreamResponse("BRS HTML type is unsupported")
        tree = _HTMLTree()
        tree.feed(html)
        tree.close()
        article = _single(
            [node for node in tree.root.descendants() if node.tag == "article"]
        )
        selection = _single(
            [n for n in article.descendants() if "selection-block" in n.classes()]
        )
        selects = [n for n in selection.descendants() if n.tag == "select"]
        for selector_id in ("group-select", "year-select", "semester-select"):
            _single([n for n in selects if n.attrs.get("id") == selector_id])
        year = _selected(
            _single([n for n in selects if n.attrs.get("id") == "year-select"])
        )
        semester = _selected(
            _single([n for n in selects if n.attrs.get("id") == "semester-select"])
        )
        if not _YEAR.fullmatch(year) or semester not in {"Осенний", "Весенний"}:
            raise InvalidUpstreamResponse("BRS HTML period is unrecognized")
        first_year, last_year = (int(part) for part in year.split("/"))
        if last_year != first_year + 1:
            raise InvalidUpstreamResponse("BRS HTML academic year is invalid")
        # September is demonstrably autumn in the current academic year. This
        # guard rejects a stale default, but does not choose periods for other dates.
        if as_of.month == 9 and (first_year != as_of.year or semester != "Осенний"):
            raise InvalidUpstreamResponse("BRS HTML selected period contradicts the request date")
        header = _single(
            [
                n
                for n in article.descendants()
                if "disciplines-list-header" in n.classes()
            ]
        )
        if [n.all_text() for n in header.children] != [
            "Дисциплина",
            "Итоговый балл",
            "Итоговая оценка",
        ]:
            raise InvalidUpstreamResponse("BRS HTML columns are unrecognized")
        descendants = article.descendants()
        if any(
            "pagination" in n.classes() or n.attrs.get("rel") == "next"
            for n in descendants
        ):
            raise InvalidUpstreamResponse("BRS HTML pagination is unrecognized")
        outers = [n for n in descendants if "discipline-outer-container" in n.classes()]
        if not outers:
            raise InvalidUpstreamResponse("BRS HTML has no verified rows")
        covered = sum(
            sum("discipline" in n.classes() for n in outer.descendants())
            for outer in outers
        )
        total = sum("discipline" in n.classes() for n in descendants)
        if covered != total:
            raise InvalidUpstreamResponse(
                "BRS HTML contains rows outside the verified list"
            )
        rows: list[BRSSubject] = []
        seen: set[str] = set()
        for outer in outers:
            if not outer.closed:
                raise InvalidUpstreamResponse("BRS HTML row is truncated")
            discipline = _single(
                [n for n in outer.children if "discipline" in n.classes()]
            )
            anchor = _single(
                [
                    n
                    for n in discipline.children
                    if n.tag == "a" and "discipline-header" in n.classes()
                ]
            )
            cells = [
                _single([n for n in anchor.children if f"td-{index}" in n.classes()])
                for index in range(3)
            ]
            name = cells[0].text()  # Ignore nested mobile mark and lazy-loaded details.
            score_text = cells[1].text()
            if not name or not _SCORE.fullmatch(score_text) or cells[1].children:
                raise InvalidUpstreamResponse("BRS HTML score meaning is unsupported")
            key = " ".join(name.split()).casefold()
            if key in seen:
                raise InvalidUpstreamResponse(
                    "BRS HTML contains duplicate subject names"
                )
            seen.add(key)
            rows.append(
                BRSSubject(
                    subject_name=name,
                    earned_points=Decimal(score_text.replace(",", ".")),
                    maximum_points=None,
                    points_status=BRSPointsStatus.AVAILABLE,
                    updated_at=None,
                )
            )
        return BRSResult(
            period=BRSPeriod(
                label=f"{year} — {semester}", starts_on=None, ends_on=None
            ),
            subjects=tuple(rows),
            completeness=BRSCompleteness.COMPLETE,
        )
