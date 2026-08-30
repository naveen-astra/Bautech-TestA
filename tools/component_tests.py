"""Guard tests for the components around the reasoning core.

`selftest.py` proves the judging is sound. This proves the machinery feeding it
refuses bad input rather than passing it along: a sheet with no expectation, a
plan referencing UI that does not exist, a screen map with a dangling reference,
a selector nobody has verified.

Most of these are failures that would otherwise surface an hour into a run, as
an unexplained crash or - much worse - a confident wrong verdict.

    python tools/component_tests.py
"""

from __future__ import annotations

import copy
import csv
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("SENTINEL_ALLOW_UNVERIFIED", "1")

from sentinel import oracle  # noqa: E402
from sentinel.compiler import CompilerError, PlanCompiler, _inline_refs, _PlanDraft  # noqa: E402
from sentinel.parser import SheetError, load_suite, normalise_case_id  # noqa: E402
from sentinel.probes import ProbeReport, augment  # noqa: E402
from sentinel.schema import (  # noqa: E402
    Assertion,
    AssertionKind,
    Capability,
    ObservedValue,
    Segment,
    Step,
    TestPlan,
)
from sentinel.screen_map import ScreenMap, ScreenMapError, UnverifiedTarget  # noqa: E402
from sentinel.verifier import verify  # noqa: E402

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


def rejects(name: str, fn, exc=Exception) -> None:
    global _checks
    _checks += 1
    try:
        fn()
    except exc:
        print(f"  ok    {name}")
        return
    print(f"  FAIL  {name}: accepted something it should have refused")
    _failures.append(name)


def section(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


def sheet(rows, header) -> str:
    handle, path = tempfile.mkstemp(suffix=".csv")
    os.close(handle)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)
    return path


STANDARD = ["ID", "Test case", "Who", "Steps to replicate", "Expected result"]


# --------------------------------------------------------------------------- #
section("Sheet parsing")

cases = load_suite(ROOT / "tests" / "bautech_suite.csv")
check("all 85 cases load", len(cases), 85)
check("three personas", sorted({c.persona_hint for c in cases}),
      ["Admin", "Owner", "Site Engineer"])
check("every case states an expectation",
      all(c.expected_text.strip() for c in cases), True)
check("no editorial hint survives into a title",
      [c.case_id for c in cases if "BLOCK" in c.title.upper()], [])
check("id normalisation", [normalise_case_id(x) for x in ("TC-1", "tc-001", "Sites")],
      ["TC-001", "TC-001", None])

rejects("a row with no expectation is refused",
        lambda: load_suite(sheet([["TC-090", "x", "Owner", "do a thing", ""]], STANDARD)),
        SheetError)
rejects("a duplicate id is refused",
        lambda: load_suite(sheet([["TC-091", "a", "Owner", "s", "e"],
                                  ["TC-091", "b", "Owner", "s", "e"]], STANDARD)),
        SheetError)
rejects("a sheet with no Expected column is refused",
        lambda: load_suite(sheet([["TC-092", "a"]], ["ID", "Test case"])), SheetError)

# A non-technical user renaming headings in Excel must not break the run.
renamed = load_suite(sheet(
    [["TC-086", "Add cement", "Site Engineer", "Material -> Add purchase -> 50",
      "Stock increases by 50", "P"]],
    ["TC ID", "Scenario", "Role", "Steps", "Expected Behaviour", "P/F"]))[0]
check("renamed headers still parse", (renamed.case_id, renamed.persona_hint),
      ("TC-086", "Site Engineer"))
check("a prior verdict column is not carried into the model",
      "P/F" in renamed.model_dump_json(), False)

# Rewording is what makes the live walkthrough work.
original = next(c for c in cases if c.case_id == "TC-046")
reworded = original.model_copy(update={"expected_text": "Make sure the stock count goes up."})
check("rewording changes the cache key", original.source_hash != reworded.source_hash, True)


# --------------------------------------------------------------------------- #
section("Permission oracle")

check("matrix covers every module action", len(oracle.actions()), 80)
check("`yes` survives YAML as a string",
      oracle.grant("Owner", "money", "view_profit_margin"), "yes")

for label, persona, module, action, want in [
    ("engineer cannot create a site", "Site Engineer", "sites", "create_site", False),
    ("admin can", "Admin", "sites", "create_site", True),
    ("engineer cannot approve a join", "Site Engineer", "users", "approve_reject_join", False),
    ("engineer can still invite", "Site Engineer", "users", "invite_share_site_link", True),
    ("admin cannot open billing", "Admin", "company", "billing_subscription", False),
    ("admin cannot see margin", "Admin", "money", "view_profit_margin", False),
    ("owner can see margin", "Owner", "money", "view_profit_margin", True),
    ("nobody can DM", "Site Engineer", "chat", "direct_message", False),
]:
    check(label, oracle.is_allowed(persona, module, action), want)

check("engineer deletes only their own material",
      oracle.is_scoped("Site Engineer", "material", "delete_material_entry"), True)
check("control persona for a site-creation probe",
      oracle.control_persona("Site Engineer", "sites", "create_site"), "Admin")
check("control persona for a margin probe",
      oracle.control_persona("Admin", "money", "view_profit_margin"), "Owner")
check("no control persona when nobody is permitted",
      oracle.control_persona("Site Engineer", "chat", "direct_message"), None)


# --------------------------------------------------------------------------- #
section("Screen map")

screen_map = ScreenMap.load()
vocabulary = screen_map.vocabulary()
check("vocabulary is populated",
      all(vocabulary[k] for k in ("screens", "controls", "values", "entities")), True)

os.environ["SENTINEL_ALLOW_UNVERIFIED"] = ""
rejects("an unverified target refuses to render",
        lambda: ScreenMap.load().control("add_site_button"), UnverifiedTarget)
os.environ["SENTINEL_ALLOW_UNVERIFIED"] = "1"

raw = copy.deepcopy(screen_map._data)
raw["controls"]["ghost"] = {"screen": "nowhere", "selector": {"text": "x"}}
rejects("a control pointing at a missing screen is refused",
        lambda: ScreenMap(raw), ScreenMapError)

raw2 = copy.deepcopy(screen_map._data)
raw2["screens"]["ghost"] = {"source": "assumed"}
rejects("a screen with no anchor is refused", lambda: ScreenMap(raw2), ScreenMapError)

# The lint catches anchors that cannot prove arrival. The seeded map is full of
# them, which is the point: they are placeholders awaiting a real build.
check("lint flags anchors that cannot prove arrival", len(screen_map.lint()) > 0, True)


# --------------------------------------------------------------------------- #
section("Plan compiler")

schema = _inline_refs(_PlanDraft.model_json_schema())
import json as _json  # noqa: E402

check("tool schema is self-contained", "$ref" not in _json.dumps(schema), True)
check("model cannot author identity fields",
      "case_id" in schema["properties"], False)


class _Block:
    def __init__(self, payload):
        self.type, self.input, self.id = "tool_use", payload, "tu"


class _Response:
    def __init__(self, payload):
        self.content = [_Block(payload)]


class _FakeClient:
    """Stands in for the Anthropic client so the compiler is testable offline."""

    def __init__(self, payloads):
        self.payloads, self.calls = list(payloads), 0

    class _Messages:
        def __init__(self, outer):
            self.outer = outer

        def create(self, **kwargs):
            self.outer.calls += 1
            return _Response(self.outer.payloads.pop(0))

    @property
    def messages(self):
        return _FakeClient._Messages(self)


GOOD_PLAN = {
    "primary_persona": "Site Engineer",
    "segments": [{"persona": "Site Engineer", "intent": "buy cement", "steps": [
        {"capability": "navigate", "target": "material"},
        {"capability": "read_value", "target": "stock_level", "observation_key": "before"},
        {"capability": "create_entity", "target": "material_purchase",
         "args": {"item": "SNTL-cement-${RUN_ID}", "quantity": 100}},
        {"capability": "read_value", "target": "stock_level", "observation_key": "after"}]}],
    "assertions": [{"id": "delta", "kind": "numeric_delta", "expected_text": "+100",
                    "consumes": ["before", "after"], "expected_delta": 100}],
    "cleanup": [], "feasibility": "automatable",
}
BAD_TARGET = copy.deepcopy(GOOD_PLAN)
BAD_TARGET["segments"][0]["steps"][0]["target"] = "screen_that_does_not_exist"

with tempfile.TemporaryDirectory() as cache:
    compiler = PlanCompiler(screen_map, client=_FakeClient([GOOD_PLAN]), cache_dir=cache)
    plan = compiler.compile(original)
    check("compiles a plan", plan.case_id, "TC-046")
    check("identity comes from the sheet", plan.source_hash, original.source_hash)

    warm = PlanCompiler(screen_map, client=_FakeClient([]), cache_dir=cache)
    warm.compile(original)
    check("a second run makes no model call", warm.stats["cache_hits"], 1)

    fresh = PlanCompiler(screen_map, client=_FakeClient([GOOD_PLAN]), cache_dir=cache)
    fresh.compile(reworded)
    check("a reworded case is re-planned", fresh.stats["compiled"], 1)

    repairing = PlanCompiler(
        screen_map, client=_FakeClient([BAD_TARGET, GOOD_PLAN]), cache_dir=cache)
    repairing.compile(original, use_cache=False)
    check("an unknown target triggers one repair", repairing.stats["repairs"], 1)

    stubborn = PlanCompiler(
        screen_map, client=_FakeClient([BAD_TARGET, BAD_TARGET]), cache_dir=cache)
    rejects("two bad drafts fail loudly rather than yielding a broken plan",
            lambda: stubborn.compile(original, use_cache=False), CompilerError)


# --------------------------------------------------------------------------- #
section("Differential probes")


def prohibition(case_id: str, persona: str, control: str) -> TestPlan:
    return TestPlan(
        case_id=case_id, source_hash="h", title=case_id, primary_persona=persona,
        segments=[Segment(persona=persona, intent="probe", steps=[
            Step(capability=Capability.NAVIGATE, target="sites_list"),
            Step(capability=Capability.PROBE_CONTROL, target=control,
                 observation_key="probe")])],
        assertions=[Assertion(id="a", kind=AssertionKind.CONTROL_ABSENT,
                              expected_text="must be blocked", consumes=["probe"],
                              control=control)])


report = ProbeReport()
augmented = augment(prohibition("TC-009", "Site Engineer", "add_site_button"),
                    screen_map, report)
check("a control segment is appended", len(augmented.segments), 2)
check("run by the persona the spec permits", augmented.segments[-1].persona, "Admin")
check("the control probe navigates before looking",
      augmented.segments[-1].steps[0].capability, Capability.NAVIGATE)
check("it mirrors the original probe",
      augmented.segments[-1].steps[-1].target, "add_site_button")
check("the control evidence becomes mandatory",
      augmented.assertions[0].consumes, ["probe", "probe__control"])

unprovable = augment(prohibition("TC-080", "Site Engineer", "direct_message_button"),
                     screen_map, ProbeReport())
check("no control probe when nobody is permitted", len(unprovable.segments), 1)

# The property the whole mechanism rests on.
missing_control = verify(
    augmented,
    {"probe": ObservedValue(key="probe", raw="absent", anchor_found=True)},
    screen_map.refusal_markers)[0]
check("absence without its control probe cannot pass", missing_control.passed, False)
check("  and is classed as our failure",
      missing_control.failure_class.value, "automation_failure")


# --------------------------------------------------------------------------- #
print(f"\n{'=' * 60}")
if _failures:
    print(f"FAILED {len(_failures)}/{_checks}:")
    for name in _failures:
        print(f"  - {name}")
    sys.exit(1)
print(f"All {_checks} checks passed.")
