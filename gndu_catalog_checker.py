from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests
from bs4 import BeautifulSoup


RESULT_URL = "https://collegeadmissions.gndu.ac.in/studentArea/GNDUEXAMRESULT.aspx"


@dataclass(frozen=True)
class CatalogCheckResult:
    ok: bool
    reason: str
    checked_at_utc: str
    snapshot: dict[str, Any] | None = None
    new_courses: list[dict[str, Any]] | None = None
    new_semesters: list[dict[str, Any]] | None = None
    baseline_created: bool = False


class GNDUCatalogChecker:
    """Read all course and semester options for 2026 May CBGS New."""

    def __init__(
        self,
        *,
        year: str = "2026",
        month: str = "5",
        course_type: str = "C-",
        timeout: int = 30,
        request_delay_seconds: float = 0.2,
    ) -> None:
        self.year = year
        self.month = month
        self.course_type = course_type
        self.timeout = timeout
        self.request_delay_seconds = max(request_delay_seconds, 0.0)

    @staticmethod
    def _checked_at() -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    @staticmethod
    def _form_payload(soup: BeautifulSoup) -> dict[str, str]:
        payload: dict[str, str] = {}

        for element in soup.select("input[name]"):
            name = element.get("name")
            input_type = (element.get("type") or "text").lower()
            if not name or input_type in {"submit", "button", "image", "file", "reset"}:
                continue
            if element.has_attr("disabled"):
                continue
            payload[name] = element.get("value", "")

        for select in soup.select("select[name]"):
            selected = select.select_one("option[selected]") or select.select_one("option")
            payload[select["name"]] = selected.get("value", "") if selected else ""

        return payload

    def _postback(
        self,
        session: requests.Session,
        html: str,
        event_target: str,
        changes: dict[str, str],
    ) -> str:
        soup = BeautifulSoup(html, "html.parser")
        payload = self._form_payload(soup)
        payload.update(changes)
        payload["__EVENTTARGET"] = event_target
        payload["__EVENTARGUMENT"] = ""

        response = session.post(
            RESULT_URL,
            data=payload,
            headers={"Referer": RESULT_URL},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.text

    @staticmethod
    def _options(html: str, element_id: str) -> list[tuple[str, str]]:
        soup = BeautifulSoup(html, "html.parser")
        element = soup.find(id=element_id)
        if element is None:
            raise RuntimeError(f"GNDU page did not contain the expected element: {element_id}")
        return [
            (option.get_text(" ", strip=True), option.get("value", ""))
            for option in element.select("option")
            if option.get("value", "").strip() and option.get_text(" ", strip=True)
        ]

    @staticmethod
    def _normalise_text(value: str) -> str:
        return re.sub(r"\s+", " ", value).strip().casefold()

    @classmethod
    def _semester_key(cls, semester: dict[str, str]) -> str:
        value = semester.get("value", "").strip()
        return f"value:{value}" if value else f"text:{cls._normalise_text(semester.get('text', ''))}"

    def fetch_snapshot(self) -> dict[str, Any]:
        with requests.Session() as session:
            session.headers.update(
                {
                    "User-Agent": "GNDU-Catalog-Monitor/1.0 (+personal Telegram notifier)",
                    "Accept": "text/html,application/xhtml+xml",
                }
            )

            response = session.get(RESULT_URL, timeout=self.timeout)
            response.raise_for_status()
            html = response.text

            # GNDU is an ASP.NET Web Forms page; these dropdowns are server postbacks.
            html = self._postback(
                session,
                html,
                "DrpDwnMonth",
                {"DrpDwnYear": self.year, "DrpDwnMonth": self.month},
            )
            html = self._postback(
                session,
                html,
                "DropDownCourseType",
                {
                    "DrpDwnYear": self.year,
                    "DrpDwnMonth": self.month,
                    "DropDownCourseType": self.course_type,
                },
            )

            course_options = self._options(html, "DrpDwnCMaster")
            if not course_options:
                raise RuntimeError("GNDU returned no course/class options for 2026 May CBGS New")

            # Keep the course-list response as the base for every course request so that
            # each ASP.NET postback starts from a consistent view state.
            course_list_html = html
            courses: dict[str, Any] = {}
            for course_text, course_code in course_options:
                course_html = self._postback(
                    session,
                    course_list_html,
                    "DrpDwnCMaster",
                    {
                        "DrpDwnYear": self.year,
                        "DrpDwnMonth": self.month,
                        "DropDownCourseType": self.course_type,
                        "DrpDwnCMaster": course_code,
                    },
                )
                semester_options = self._options(course_html, "DrpDwnCdetail")
                semesters = [
                    {"text": semester_text, "value": semester_value}
                    for semester_text, semester_value in semester_options
                ]
                courses[course_code] = {
                    "text": course_text,
                    "semesters": semesters,
                }
                if self.request_delay_seconds:
                    time.sleep(self.request_delay_seconds)

            snapshot = {
                "year": self.year,
                "month": self.month,
                "course_type": self.course_type,
                "courses": courses,
            }
            self._validate_snapshot(snapshot)
            return snapshot

    @staticmethod
    def _validate_snapshot(snapshot: dict[str, Any]) -> None:
        courses = snapshot.get("courses")
        if not isinstance(courses, dict) or not courses:
            raise RuntimeError("GNDU returned an invalid or empty course snapshot")
        for course_code, course in courses.items():
            if not isinstance(course_code, str) or not isinstance(course, dict):
                raise RuntimeError("GNDU returned an invalid course entry")
            if not isinstance(course.get("text"), str) or not isinstance(course.get("semesters"), list):
                raise RuntimeError(f"GNDU returned an invalid entry for course {course_code}")
            for semester in course["semesters"]:
                if not isinstance(semester, dict) or not isinstance(semester.get("text"), str) or not isinstance(semester.get("value"), str):
                    raise RuntimeError(f"GNDU returned an invalid semester entry for course {course_code}")

    @classmethod
    def diff_snapshots(
        cls,
        previous: dict[str, Any],
        current: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        previous_courses = previous.get("courses", {})
        current_courses = current.get("courses", {})

        new_courses: list[dict[str, Any]] = []
        for course_code, course in current_courses.items():
            if course_code not in previous_courses:
                new_courses.append(
                    {
                        "code": course_code,
                        "text": course.get("text", ""),
                        "semesters": course.get("semesters", []),
                    }
                )

        # Only report new semesters for courses that were already present in the
        # previous snapshot. A new course is reported in the new-course section.
        new_semesters: list[dict[str, Any]] = []
        for course_code, current_course in current_courses.items():
            if course_code not in previous_courses:
                continue
            old_course = previous_courses.get(course_code, {})
            old_keys = {
                cls._semester_key(semester)
                for semester in old_course.get("semesters", [])
            }
            for semester in current_course.get("semesters", []):
                if cls._semester_key(semester) not in old_keys:
                    new_semesters.append(
                        {
                            "course_code": course_code,
                            "course_text": current_course.get("text", ""),
                            "semester_text": semester.get("text", ""),
                            "semester_value": semester.get("value", ""),
                        }
                    )

        return new_courses, new_semesters

    def check(self, previous_snapshot: dict[str, Any] | None) -> CatalogCheckResult:
        checked_at = self._checked_at()
        try:
            current_snapshot = self.fetch_snapshot()
            self._validate_snapshot(current_snapshot)
            if previous_snapshot is not None:
                self._validate_snapshot(previous_snapshot)
            if previous_snapshot is None:
                return CatalogCheckResult(
                    ok=True,
                    reason="Initial catalog snapshot saved; existing entries will not be treated as new.",
                    checked_at_utc=checked_at,
                    snapshot=current_snapshot,
                    new_courses=[],
                    new_semesters=[],
                    baseline_created=True,
                )

            new_courses, new_semesters = self.diff_snapshots(previous_snapshot, current_snapshot)
            total_courses = len(current_snapshot.get("courses", {}))
            if new_courses or new_semesters:
                reason = (
                    f"Detected {len(new_courses)} new course/class option(s) and "
                    f"{len(new_semesters)} new semester option(s) across {total_courses} courses."
                )
            else:
                reason = f"No new course or semester option detected across {total_courses} courses."

            return CatalogCheckResult(
                ok=True,
                reason=reason,
                checked_at_utc=checked_at,
                snapshot=current_snapshot,
                new_courses=new_courses,
                new_semesters=new_semesters,
            )
        except requests.RequestException as exc:
            return CatalogCheckResult(
                ok=False,
                reason=f"GNDU request failed: {exc.__class__.__name__}: {exc}",
                checked_at_utc=checked_at,
            )
        except Exception as exc:  # Keep the Telegram process alive after HTML changes.
            return CatalogCheckResult(
                ok=False,
                reason=f"Catalog checker error: {exc.__class__.__name__}: {exc}",
                checked_at_utc=checked_at,
            )


if __name__ == "__main__":
    result = GNDUCatalogChecker().check(None)
    print(result.reason)
    if result.snapshot:
        print(f"courses={len(result.snapshot.get('courses', {}))}")
