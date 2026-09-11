"""Prove every demo run lands in one findable place, not scattered loose.

Before this existed, a live run wrote one ad-hoc screenshot straight into
results/ and printed the rest to a terminal nobody kept. Forty-plus such
files accumulated with no way to tell which run produced which, or what any
of them actually showed - a real usability failure, not a cosmetic one.

These checks pin down the structural fix: every run gets its own folder,
with a report that actually contains what happened, and the master index
always reflects exactly what is on disk - no separate bookkeeping to drift
out of sync with reality.

    python tools/report_tests.py
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SENTINEL_ALLOW_UNVERIFIED", "1")

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from sentinel.agent import AgentRun, Observation, Step  # noqa: E402
from sentinel.demo_report import update_index, write_report  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = ROOT / "results" / "_report_test_scratch"
_failures: list[str] = []
_checks = 0


def check(name: str, got, want) -> None:
    global _checks
    _checks += 1
    if got == want:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}: got {got!r}, want {want!r}")
        _failures.append(name)


def check_true(name: str, condition: bool) -> None:
    check(name, bool(condition), True)


def section(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


@dataclass
class FakeCase:
    case_id: str
    title: str
    persona: str
    steps: str
    expected: str


@dataclass
class FakeBrain:
    provider: str = "groq"
    model: str = "openai/gpt-oss-120b"
    usage: object = None


def fake_adb_ok(cmd, capture_output=True, timeout=15):
    class R:
        stdout = b"\x89PNG\r\n\x1a\nfake-screenshot-bytes"
    return R()


if SCRATCH.exists():
    shutil.rmtree(SCRATCH)
SCRATCH.mkdir(parents=True)

# Patch subprocess.run inside demo_report to a real, controlled fake -
# nothing here needs an actual device.
import sentinel.demo_report as demo_report_module  # noqa: E402
demo_report_module.subprocess.run = fake_adb_ok


# --------------------------------------------------------------------------- #
section("A successful run lands in its own folder, not loose in results/")

case = FakeCase(case_id="TC-006", title="View site list", persona="Owner",
                steps="Open the Sites list.",
                expected="Both sites visible with correct names/details.")
run = AgentRun(
    case_id="TC-006",
    steps=[Step(number=1, action_name="confirm_screen", description="confirm on Sites",
               result="confirmed on Sites")],
    observations={"site1": Observation(key="site1", value="Aqua Line", screen_confirmed=True)},
    confirmations=[("Sites List", "real evidence cited")],
    finished=True, reached_target_screen=True, summary="Both sites visible.",
)

before = len(list((SCRATCH / "demos").iterdir())) if (SCRATCH / "demos").exists() else 0
folder = write_report(SCRATCH, "adb", case, run, FakeBrain(), "1. BUTTON 'Sites'",
                       start_screenshot=b"\x89PNG-start-bytes")

check_true("a dedicated folder was created", folder.is_dir())
check_true("it lives under results/demos, not loose in results/",
           folder.parent.name == "demos" and folder.parent.parent == SCRATCH)
check_true("the folder name identifies the case", "TC-006" in folder.name)
check_true("a report.html exists", (folder / "report.html").exists())
check_true("the starting screenshot was saved", (folder / "screen_start.png").exists())
check_true("the final screenshot was saved", (folder / "screen_final.png").exists())
check_true("a plain-text trace was saved", (folder / "trace.txt").exists())

report_text = (folder / "report.html").read_text(encoding="utf-8")
check_true("the report names the real case", "TC-006" in report_text)
check_true("the report states the expected result",
           "Both sites visible with correct names/details" in report_text)
check_true("the report shows what was actually recorded", "Aqua Line" in report_text)
check_true("the report cites the screen confirmation reasoning",
           "real evidence cited" in report_text)
check_true("a completed, confirmed run is marked clearly ok", 'class="badge ok"' in report_text)


# --------------------------------------------------------------------------- #
section("Values are escaped, not exposed to injection")

hostile_case = FakeCase(
    case_id="TC-666", title="<script>alert(1)</script>", persona="Owner",
    steps="do stuff", expected="a value containing <b>markup</b> & \"quotes\"",
)
hostile_run = AgentRun(case_id="TC-666", steps=[], finished=True,
                       summary="<img src=x onerror=alert(2)>")
folder2 = write_report(SCRATCH, "adb", hostile_case, hostile_run, FakeBrain(), "screen")
report2 = (folder2 / "report.html").read_text(encoding="utf-8")
check_true("a script tag in the case title is escaped, not executable",
           "<script>alert(1)</script>" not in report2)
check_true("an img-onerror payload's tag delimiters are escaped, so it cannot "
           "render as a real element",
           "<img src=x onerror=alert(2)>" not in report2)
check_true("the escaped content is still legible in the markup",
           "&lt;script&gt;" in report2)


# --------------------------------------------------------------------------- #
section("A run with no starting screenshot still produces a valid report")

no_start_run = AgentRun(case_id="TC-009", gave_up=True, give_up_reason="stuck at login")
folder3 = write_report(SCRATCH, "adb", FakeCase("TC-009", "x", "Site Engineer", "s", "e"),
                       no_start_run, FakeBrain(), "screen", start_screenshot=None)
check_true("no screen_start.png is written when there is none",
           not (folder3 / "screen_start.png").exists())
check_true("the final screenshot is still captured", (folder3 / "screen_final.png").exists())
report3 = (folder3 / "report.html").read_text(encoding="utf-8")
check_true("an honest give-up is labelled as such, not hidden", "GAVE UP" in report3)
check_true("the real reason is in the report", "stuck at login" in report3)


# --------------------------------------------------------------------------- #
section("The master index always reflects what is actually on disk")

index_path = SCRATCH / "demos" / "index.html"
check_true("the index exists after writing reports", index_path.exists())
index_text = index_path.read_text(encoding="utf-8")
check_true("every run written so far is listed", "TC-006" in index_text
           and "TC-666" in index_text and "TC-009" in index_text)
check_true("each entry links to its own report",
           f"{folder.name}/report.html" in index_text)

# Delete one run's folder entirely, then rebuild - the index must not still
# claim it exists. This is the property that matters: no separate
# bookkeeping that can drift from reality.
shutil.rmtree(folder)
update_index(SCRATCH)
index_after_delete = index_path.read_text(encoding="utf-8")
check_true("removing a run's folder removes it from the index on rebuild",
           f"{folder.name}/report.html" not in index_after_delete)
check_true("the other real runs are still listed", "TC-666" in index_after_delete)


# --------------------------------------------------------------------------- #
section("An empty results directory produces a valid, honest index")

empty_root = SCRATCH / "_empty"
empty_root.mkdir(exist_ok=True)
empty_index = update_index(empty_root)
check_true("an index is still written with zero runs", empty_index.exists())
check_true("it says so honestly rather than showing a blank table",
           "no demo runs yet" in empty_index.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
shutil.rmtree(SCRATCH, ignore_errors=True)

print(f"\n{'=' * 60}")
if _failures:
    print(f"FAILED {len(_failures)}/{_checks}:")
    for name in _failures:
        print(f"  - {name}")
    sys.exit(1)
print(f"All {_checks} checks passed.")
print("Every demo run is one folder, one report, one index - never scattered loose.")
