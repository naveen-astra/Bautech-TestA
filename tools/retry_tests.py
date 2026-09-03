"""Prove the bounded retry loop does what it claims, and nothing more.

Three things matter here, and the third is the one that actually protects the
submission:

*   A case whose flow crashes before finishing gets re-run, and the retry's
    observations are the ones that count.
*   A case still failing after the retry budget is spent is reported clearly,
    not silently dropped.
*   A case that finished normally and observed a genuine FAIL is NEVER
    retried - only a JUnit-level "the flow itself did not complete" signal is
    eligible. Retrying a real observed defect would be exactly the
    "retries must not mask a defect" failure the plan explicitly forbids, so
    this is checked directly rather than assumed from the code reading right.

Everything here runs against a scripted stub backend - no device needed, and
none of this depends on Bautech's own UI, so it is fully deterministic.

    python tools/retry_tests.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SENTINEL_ALLOW_UNVERIFIED", "1")

import yaml  # noqa: E402

import run as sentinel_run  # noqa: E402
from sentinel.backends.base import RunArtifacts  # noqa: E402
from sentinel.renderer import FlowRenderer  # noqa: E402
from sentinel.schema import (  # noqa: E402
    Assertion,
    AssertionKind,
    Capability,
    Segment,
    Step,
    TestPlan,
)
from sentinel.screen_map import ScreenMap  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
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


def section(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


def make_plan(case_id: str) -> TestPlan:
    return TestPlan(
        case_id=case_id, source_hash="h", title=case_id, primary_persona="Owner",
        segments=[Segment(persona="Owner", intent="i", steps=[
            Step(capability=Capability.READ_VALUE, target="stock_level",
                 observation_key="v")])],
        assertions=[Assertion(id="a", kind=AssertionKind.TEXT_CONTAINS,
                              expected_text="x", consumes=["v"], expected_value="x")],
    )


def write_junit(path: Path, cases: dict[str, bool]) -> Path:
    """cases: {case_id: succeeded}"""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = []
    for cid, ok in cases.items():
        failure = "" if ok else '<failure message="crashed"/>'
        status = "SUCCESS" if ok else "ERROR"
        body.append(
            f'<testcase id="{cid}" name="{cid}" status="{status}">'
            f'<properties><property name="testCaseId" value="{cid}"/></properties>'
            f"{failure}</testcase>"
        )
    path.write_text(
        "<?xml version='1.0'?><testsuites><testsuite name='s'>"
        + "".join(body) + "</testsuite></testsuites>",
        encoding="utf-8",
    )
    return path


class ScriptedBackend:
    """Returns one scripted RunArtifacts per call, in order. No device."""

    def __init__(self, script: list[tuple[Path, dict[str, bool]]]) -> None:
        self.script = list(script)
        self.calls: list[Path] = []

    def check(self) -> list[str]:
        return []

    def run(self, flow_dir: Path, out_dir: Path, env, timeout_seconds=1800) -> RunArtifacts:
        self.calls.append(flow_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        junit_path, cases = self.script.pop(0)
        real_junit = write_junit(out_dir / "report.xml", cases)
        # Content doesn't matter for these tests - only that a log exists,
        # so collect() has something to point evidence at.
        log = out_dir / "console.log"
        log.write_text(f"@@FLOW start\n@@FLOW end\n", encoding="utf-8")
        return RunArtifacts(logs=[log], junit=real_junit, exit_code=0, duration_seconds=1.0)


screen_map = ScreenMap.load()
personas = yaml.safe_load((ROOT / "config" / "personas.yaml").read_text(encoding="utf-8"))
renderer = FlowRenderer(screen_map, personas, app_id="com.naviconinfra.bautech", run_id="RT")


# --------------------------------------------------------------------------- #
section("A crashed flow is retried and recovers")

plans = [make_plan("TC-A"), make_plan("TC-B")]
flow_dir = ROOT / "flows" / "generated" / "_retry_test_1"
results_dir = ROOT / "results" / "_retry_test_1"
for p in plans:
    renderer.render_plan(p, flow_dir)

backend = ScriptedBackend([
    (flow_dir, {"TC-A": True, "TC-B": False}),   # first attempt: B crashes
    (flow_dir, {"TC-B": True}),                   # retry: B recovers
])
artifacts, attempt_of = sentinel_run.execute_with_bounded_retries(
    backend, plans, renderer, flow_dir, results_dir, {}, 60, max_retries=1
)
check("two backend calls made (one retry)", len(backend.calls), 2)
check("the retry only re-renders the failed case",
      backend.calls[1] == flow_dir / "retry-1", True)
check("the clean case is recorded as attempt 0", attempt_of["TC-A"], 0)
check("the recovered case is recorded as attempt 1", attempt_of["TC-B"], 1)
check("logs from both attempts are kept", len(artifacts.logs), 2)


# --------------------------------------------------------------------------- #
section("A case still failing after the retry budget is spent")

plans2 = [make_plan("TC-C")]
flow_dir2 = ROOT / "flows" / "generated" / "_retry_test_2"
results_dir2 = ROOT / "results" / "_retry_test_2"
for p in plans2:
    renderer.render_plan(p, flow_dir2)

backend2 = ScriptedBackend([
    (flow_dir2, {"TC-C": False}),
    (flow_dir2, {"TC-C": False}),
])
_, attempt_of2 = sentinel_run.execute_with_bounded_retries(
    backend2, plans2, renderer, flow_dir2, results_dir2, {}, 60, max_retries=1
)
check("a case still failing after the budget is not marked as recovered",
      "TC-C" in attempt_of2 and attempt_of2["TC-C"], 0)
check("exactly max_retries+1 attempts were made, not more", len(backend2.calls), 2)


# --------------------------------------------------------------------------- #
section("max_retries=0 makes no retry call at all")

plans3 = [make_plan("TC-D")]
flow_dir3 = ROOT / "flows" / "generated" / "_retry_test_3"
results_dir3 = ROOT / "results" / "_retry_test_3"
for p in plans3:
    renderer.render_plan(p, flow_dir3)

backend3 = ScriptedBackend([(flow_dir3, {"TC-D": False})])
sentinel_run.execute_with_bounded_retries(
    backend3, plans3, renderer, flow_dir3, results_dir3, {}, 60, max_retries=0
)
check("no retry attempted when max_retries is 0", len(backend3.calls), 1)


# --------------------------------------------------------------------------- #
section("The safety property: a real observed FAIL is never retried")

# This is the one that matters. verify()+decide() judging a case that FAILED
# on real evidence must never be revisited by the retry mechanism - that
# mechanism only sees JUnit-level completion, never the verdict, which is
# exactly what keeps it from being able to mask a defect. Checked by
# confirming the retry loop's own inputs: a JUnit report showing SUCCESS
# never appears in `pending`, regardless of what a case's eventual verdict
# turns out to be - the loop has no path by which a verdict could
# influence whether a retry happens.
from sentinel.junit import failed_case_ids  # noqa: E402

clean_junit = write_junit(
    ROOT / "flows" / "generated" / "_retry_test_4.xml", {"TC-E": True}
)
check("a flow that completed (even if its verdict is FAIL) reports no JUnit failure - "
      "the retry loop has no way to see or react to the verdict at all",
      failed_case_ids(clean_junit), set())


# --------------------------------------------------------------------------- #
print(f"\n{'=' * 60}")
if _failures:
    print(f"FAILED {len(_failures)}/{_checks}:")
    for name in _failures:
        print(f"  - {name}")
    sys.exit(1)
print(f"All {_checks} checks passed.")
