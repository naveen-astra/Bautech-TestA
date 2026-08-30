"""Prove the reasoning core is sound, without a device.

Runs the verifier and adjudicator against synthetic observations covering every
outcome that matters. It answers the question a reviewer should ask first:

    "How do I know this thing is not just printing PASS?"

The load-bearing cases are the ones where the app looks blocked but is not, and
where our automation is broken but could be mistaken for enforcement. Those must
come back as automation failures. If any of them ever returns PASS, the suite is
capable of manufacturing green results and the whole submission is worthless.

    python tools/selftest.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sentinel.schema import (  # noqa: E402
    ActionOutcome,
    Assertion,
    AssertionKind,
    FailureClass,
    ObservedValue,
    Segment,
    Step,
    Capability,
    TestPlan,
)
from sentinel.verifier import parse_number, verify  # noqa: E402

REFUSALS = ["not authorised", "permission denied", "access denied"]

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


def plan_with(assertion: Assertion, keys: list[str]) -> TestPlan:
    """A minimal plan whose steps produce exactly the given observation keys."""
    steps = [
        Step(capability=Capability.PROBE_CONTROL, target="add_site_button", observation_key=k)
        for k in keys
    ]
    return TestPlan(
        case_id="TC-000",
        source_hash="h",
        title="synthetic",
        primary_persona="Site Engineer",
        segments=[Segment(persona="Site Engineer", intent="synthetic", steps=steps)],
        assertions=[assertion],
    )


def judge(assertion: Assertion, observations: list[ObservedValue]):
    obs = {o.key: o for o in observations}
    plan = plan_with(assertion, [o.key for o in observations])
    return verify(plan, obs, REFUSALS)[0]


def probe(key: str, state: str, *, anchor: bool = True, text: list[str] | None = None):
    return ObservedValue(
        key=key, raw=state, anchor_found=anchor, screen_texts=text or []
    )


# --------------------------------------------------------------------------- #

section("Number parsing (refuses to guess)")
check("plain integer", parse_number("600"), 600.0)
check("thousands separator", parse_number("Rs 1,300"), 1300.0)
check("unit suffix", parse_number("600 bags"), 600.0)
check("rupee symbol", parse_number("₹ 1,300.50"), 1300.5)
check("ambiguous '500 of 600' refused", parse_number("500 of 600"), None)
check("no number", parse_number("Stock unavailable"), None)


section("Numeric state (positive cases, and the seeded-regression shape)")
delta = Assertion(
    id="stock", kind=AssertionKind.NUMERIC_DELTA,
    expected_text="Stock increases by 100", consumes=["before", "after"], expected_delta=100,
)
r = judge(delta, [probe("before", "500"), probe("after", "600")])
check("500 -> 600 passes", (r.passed, r.failure_class), (True, None))

r = judge(delta, [probe("before", "500"), probe("after", "550")])
check("500 -> 550 fails as app defect",
      (r.passed, r.failure_class), (False, FailureClass.APPLICATION_DEFECT))
check("  and reports both numbers", "500 -> 550" in r.actual, True)

r = judge(delta, [probe("before", "unavailable"), probe("after", "600")])
check("unreadable value is our failure, not a defect",
      (r.passed, r.failure_class), (False, FailureClass.AUTOMATION_FAILURE))

r = judge(delta, [probe("before", "500", anchor=False), probe("after", "600")])
check("never reached the screen -> automation failure",
      (r.passed, r.failure_class), (False, FailureClass.AUTOMATION_FAILURE))


section("Missing evidence never passes")
orphan = Assertion(
    id="orphan", kind=AssertionKind.NUMERIC_DELTA,
    expected_text="Stock increases by 100", consumes=["before", "after"], expected_delta=100,
)
obs = {"before": probe("before", "500")}
plan = plan_with(orphan, ["before", "after"])
r = verify(plan, obs, REFUSALS)[0]
check("uncaptured observation -> automation failure",
      (r.passed, r.failure_class), (False, FailureClass.AUTOMATION_FAILURE))


section("control_absent: the differential probe")
absent = Assertion(
    id="no-add-site", kind=AssertionKind.CONTROL_ABSENT,
    expected_text="Blocked - no Add Site option for Site Engineer",
    consumes=["probe"], control="add_site_button",
)

r = judge(absent, [probe("probe", "absent"), probe("probe__control", "present")])
check("hidden from engineer, visible to admin -> PASS", (r.passed, r.failure_class), (True, None))
check("  ladder records all three rungs",
      (r.ladder.get("reachability"), r.ladder.get("affordance"), r.ladder.get("differential")),
      (True, True, True))

# The single most important case in this file.
r = judge(absent, [probe("probe", "absent"), probe("probe__control", "absent")])
check("hidden from BOTH -> automation failure, never PASS",
      (r.passed, r.failure_class), (False, FailureClass.AUTOMATION_FAILURE))

r = judge(absent, [probe("probe", "present"), probe("probe__control", "present")])
check("engineer can see a forbidden control -> app defect",
      (r.passed, r.failure_class), (False, FailureClass.APPLICATION_DEFECT))

r = judge(absent, [probe("probe", "absent", anchor=False), probe("probe__control", "present")])
check("never reached the screen -> automation failure",
      (r.passed, r.failure_class), (False, FailureClass.AUTOMATION_FAILURE))

r = judge(absent, [probe("probe", "absent")])
check("no control probe -> blocked, not passed",
      (r.passed, r.failure_class), (False, FailureClass.ENVIRONMENT_BLOCK))


section("token_absent: proving Site B is invisible everywhere")
token = Assertion(
    id="no-site-b", kind=AssertionKind.TOKEN_ABSENT,
    expected_text="zero Site B data appears anywhere",
    consumes=["reports", "dashboard"], token="Site B",
)

r = judge(token, [
    probe("reports", "", text=["Site A", "Total 41,000"]),
    probe("dashboard", "", text=["Site A progress"]),
    probe("reports__control", "", text=["Site A", "Site B", "Total 92,000"]),
])
check("absent for engineer, present for admin -> PASS", (r.passed, r.failure_class), (True, None))

r = judge(token, [
    probe("reports", "", text=["Site A", "Site B leaked here"]),
    probe("dashboard", "", text=["Site A progress"]),
    probe("reports__control", "", text=["Site B"]),
])
check("engineer sees Site B -> app defect",
      (r.passed, r.failure_class), (False, FailureClass.APPLICATION_DEFECT))
check("  and names where it leaked", "reports" in r.actual, True)

r = judge(token, [
    probe("reports", "", text=["Site A"]),
    probe("dashboard", "", text=["Site A"]),
    probe("reports__control", "", text=["Site A"]),
])
check("invisible to everyone -> automation failure, not PASS",
      (r.passed, r.failure_class), (False, FailureClass.AUTOMATION_FAILURE))

r = judge(token, [
    probe("reports", "", text=["Site A"]),
    probe("dashboard", "", anchor=False, text=[]),
    probe("reports__control", "", text=["Site B"]),
])
check("a screen in scope never opened -> automation failure",
      (r.passed, r.failure_class), (False, FailureClass.AUTOMATION_FAILURE))


section("action_rejected: attempting the forbidden action")
rejected = Assertion(
    id="cannot-approve", kind=AssertionKind.ACTION_REJECTED,
    expected_text="Blocked - only Admin/Owner can approve", consumes=["attempt"],
)


def attempt(outcome: ActionOutcome, *, anchor: bool = True, text: list[str] | None = None):
    return ObservedValue(
        key="attempt", raw="", outcome=outcome, anchor_found=anchor, screen_texts=text or []
    )


r = judge(rejected, [attempt(ActionOutcome.REFUSED, text=["Permission denied"])])
check("explicitly refused -> PASS", (r.passed, r.failure_class), (True, None))
check("  cites the refusal message", "explicit refusal" in r.actual, True)

r = judge(rejected, [attempt(ActionOutcome.SUCCEEDED)])
check("action went through -> app defect",
      (r.passed, r.failure_class), (False, FailureClass.APPLICATION_DEFECT))

r = judge(rejected, [attempt(ActionOutcome.STALLED)])
check("attempt stalled -> automation failure",
      (r.passed, r.failure_class), (False, FailureClass.AUTOMATION_FAILURE))

r = judge(rejected, [attempt(ActionOutcome.NO_CHANGE)])
check("accepted but nothing changed -> PASS", (r.passed, r.failure_class), (True, None))

r = judge(rejected, [attempt(ActionOutcome.REFUSED, anchor=False)])
check("never reached the screen -> automation failure",
      (r.passed, r.failure_class), (False, FailureClass.AUTOMATION_FAILURE))


section("Every verdict carries its reasoning")
r = judge(absent, [probe("probe", "absent"), probe("probe__control", "present")])
check("reasoning is populated", bool(r.reasoning.strip()), True)
check("evidence is cited", len(r.evidence) >= 2, True)


# --------------------------------------------------------------------------- #
print(f"\n{'=' * 60}")
if _failures:
    print(f"FAILED {len(_failures)}/{_checks}:")
    for name in _failures:
        print(f"  - {name}")
    sys.exit(1)
print(f"All {_checks} checks passed.")
print("No configuration of missing or broken evidence produced a PASS.")
