from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests
from bs4 import BeautifulSoup


RESULT_URL = "https://collegeadmissions.gndu.ac.in/studentArea/GNDUEXAMRESULT.aspx"


@dataclass(frozen=True)
class CheckResult:
    available: bool
    reason: str
    semester_text: str | None
    checked_at_utc: str
    source_url: str = RESULT_URL


class GNDUResultChecker:
    """Checks GNDU's public result form without submitting a student's roll number."""

    def __init__(
        self,
        *,
        year: str = "2026",
        month: str = "5",
        course_type: str = "C-",
        course_code: str = "1211",
        semester_pattern: str = r"\bsemester\s+iv\b",
        timeout: int = 30,
    ) -> None:
        self.year = year
        self.month = month
        self.course_type = course_type
        self.course_code = course_code
        self.semester_regex = re.compile(semester_pattern, re.IGNORECASE)
        self.timeout = timeout

    @staticmethod
    def _form_payload(soup: BeautifulSoup) -> dict[str, str]:
        """Collect the fields that a normal browser submits from this ASP.NET form."""
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
        if not element:
            return []
        return [
            (option.get_text(" ", strip=True), option.get("value", ""))
            for option in element.select("option")
        ]

    @staticmethod
    def _checked_at() -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    def check(self) -> CheckResult:
        checked_at = self._checked_at()

        try:
            with requests.Session() as session:
                session.headers.update(
                    {
                        "User-Agent": "GNDU-Result-Monitor/1.0 (+personal Telegram notifier)",
                        "Accept": "text/html,application/xhtml+xml",
                    }
                )

                response = session.get(RESULT_URL, timeout=self.timeout)
                response.raise_for_status()
                html = response.text

                # The page is ASP.NET Web Forms: each dropdown change is a postback.
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

                courses = self._options(html, "DrpDwnCMaster")
                course = next((item for item in courses if item[1] == self.course_code), None)
                if course is None:
                    return CheckResult(
                        False,
                        f"Target course code {self.course_code} is not listed yet for {self.year} May.",
                        None,
                        checked_at,
                    )

                html = self._postback(
                    session,
                    html,
                    "DrpDwnCMaster",
                    {
                        "DrpDwnYear": self.year,
                        "DrpDwnMonth": self.month,
                        "DropDownCourseType": self.course_type,
                        "DrpDwnCMaster": self.course_code,
                    },
                )

                semesters = self._options(html, "DrpDwnCdetail")
                target = next(
                    (item for item in semesters if self.semester_regex.search(item[0])),
                    None,
                )
                if target is None:
                    listed = ", ".join(text for text, _ in semesters if text.strip()) or "none"
                    return CheckResult(
                        False,
                        f"Semester IV is not listed yet. Current semester options: {listed}.",
                        None,
                        checked_at,
                    )

                return CheckResult(
                    True,
                    "GNDU now lists the target semester in the result form.",
                    target[0],
                    checked_at,
                )

        except requests.RequestException as exc:
            return CheckResult(False, f"GNDU request failed: {exc.__class__.__name__}: {exc}", None, checked_at)
        except Exception as exc:  # Keep the monitoring loop alive on unexpected HTML changes.
            return CheckResult(False, f"Checker error: {exc.__class__.__name__}: {exc}", None, checked_at)

    def as_dict(self, result: CheckResult) -> dict[str, Any]:
        return {
            "available": result.available,
            "reason": result.reason,
            "semester_text": result.semester_text,
            "checked_at_utc": result.checked_at_utc,
            "source_url": result.source_url,
        }


if __name__ == "__main__":
    result = GNDUResultChecker().check()
    print(f"available={result.available}")
    print(result.reason)
    if result.semester_text:
        print(f"semester={result.semester_text}")
    print(f"checked_at_utc={result.checked_at_utc}")
