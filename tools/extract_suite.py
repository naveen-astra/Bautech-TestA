"""Regenerate tests/bautech_suite.csv from the human test-case sheet.

Provenance tool. The 85 cases live in the QA team's Word document as twelve
section tables sharing one header:

    ID | Test case | Who | Steps to replicate | Expected result | P/F | Remarks | DEV

We keep only the five columns a human tester actually authors. The P/F and
Remarks columns are a record of a *past manual run* and are deliberately
dropped: letting them reach the agent would leak the answers.

Usage:
    python tools/extract_suite.py "path/to/Testing Day-Wise_word.docx"
"""

from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

import docx

# Columns the agent is allowed to see. Order matters - it is the CSV header.
COLUMNS = ["ID", "Test case", "Who", "Steps to replicate", "Expected result"]

TC_ID = re.compile(r"^TC\s*-?\s*(\d{1,3})$")


def _clean(cell: str) -> str:
    """Collapse the whitespace Word scatters through table cells."""
    return re.sub(r"\s+", " ", cell.replace("\n", " ")).strip()


def _normalise_id(raw: str) -> str | None:
    """'TC- 001' / 'TC-1' -> 'TC-001'. Returns None for non-case rows."""
    match = TC_ID.match(_clean(raw).replace(" ", ""))
    return f"TC-{int(match.group(1)):03d}" if match else None


def extract(doc_path: Path) -> list[dict[str, str]]:
    document = docx.Document(str(doc_path))
    rows: list[dict[str, str]] = []
    seen: set[str] = set()

    for table in document.tables:
        header = [_clean(c.text) for c in table.rows[0].cells]
        # The suite tables are the ones carrying the tester's own header.
        if header[:5] != COLUMNS:
            continue
        for row in table.rows[1:]:
            cells = [_clean(c.text) for c in row.cells]
            case_id = _normalise_id(cells[0])
            if case_id is None or case_id in seen:
                continue
            seen.add(case_id)
            rows.append(dict(zip(COLUMNS, [case_id, *cells[1:5]])))

    return sorted(rows, key=lambda r: r["ID"])


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2

    source = Path(sys.argv[1])
    if not source.exists():
        print(f"error: no such file: {source}")
        return 1

    rows = extract(source)
    if not rows:
        print("error: no suite tables found - has the sheet's header changed?")
        return 1

    out = Path(__file__).resolve().parent.parent / "tests" / "bautech_suite.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    print(f"wrote {len(rows)} cases -> {out}")

    expected = {f"TC-{n:03d}" for n in range(1, 86)}
    missing = sorted(expected - {r["ID"] for r in rows})
    if missing:
        print(f"warning: missing {len(missing)} case(s): {', '.join(missing)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
