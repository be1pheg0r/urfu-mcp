"""Immutable normalized eLearn models and stable public projections."""

from __future__ import annotations

from datetime import date
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ELearnModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CourseSummary(ELearnModel):
    course_id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=300)
    short_name: str | None = None
    progress_group: str | None = None


class CourseActivity(ELearnModel):
    name: str = Field(min_length=1, max_length=300)
    modtype: str | None = Field(default=None, max_length=60)
    url: str | None = Field(default=None, max_length=2000)
    restricted: bool = False

    @field_validator("url")
    @classmethod
    def url_must_be_http(cls, value: str | None) -> str | None:
        if value is not None and urlsplit(value).scheme.lower() not in {"http", "https"}:
            raise ValueError("url must use http or https")
        return value


class CourseSection(ELearnModel):
    name: str = Field(max_length=300)
    section_id: str | None = None
    activities: tuple[CourseActivity, ...]


class CourseContent(ELearnModel):
    course_id: int
    name: str | None = None
    sections: tuple[CourseSection, ...]


def public_courses_payload(courses: tuple[CourseSummary, ...], *, as_of: date) -> dict[str, object]:
    return {
        "as_of": as_of.isoformat(),
        "timezone": "Asia/Yekaterinburg",
        "course_count": len(courses),
        "courses": [course.model_dump(mode="json") for course in courses],
        "source": "elearn_moodle",
    }


def public_course_content_payload(content: CourseContent, *, selector: str, as_of: date) -> dict[str, object]:
    sections = [section.model_dump(mode="json") for section in content.sections]
    return {
        "as_of": as_of.isoformat(),
        "timezone": "Asia/Yekaterinburg",
        "course_id": content.course_id,
        "name": content.name,
        "selector": selector,
        "sections": sections,
        "section_count": len(content.sections),
        "activity_count": sum(len(section.activities) for section in content.sections),
        "source": "elearn_moodle",
    }


__all__ = ["CourseActivity", "CourseContent", "CourseSection", "CourseSummary", "public_course_content_payload", "public_courses_payload"]
