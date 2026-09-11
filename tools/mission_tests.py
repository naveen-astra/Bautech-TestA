"""Prove the seam: a thinking agent still cannot manufacture a verdict.

The agent is free to navigate however it likes. What it is not free to do is
decide whether it succeeded. These checks pin that down on real plan shapes,
with a scripted brain and no device, so the guarantee is tested rather than
asserted:

*   Numbers are settled by subtraction, not by the agent's opinion. An agent
    that says "looks right" while the numbers say otherwise still gets FAIL.
*   An absence noticed without confirming the screen is never a pass. This is
    the assessment's hardest requirement and the one a naive agent fails
    silently.
*   A forbidden action that actually goes through is reported as a defect,
    even though the agent was the one that performed it.
*   An agent that gets lost produces an automation failure, not a verdict
    about the app.

    python tools/mission_tests.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SENTINEL_ALLOW_UNVERIFIED", "1")

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from sentinel import actions as actions_module  # noqa: E402
from sentinel import agent as agent_module  # noqa: E402
from sentinel.mission import execute, mission_for  # noqa: E402
from sentinel.perception import Element, Screen  # noqa: E402
from sentinel.schema import (  # noqa: E402
    Assertion,
    AssertionKind,
    Capability,
    FailureClass,
    Feasibility,
    RawTestCase,
    Segment,
    Step,
    TestPlan,
    Verdict,
)

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


class ScriptedBrain:
    def __init__(self, script):
        self.script = list(script)
        self.first_message = ""

    def decide(self, system, messages):
        if not self.first_message:
            self.first_message = messages[0]["content"]
        if not self.script:
            return "give_up", {"reason": "script exhausted"}
        return self.script.pop(0)


def install_device(labels: list[str]):
    screen = Screen(
        elements=[
            Element(index=i + 1, text=t, role="BUTTON", clickable=True, focused=False,
                    scrollable=False, bounds=(0, i * 100, 200, i * 100 + 80))
            for i, t in enumerate(labels)
        ],
        package="pkg", width=720, height=1600,
    )
    agent_module.capture = lambda adb, package, attempts=3: screen
    actions_module._shell = lambda adb, *a, timeout=30: ""


def delta_plan() -> tuple[RawTestCase, TestPlan]:
    case = RawTestCase(
        case_id="TC-046", title="Add purchase / stock", persona_hint="Site Engineer",
        steps_text="Site A -> Material -> Add purchase -> item, qty, vendor -> Save.",
        expected_text="Stock increases by the quantity added.",
    )
    plan = TestPlan(
        case_id="TC-046", source_hash="h", title=case.title,
        expected_text=case.expected_text, primary_persona="Site Engineer",
        segments=[Segment(persona="Site Engineer", intent="add stock", steps=[
            Step(capability=Capability.READ_VALUE, target="stock_level",
                 observation_key="stock_before"),
            Step(capability=Capability.READ_VALUE, target="stock_level",
                 observation_key="stock_after"),
        ])],
        assertions=[Assertion(
            id="delta", kind=AssertionKind.NUMERIC_DELTA,
            expected_text="Stock increases by the quantity added.",
            consumes=["stock_before", "stock_after"], expected_delta=100,
        )],
    )
    return case, plan


def absence_plan() -> tuple[RawTestCase, TestPlan]:
    case = RawTestCase(
        case_id="TC-009", title="Create site - Engineer", persona_hint="Site Engineer",
        steps_text="Sites -> look for Add Site.",
        expected_text="Blocked - no Add Site option for Site Engineer.",
    )
    plan = TestPlan(
        case_id="TC-009", source_hash="h", title=case.title,
        expected_text=case.expected_text, primary_persona="Site Engineer",
        segments=[Segment(persona="Site Engineer", intent="look for Add Site", steps=[
            Step(capability=Capability.PROBE_CONTROL, target="add_site_button",
                 observation_key="probe"),
        ])],
        assertions=[Assertion(
            id="absent", kind=AssertionKind.CONTROL_ABSENT,
            expected_text="Blocked - no Add Site option for Site Engineer.",
            consumes=["probe"], control="add_site_button",
        )],
    )
    return case, plan


# --------------------------------------------------------------------------- #
section("The mission tells the agent what to bring back, never where to go")

case, plan = delta_plan()
mission = mission_for(plan)
check("both observations are demanded", sorted(mission), ["stock_after", "stock_before"])
check_true("the before/after distinction is explained",
           "before you act" in mission["stock_before"]
           and "after you act" in mission["stock_after"])
route_words = ("tap", "navigate", "menu", "button labelled", "screen map")
check_true("the mission contains no route for the agent to follow",
           not any(word in " ".join(mission.values()).lower() for word in route_words))

_, absence = absence_plan()
absence_mission = mission_for(absence)
check_true("an absence case asks for a proper look, not a assumption",
           "look" in absence_mission["probe"].lower())


# --------------------------------------------------------------------------- #
section("Numbers are settled by subtraction, not by the agent's opinion")

install_device(["Cement", "Total Stock: 500"])
brain = ScriptedBrain([
    ("confirm_screen", {"screen": "Material", "evidence": "item list with stock levels"}),
    ("note", {"key": "stock_before", "value": "500"}),
    ("note", {"key": "stock_after", "value": "600"}),
    ("finish", {"summary": "added 100 bags", "reached_target_screen": True}),
])
result, run = execute(case, plan, brain, adb="adb", package="pkg", log=lambda m: None)
check("a real +100 is a PASS", result.verdict, Verdict.PASS)

# The same agent, equally confident, but the app moved by 50.
install_device(["Cement", "Total Stock: 500"])
brain = ScriptedBrain([
    ("confirm_screen", {"screen": "Material", "evidence": "item list with stock levels"}),
    ("note", {"key": "stock_before", "value": "500"}),
    ("note", {"key": "stock_after", "value": "550"}),
    ("finish", {"summary": "looked fine to me, saved successfully",
                "reached_target_screen": True}),
])
result, run = execute(case, plan, brain, adb="adb", package="pkg", log=lambda m: None)
check("a +50 is a FAIL however confident the agent sounded", result.verdict, Verdict.FAIL)
check("and it is called an app defect, not our failure",
      result.failure_class, FailureClass.APPLICATION_DEFECT)
check_true("both real numbers appear in the report",
           "500" in result.actual and "550" in result.actual)


# --------------------------------------------------------------------------- #
section("An absence proves nothing until the screen is confirmed")

install_device(["Sites", "Aqua Line", "Terminal 1"])
brain = ScriptedBrain([
    # Never confirms the screen - just declares the control missing.
    ("note", {"key": "probe", "value": "absent"}),
    ("finish", {"summary": "no Add Site anywhere", "reached_target_screen": False}),
])
result, run = execute(case=absence_plan()[0], plan=absence_plan()[1], brain=brain,
                      adb="adb", package="pkg", log=lambda m: None)
check("an unconfirmed absence is never a PASS", result.verdict != Verdict.PASS, True)
check("it is charged to our automation, not to the app",
      result.failure_class, FailureClass.AUTOMATION_FAILURE)

install_device(["Sites", "Aqua Line", "Terminal 1"])
brain = ScriptedBrain([
    ("confirm_screen", {"screen": "Sites",
                        "evidence": "the site list with Aqua Line and Terminal 1"}),
    ("scroll", {"direction": "down"}),
    ("note", {"key": "probe", "value": "absent"}),
    ("finish", {"summary": "looked properly, no Add Site control",
                "reached_target_screen": True}),
])
result, run = execute(case=absence_plan()[0], plan=absence_plan()[1], brain=brain,
                      adb="adb", package="pkg", log=lambda m: None)
check_true("a confirmed absence is at least judgeable", result.verdict != Verdict.BLOCKED
           or result.failure_class != FailureClass.AUTOMATION_FAILURE)
check_true("the confirmation itself is in the evidence",
           any("confirmed on Sites" in e for e in result.evidence))
check_true("the agent's reasons are recorded, not just its conclusion",
           any("Aqua Line" in e for e in result.evidence))


# --------------------------------------------------------------------------- #
section("A forbidden action that goes through is reported, not buried")

install_device(["Sites", "Add Site"])
brain = ScriptedBrain([
    ("confirm_screen", {"screen": "Sites", "evidence": "the site list"}),
    ("note", {"key": "probe", "value": "present"}),
    ("finish", {"summary": "Add Site was there and I could use it",
                "reached_target_screen": True}),
])
result, run = execute(case=absence_plan()[0], plan=absence_plan()[1], brain=brain,
                      adb="adb", package="pkg", log=lambda m: None)
check("a control that should be absent but is present is a FAIL", result.verdict, Verdict.FAIL)
check("and it is the app's defect", result.failure_class, FailureClass.APPLICATION_DEFECT)


# --------------------------------------------------------------------------- #
section("An agent that gets lost does not get to have an opinion")

install_device(["Welcome Back", "Send OTP"])
brain = ScriptedBrain([
    ("give_up", {"reason": "never got past the login screen"}),
])
result, run = execute(case, plan, brain, adb="adb", package="pkg", log=lambda m: None)
check("being lost is BLOCKED", result.verdict, Verdict.BLOCKED)
check("charged to automation, not to the app",
      result.failure_class, FailureClass.AUTOMATION_FAILURE)
check_true("the reason survives into the report", "login" in result.actual.lower())


# --------------------------------------------------------------------------- #
section("A case with no interface to test through is never attempted")

_, unattemptable = delta_plan()
unattemptable = unattemptable.model_copy(update={
    "feasibility": Feasibility.NEEDS_UNAVAILABLE_INTERFACE,
    "feasibility_reason": "needs a real card payment",
})
brain = ScriptedBrain([("finish", {"summary": "x", "reached_target_screen": True})])
result, run = execute(case, unattemptable, brain, adb="adb", package="pkg", log=lambda m: None)
check("it is BLOCKED with a reason", result.verdict, Verdict.BLOCKED)
check("no device time was spent on it", len(run.steps), 0)


# --------------------------------------------------------------------------- #
section("The agent reads the sheet's own words")

install_device(["Sites"])
brain = ScriptedBrain([("finish", {"summary": "done", "reached_target_screen": True})])
execute(case=absence_plan()[0], plan=absence_plan()[1], brain=brain,
        adb="adb", package="pkg", log=lambda m: None)
check_true("the tester's expected result reached the agent verbatim",
           "Blocked - no Add Site option for Site Engineer." in brain.first_message)
check_true("nothing labelled it a negative case",
           "negative" not in brain.first_message.lower())


# --------------------------------------------------------------------------- #
print(f"\n{'=' * 60}")
if _failures:
    print(f"FAILED {len(_failures)}/{_checks}:")
    for name in _failures:
        print(f"  - {name}")
    sys.exit(1)
print(f"All {_checks} checks passed.")
print("The agent decides how. It never decides whether.")
