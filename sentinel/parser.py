"""Read the human test-case sheet into RawTestCase objects.

The sheet is edited in Excel by people who are not running this code, so the
parser is deliberately forgiving about surface detail and strict about the two
things that actually matter: every row needs an ID, and every row needs an
Expected result. A row without an Expected column states no claim, so there is
nothing to judge and we refuse it loudly rather than inventing an intent.

Column headers are matched loosely ("Expected result", "expected", "Expected
Behaviour" all work) so that renaming a heading in Excel does not break a run.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

from sentinel.schema import RawTestCase

# Each field maps to the header fragments we will accept for it, in priority
# order. Matching is case-insensitive and ignores punctuation and spacing.
_HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "case_id": ("id", "testcaseid", "tcid", "case"),
    "title": ("testcase", "title", "name", "scenario"),
    "persona_hint": ("who", "persona", "role", "user"),
    "steps_text": ("stepstoreplicate", "steps", "stepstoreproduce", "procedure"),
    "expected_text": ("expectedresult", "expected", "expectedbehaviour", "expectedbehavior"),
}

# Columns that record a previous manual run. If someone exports a sheet that
# still has them we drop them: they are prior answers, and letting them reach
# the compiler would leak the verdicts we are supposed to be deriving.
_VERDICT_LEAK_COLUMNS = ("pf", "p/f", "passfail", "result", "remarks", "status", "dev")

# Some titles in the human sheet carry an editorial hint - "[MUST BE BLOCKED]".
# We strip it. Whether a case is a prohibition has to come from reading the
# Expected column, for two reasons: only 14 of the 19 prohibitions in this suite
# are tagged, so a tag-reading agent silently misses five of them; and a system
# that is handed the answer is not deriving it. Stripping the hint is also what
# makes the claim testable - the polarity we produce is provably a reading of
# the expectation rather than an echo of the label.
_EDITORIAL_HINT = re.compile(
    r"\[\s*(?:must\s+be\s+)?(?:blocked|denied|not\s+allowed|negative)\s*\]",
    re.IGNORECASE,
)

_TC_ID = re.compile(r"^TC\s*-?\s*(\d{1,3})$", re.IGNORECASE)


class SheetError(Exception):
    """The sheet cannot be read as a test suite."""


def _norm(header: str) -> str:
    return re.sub(r"[^a-z0-9]", "", header.lower())


def _resolve_columns(fieldnames: list[str]) -> dict[str, str]:
    """Map our field names onto the sheet actual headers."""
    normalised = {_norm(name): name for name in fieldnames if name}
    resolved: dict[str, str] = {}

    for field, aliases in _HEADER_ALIASES.items():
        for alias in aliases:
            # Exact match first, then prefix match, so "Expected result (UI)"
            # still resolves but "ID" never swallows "Steps to replicate".
            if alias in normalised:
                resolved[field] = normalised[alias]
                break
            hit = next(
                (orig for norm, orig in normalised.items() if norm.startswith(alias)),
                None,
            )
            if hit is not None:
                resolved[field] = hit
                break

    for required in ("case_id", "expected_text"):
        if required not in resolved:
            raise SheetError(
                f"sheet is missing a required column for {required!r}; "
                f"saw headers: {', '.join(fieldnames)}"
            )
    return resolved


def normalise_case_id(raw: str) -> str | None:
    """'TC- 1' / 'tc-001' -> 'TC-001'. None if this is not a case row."""
    match = _TC_ID.match(raw.strip().replace(" ", ""))
    return f"TC-{int(match.group(1)):03d}" if match else None


def _clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").replace("\n", " ")).strip()


def load_suite(path: str | Path) -> list[RawTestCase]:
    """Parse the sheet. Raises SheetError on anything we cannot judge."""
    path = Path(path)
    if not path.exists():
        raise SheetError(f"no test sheet at {path}")

    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise SheetError(f"{path} is empty")
        columns = _resolve_columns(list(reader.fieldnames))
        rows = list(reader)

    dropped = [f for f in reader.fieldnames if _norm(f) in _VERDICT_LEAK_COLUMNS]

    cases: list[RawTestCase] = []
    seen: dict[str, int] = {}
    problems: list[str] = []
    hints_stripped: list[str] = []

    for line_no, row in enumerate(rows, start=2):
        case_id = normalise_case_id(_clean(row.get(columns["case_id"])))
        if case_id is None:
            continue  # section heading or spacer row

        expected = _clean(row.get(columns["expected_text"]))
        if not expected:
            problems.append(f"line {line_no} ({case_id}): no Expected result - nothing to judge")
            continue

        if case_id in seen:
            problems.append(
                f"line {line_no}: duplicate {case_id}, first seen on line {seen[case_id]}"
            )
            continue
        seen[case_id] = line_no

        raw_title = _clean(row.get(columns.get("title", ""), ""))
        title = _clean(_EDITORIAL_HINT.sub("", raw_title))
        if title != raw_title:
            hints_stripped.append(case_id)

        cases.append(
            RawTestCase(
                case_id=case_id,
                title=title or case_id,
                persona_hint=_clean(row.get(columns.get("persona_hint", ""), "")),
                steps_text=_clean(row.get(columns.get("steps_text", ""), "")),
                expected_text=expected,
            )
        )

    # Report specific problems before the generic "nothing here" complaint,
    # otherwise a sheet whose only fault is one bad row blames the whole file.
    if problems:
        raise SheetError("test sheet has problems:\n  - " + "\n  - ".join(problems))
    if not cases:
        raise SheetError(f"{path} contained no rows with a TC-nnn id")

    # Not fatal - just make it visible what we refused to look at.
    if dropped:
        print(f"[parser] ignoring prior-run columns: {', '.join(dropped)}")
    if hints_stripped:
        print(
            f"[parser] stripped editorial hints from {len(hints_stripped)} title(s); "
            "polarity is read from the Expected column"
        )

    return sorted(cases, key=lambda c: c.case_id)
