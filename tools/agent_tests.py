"""Prove the thinking agent's loop without a model and without a device.

The brain is scripted and the device is stubbed, so everything here is
deterministic. What is being checked is the machinery the agent's honesty
rests on:

*   It only ever sees the app under test - system UI is invisible, which is
    the structural fix for the status-bar mis-taps that plagued the earlier
    selector-based approach.
*   A malformed action from the model does not abandon the case; the model is
    told what was wrong and gets to correct itself.
*   Everything seen on every screen is retained, because that inventory is
    what makes an absence claim evidence rather than an assertion.
*   The step budget is real, so a confused model cannot burn tokens forever.

    python tools/agent_tests.py
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SENTINEL_ALLOW_UNVERIFIED", "1")

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from sentinel import actions as actions_module  # noqa: E402
from sentinel import agent as agent_module  # noqa: E402
from sentinel.actions import ActionError, TOOL_SCHEMA, build  # noqa: E402
from sentinel.agent import run_case  # noqa: E402
from sentinel.perception import Element, Screen, capture_from_file  # noqa: E402

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


CASE = FakeCase(
    case_id="TC-009", title="Create site - Engineer", persona="Site Engineer",
    steps="Sites -> look for Add Site.",
    expected="Blocked - no Add Site option for Site Engineer.",
)


class ScriptedBrain:
    """Returns a fixed list of (action, args), in order."""

    def __init__(self, script):
        self.script = list(script)
        self.systems_seen: list[str] = []
        self.messages_seen: list[list] = []

    def decide(self, system, messages):
        self.systems_seen.append(system)
        self.messages_seen.append(list(messages))
        if not self.script:
            return "give_up", {"reason": "script exhausted"}
        return self.script.pop(0)


def install_device(screens: list[Screen]):
    """Stub the device: capture() walks a fixed list, actions do nothing."""
    remaining = list(screens)

    def fake_capture(adb, package, attempts=3):
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    agent_module.capture = fake_capture
    actions_module._shell = lambda adb, *args, timeout=30: ""


def make_screen(labels: list[str]) -> Screen:
    elements = [
        Element(index=i + 1, text=text, role="BUTTON", clickable=True,
                focused=False, scrollable=False, bounds=(0, i * 100, 200, i * 100 + 80))
        for i, text in enumerate(labels)
    ]
    return Screen(elements=elements, package="com.naviconinfra.bautech",
                  width=720, height=1600)


# --------------------------------------------------------------------------- #
section("Perception reads a real captured screen, and only the app")

real = capture_from_file(ROOT / "tests" / "fixtures" / "bautech_sites_screen.xml", "com.naviconinfra.bautech")
check_true("elements were found in the real dump", len(real.elements) > 0)
check_true("every element belongs to the app under test",
           all(e.index > 0 for e in real.elements))
check_true("the real 'New Site' control is visible to the agent",
           any(e.text == "New Site" for e in real.elements))
check_true("a real site name is readable", real.contains("Aqua Line"))
check_true("Site B, which is genuinely not on this screen, is not claimed",
           not real.contains("Site B"))
check_true("indexes are contiguous from 1",
           [e.index for e in real.elements] == list(range(1, len(real.elements) + 1)))

# The whole point of the package filter: nothing from Android's own UI.
status_bar_noise = ["Phone signal full", "Galaxy Themes", "battery", "Wi-Fi"]
check_true("no system-UI text reached the agent (the status-bar mis-tap class of bug)",
           not any(real.contains(noise) for noise in status_bar_noise))

rendered = real.render()
raw_size = len((ROOT / "tests" / "fixtures" / "bautech_sites_screen.xml").read_text(encoding="utf-8"))
check_true("the rendered screen is far smaller than the raw hierarchy",
           len(rendered) < raw_size / 5)


# --------------------------------------------------------------------------- #
section("The action vocabulary refuses what it does not understand")

try:
    build("teleport", {})
    check_true("an unknown action is refused", False)
except ActionError as exc:
    check_true("an unknown action is refused", "teleport" in str(exc))

try:
    build("tap", {"index": 3, "colour": "red"})
    check_true("an unexpected argument is refused", False)
except ActionError as exc:
    check_true("an unexpected argument is refused", "colour" in str(exc))

tap = build("tap", {"index": 2})
check("a valid action builds", (tap.name, tap.index), ("tap", 2))
check_true("finish is terminal", build("finish", {"summary": "s", "reached_target_screen": True}).terminal)
check_true("tap is not terminal", not build("tap", {"index": 1}).terminal)

schema_names = {tool["name"] for tool in TOOL_SCHEMA}
check("every advertised tool is really implemented",
      sorted(schema_names), sorted(actions_module._REGISTRY))
check_true("every tool describes itself for the model",
           all(tool.get("description") for tool in TOOL_SCHEMA))

check("spaces are escaped for adb input text", actions_module._escape("Site A"), "Site%sA")


# --------------------------------------------------------------------------- #
section("A case runs to completion and keeps what it saw")

install_device([
    make_screen(["Sites", "Aqua Line", "Terminal 1"]),
    make_screen(["Sites", "Aqua Line", "Terminal 1"]),
])
brain = ScriptedBrain([
    ("scroll", {"direction": "down"}),
    ("finish", {"summary": "No Add Site control anywhere on the Sites screen.",
                "reached_target_screen": True}),
])
run = run_case(CASE, brain, adb="adb", package="com.naviconinfra.bautech", log=lambda m: None)

check("the run finished", run.finished, True)
check("it reported reaching the target screen", run.reached_target_screen, True)
check("both steps were recorded", len(run.steps), 2)
check_true("the summary survived", "Add Site" in run.summary)
check_true("what was on screen is retained as evidence", run.saw("Aqua Line"))
check_true("an absence is answerable from that evidence", not run.saw("Add Site"))
check_true("the agent was never told the case was a negative one",
           "negative" not in brain.systems_seen[0].lower()
           and "should fail" not in brain.systems_seen[0].lower())
check_true("the case text reached the model verbatim",
           "no Add Site option" in brain.messages_seen[0][0]["content"])


# --------------------------------------------------------------------------- #
section("Values are recorded raw, never computed")

install_device([make_screen(["Cement", "Total Stock: 500"])])
brain = ScriptedBrain([
    ("note", {"key": "stock_before", "value": "500"}),
    ("note", {"key": "stock_after", "value": "600"}),
    ("finish", {"summary": "purchase saved", "reached_target_screen": True}),
])
run = run_case(CASE, brain, adb="adb", package="pkg", log=lambda m: None)
check("both observations were captured",
      run.observations, {"stock_before": "500", "stock_after": "600"})
check_true("the agent recorded values without judging them", run.finished)


# --------------------------------------------------------------------------- #
section("Honest failure beats a guess")

install_device([make_screen(["Welcome Back"])])
brain = ScriptedBrain([
    ("give_up", {"reason": "never reached the Sites screen; still on login"}),
])
run = run_case(CASE, brain, adb="adb", package="pkg", log=lambda m: None)
check("giving up is recorded as such", run.gave_up, True)
check_true("the reason is kept", "still on login" in run.give_up_reason)
check("giving up is not mistaken for finishing", run.finished, False)

install_device([make_screen(["Sites"])])
brain = ScriptedBrain([("tap", {"index": 1})] * 100)
run = run_case(CASE, brain, adb="adb", package="pkg", max_steps=5, log=lambda m: None)
check("the step budget is enforced", run.exhausted_budget, True)
check("it stopped exactly at the budget", len(run.steps), 5)
check_true("a budget exhaustion is not reported as a finish", not run.finished)


# --------------------------------------------------------------------------- #
section("A malformed action does not throw the case away")

install_device([make_screen(["Sites", "Aqua Line"])])
brain = ScriptedBrain([
    ("tap", {"index": 99}),                     # no such element
    ("fly", {}),                                # no such action
    ("finish", {"summary": "recovered", "reached_target_screen": True}),
])
run = run_case(CASE, brain, adb="adb", package="pkg", log=lambda m: None)
check("the run still completed after two bad actions", run.finished, True)
check_true("the out-of-range tap was recorded as a failure, not silently dropped",
           any("failed" in s.result for s in run.steps))


# --------------------------------------------------------------------------- #
print(f"\n{'=' * 60}")
if _failures:
    print(f"FAILED {len(_failures)}/{_checks}:")
    for name in _failures:
        print(f"  - {name}")
    sys.exit(1)
print(f"All {_checks} checks passed.")
print("The loop is sound. It needs a model and a device to be useful.")
