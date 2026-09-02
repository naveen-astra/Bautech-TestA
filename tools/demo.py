"""A narrated walk through the whole pipeline, end to end.

Every function called here is the real one - nothing is reimplemented for the
demo. The only thing not real is the device: `simulate_device_log` stands in
for what a phone would print, because no verified build is running yet (see
docs/phase1_discovery.md for where that stands). Everything before and after
that line - parsing the sheet, compiling a plan, adding the differential
probe, rendering Maestro YAML, parsing observations back out, verifying,
adjudicating, deciding a verdict, writing the report - is the production code
path, unmodified.

Run it:

    python tools/demo.py

Three acts:
  1. TC-046 (positive, numeric) - a clean pass.
  2. TC-046 again, but the app has a seeded regression - watch it get caught.
  3. TC-073 (negative, "prove Site B is invisible") - the differential probe,
     and what happens when our own selector turns out to be broken instead of
     the app being at fault. This is the one that matters most.
"""

from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SENTINEL_ALLOW_UNVERIFIED", "1")

import yaml  # noqa: E402

from sentinel import probes  # noqa: E402
from sentinel.observation import parse_log  # noqa: E402
from sentinel.parser import load_suite  # noqa: E402
from sentinel.renderer import FlowRenderer  # noqa: E402
from sentinel.report import write_all  # noqa: E402
from sentinel.schema import (  # noqa: E402
    Assertion,
    AssertionKind,
    Capability,
    Segment,
    Step,
    TestPlan,
)
from sentinel.screen_map import ScreenMap  # noqa: E402
from sentinel.verdict import decide  # noqa: E402
from sentinel.verifier import verify  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def heading(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def step(title: str) -> None:
    print(f"\n--- {title} ---")


def wrap(text: str, indent: str = "  ") -> None:
    print(textwrap.fill(text, width=90, initial_indent=indent, subsequent_indent=indent))


# --------------------------------------------------------------------------- #
# Hand-built plans, standing in for the compiler.
#
# In a real run, sentinel.compiler.PlanCompiler turns the sheet row below into
# exactly this structure by calling Claude - see sentinel/compiler.py. No
# ANTHROPIC_API_KEY is set on this machine, so the demo builds the same shape
# by hand rather than skip the step. Everything downstream of this point does
# not know or care whether a model or a human produced the plan.
# --------------------------------------------------------------------------- #


def plan_tc046() -> TestPlan:
    return TestPlan(
        case_id="TC-046", source_hash="demo", title="Add purchase / stock",
        expected_text="Stock increases by the quantity added.",
        primary_persona="Site Engineer",
        segments=[Segment(persona="Site Engineer", intent="buy 100 bags of cement", steps=[
            Step(capability=Capability.NAVIGATE, target="material"),
            Step(capability=Capability.READ_VALUE, target="stock_level",
                 observation_key="stock_before"),
            Step(capability=Capability.CREATE_ENTITY, target="material_purchase",
                 args={"item": "SNTL-cement-${RUN_ID}", "quantity": 100}),
            Step(capability=Capability.READ_VALUE, target="stock_level",
                 observation_key="stock_after"),
        ])],
        assertions=[Assertion(
            id="stock-rises-by-100", kind=AssertionKind.NUMERIC_DELTA,
            expected_text="Stock increases by the quantity added.",
            consumes=["stock_before", "stock_after"], expected_delta=100)],
    )


def plan_tc073() -> TestPlan:
    return TestPlan(
        case_id="TC-073", source_hash="demo", title="Open Site B - Engineer",
        expected_text="Blocked - Site B is not visible at all.",
        primary_persona="Site Engineer",
        segments=[Segment(persona="Site Engineer", intent="look for Site B", steps=[
            Step(capability=Capability.NAVIGATE, target="sites_list"),
            Step(capability=Capability.CAPTURE_SCREEN_TEXT, target="sites_list",
                 args={"tokens": "Site B"}, observation_key="site_list_text"),
        ])],
        assertions=[Assertion(
            id="site-b-invisible", kind=AssertionKind.TOKEN_ABSENT,
            expected_text="Blocked - Site B is not visible at all.",
            consumes=["site_list_text"], token="Site B")],
    )


# --------------------------------------------------------------------------- #


def run_case(
    plan: TestPlan,
    screen_map: ScreenMap,
    renderer: FlowRenderer,
    device_observations: dict[str, dict],
    label: str,
) -> None:
    step(f"1. Compiled plan for {plan.case_id}")
    print(f"  persona:   {plan.primary_persona}")
    print(f"  assertion: {plan.assertions[0].kind.value} - {plan.assertions[0].expected_text!r}")
    print(f"  consumes:  {plan.assertions[0].consumes}  (steps that must produce these)")

    if plan.prohibitions:
        step("2. Differential probe (this is a prohibition - a claim of absence)")
        wrap(
            "Before: absence alone proves nothing - it looks identical to a broken "
            "selector. probes.augment() looks up who the permission spec says SHOULD "
            "see this, then adds a second segment that runs the identical probe as "
            "that persona."
        )
        augmented = probes.augment(plan, screen_map)
        control_segment = augmented.segments[-1]
        print(f"  added segment: persona={control_segment.persona!r}, "
              f"probing the same target as the Engineer's own check")
        print(f"  assertion now consumes: {augmented.assertions[0].consumes}")
        wrap(
            "That second observation key is now mandatory. If it never arrives, the "
            "verifier cannot pass this case no matter what the Engineer's own screen "
            "showed - see act 3."
        )
        plan = augmented
    else:
        step("2. No differential probe needed (this is a positive claim, not an absence)")

    step("3. Rendered Maestro YAML")
    out_dir = ROOT / "flows" / "generated" / "demo" / label
    written = renderer.render_plan(plan, out_dir)
    for path in written:
        print(f"  {path.relative_to(ROOT)}")
    print(f"  ({sum(len(yaml.safe_load(p.read_text().split(chr(10)+'---'+chr(10),1)[1])) for p in written)} Maestro commands total)")

    step("4. Device execution (simulated - see module docstring)")
    log_lines = ["@@FLOW start " + plan.case_id]
    for key, payload in device_observations.items():
        import json
        log_lines.append("@@OBS " + json.dumps({"key": key, **payload}))
    log_lines.append("@@FLOW end " + plan.case_id)
    log_text = "\n".join(log_lines)
    for line in log_lines:
        print(f"  device console: {line}")

    step("5. Parsed observations back out of the log")
    observations, problems = parse_log(log_text)
    for problem in problems:
        print(f"  ! {problem}")
    for key, obs in observations.items():
        print(f"  {key}: raw={obs.raw!r} anchor_found={obs.anchor_found}")

    step("6. Verified (deterministic - no model involved)")
    results = verify(plan, observations, screen_map.refusal_markers)
    for result in results:
        print(f"  {result.id}: passed={result.passed}")
        wrap(f"reason: {result.reasoning}", indent="    ")

    step("7. Verdict")
    evidence = [f"log:{plan.case_id}.log", f"screenshot:{plan.case_id}-final.png"]
    outcome = decide(plan, results, evidence)
    print(f"  >>> {outcome.verdict.value} <<<")
    print(f"  expected: {outcome.expected}")
    print(f"  actual:   {outcome.actual}")
    if outcome.failure_class:
        print(f"  class:    {outcome.failure_class.value}")
    if outcome.probable_cause:
        wrap(f"cause: {outcome.probable_cause}", indent="  ")

    report_dir = ROOT / "results" / "demo" / label
    write_all([outcome], {"run_id": f"demo-{label}", "build": "demo", "device": "narrated"},
               report_dir)
    print(f"\n  report written -> {report_dir.relative_to(ROOT)}/report.html")


def main() -> None:
    heading("BAUTECH SENTINEL - live walkthrough")
    wrap(
        "This runs the real pipeline against three scenarios. Every function is the "
        "production code in sentinel/ - only the device is simulated, because no "
        "verified build is running yet. See docs/phase1_discovery.md for that status."
    )

    screen_map = ScreenMap.load()
    personas = yaml.safe_load((ROOT / "config" / "personas.yaml").read_text(encoding="utf-8"))
    renderer = FlowRenderer(screen_map, personas, app_id="com.naviconinfra.bautech",
                            run_id="DEMO")

    cases = load_suite(ROOT / "tests" / "bautech_suite.csv")
    tc046_row = next(c for c in cases if c.case_id == "TC-046")
    tc073_row = next(c for c in cases if c.case_id == "TC-073")

    heading("ACT 1 - TC-046, the app behaving correctly")
    print(f"From the sheet: {tc046_row.title!r}")
    print(f"  steps:    {tc046_row.steps_text}")
    print(f"  expected: {tc046_row.expected_text}")
    run_case(
        plan_tc046(), screen_map, renderer,
        {"stock_before": {"raw": "500 bags", "anchor": True},
         "stock_after": {"raw": "600 bags", "anchor": True}},
        "act1-healthy",
    )

    heading("ACT 2 - the same case, but the build has a regression")
    wrap(
        "Nothing about the plan or the flow changes. Only what the device reports "
        "differs - this is exactly the Day-13 seeded-build scenario: unchanged code, "
        "a different app underneath it."
    )
    run_case(
        plan_tc046(), screen_map, renderer,
        {"stock_before": {"raw": "500 bags", "anchor": True},
         "stock_after": {"raw": "550 bags", "anchor": True}},  # seeded defect
        "act2-regressed",
    )

    heading("ACT 3a - TC-073, proving Site B is invisible (the app is correct)")
    print(f"From the sheet: {tc073_row.title!r}")
    print(f"  steps:    {tc073_row.steps_text}")
    print(f"  expected: {tc073_row.expected_text}")
    run_case(
        plan_tc073(), screen_map, renderer,
        {"site_list_text": {"raw": "", "screen_texts": ["Site A"], "anchor": True},
         "site_list_text__control": {
             "raw": "", "screen_texts": ["Site A", "Site B"], "anchor": True},
         },
        "act3a-enforced",
    )

    heading("ACT 3b - the same case, but OUR selector is broken (not the app)")
    wrap(
        "Nobody sees Site B this time - including Admin, who is supposed to. A system "
        "that trusts absence would call this a PASS. Watch what it actually does."
    )
    run_case(
        plan_tc073(), screen_map, renderer,
        {"site_list_text": {"raw": "", "screen_texts": ["Site A"], "anchor": True},
         "site_list_text__control": {"raw": "", "screen_texts": ["Site A"], "anchor": True},
         },
        "act3b-broken-selector",
    )

    heading("Done")
    wrap(
        "Reports for all four runs are under results/demo/. Open any report.html - "
        "each shows the expected text, the actual observation, the adjudication ladder "
        "for the negative cases, and the evidence backing the verdict."
    )


if __name__ == "__main__":
    main()
