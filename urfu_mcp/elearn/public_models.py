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
    modtype: str = Field(min_length=1, max_length=60)
    url: str | None = Field(default=None, max_length=2000)
    restricted: bool = False

    @field_validator("url")
    @classmethod
    def url_must_be_http(cls, value: str | None) -> str | None:
        if value is not None and urlsplit(value).scheme.lower() not in {"http", "https"}:
            raise ValueError("url must use http or https")
        return value


class CourseAssignment(CourseActivity):
    due_date: str | None = None


class CourseQuiz(CourseActivity):
    opens_at: str | None = None
    closes_at: str | None = None
    time_limit: str | None = None


class CourseMaterial(CourseActivity):
    file_size: str | None = None


class CourseFile(ELearnModel):
    display_name: str = Field(min_length=1, max_length=500)
    file_name: str = Field(min_length=1, max_length=500)
    mime_type: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    modified_date: str | None = None
    download_url: str = Field(min_length=1, max_length=4000)
    section_name: str = Field(max_length=300)


class CourseForum(CourseActivity):
    discussion_count: int | None = Field(default=None, ge=0)


class CourseSection(ELearnModel):
    name: str = Field(max_length=300)
    section_id: str
    assignments: tuple[CourseAssignment, ...] = ()
    quizzes: tuple[CourseQuiz, ...] = ()
    materials: tuple[CourseMaterial, ...] = ()
    forums: tuple[CourseForum, ...] = ()
    other: tuple[CourseActivity, ...] = ()

    @property
    def activities(self) -> tuple[CourseActivity, ...]:
        return (*self.assignments, *self.quizzes, *self.materials, *self.forums, *self.other)


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


def public_course_files_payload(files: tuple[CourseFile, ...], *, selector: str) -> dict[str, object]:
    return {
        "selector": selector,
        "file_count": len(files),
        "files": [item.model_dump(mode="json", exclude_none=True) for item in files],
        "source": "elearn_moodle",
    }


__all__ = ["CourseActivity", "CourseAssignment", "CourseContent", "CourseFile", "CourseForum", "CourseMaterial", "CourseQuiz", "CourseSection", "CourseSummary", "public_course_content_payload", "public_course_files_payload", "public_courses_payload"]
