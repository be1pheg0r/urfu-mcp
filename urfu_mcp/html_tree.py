"""Shared tolerant HTML parser primitive."""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser

_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"})


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[_Node] = field(default_factory=list)
    own_text: list[str] = field(default_factory=list)
    closed: bool = False

    def classes(self) -> set[str]:
        return set(self.attrs.get("class", "").split())

    def all_text(self) -> str:
        return " ".join(" ".join(self.own_text + [child.all_text() for child in self.children]).split())

    def descendants(self) -> list[_Node]:
        return [node for child in self.children for node in (child, *child.descendants())]


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
        self.stack[-1].children.append(_Node(tag, {key: value or "" for key, value in attrs}, closed=True))

    def handle_endtag(self, tag: str) -> None:
        if len(self.stack) > 1 and self.stack[-1].tag == tag:
            self.stack.pop().closed = True

    def handle_data(self, data: str) -> None:
        self.stack[-1].own_text.append(data)


__all__ = ["_HTMLTree", "_Node"]
