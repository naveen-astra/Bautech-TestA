"""Read back which cases a Maestro run actually finished.

The renderer stamps every flow with a `testCaseId` property (see
`render_segment` in `renderer.py`), and Maestro carries that property through
into the JUnit report it writes. That property, not the JUnit `name` or
`classname` attributes, is the reliable key back to a case id: `name` and
`classname` are free text a report viewer displays, but `properties` are the
structured values we ourselves put there.

This is what makes retries possible without re-judging anything: a case whose
flow crashed before finishing produces no `<failure>` marker of its own
concern - it just never got there - and JUnit's own pass/fail is the cheapest,
most direct signal for "did the flow itself complete", entirely separate from
whether what it observed was a pass or a fail.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path


class JunitError(Exception):
    """The report could not be read as JUnit XML."""


def failed_case_ids(path: str | Path) -> set[str]:
    """Case ids whose flow did not finish cleanly, per the JUnit report.

    A testcase counts as failed if it carries a <failure> or <error> child,
    or an explicit non-success status attribute - different Maestro versions
    have used both conventions, so both are honoured rather than picking one
    and silently missing failures reported the other way.
    """
    path = Path(path)
    if not path.exists():
        return set()

    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise JunitError(f"{path} is not valid XML: {exc}") from exc

    failed: set[str] = set()
    for testcase in root.iter("testcase"):
        has_failure_marker = testcase.find("failure") is not None or testcase.find("error") is not None
        status = (testcase.get("status") or "").upper()
        explicit_failure_status = status not in ("", "SUCCESS", "PASSED")

        if not (has_failure_marker or explicit_failure_status):
            continue

        case_id = _case_id_of(testcase)
        if case_id:
            failed.add(case_id)

    return failed


def _case_id_of(testcase: ET.Element) -> str | None:
    """The `testCaseId` property the renderer stamped onto this flow."""
    for prop in testcase.iter("property"):
        if prop.get("name") == "testCaseId":
            return prop.get("value")
    return None
