"""Drive the whole pipeline without a device.

Plan -> Maestro flow -> (simulated device log) -> observations -> verdict.

The only thing faked is the device. The flow is really rendered, the log is
really parsed by the same code that will read `maestro.log`, and the verdict is
really decided by the verifier and adjudicator. So this exercises every seam
between components, which is where integrations usually break.

It runs the same two cases against three different app behaviours:

    healthy    the app does what the sheet says it should
    regressed  a seeded defect - stock moves by 50 when 100 was purchased
    broken     our automation is broken, not the app

The third is the one that matters. A system that reports the seeded regression
but also reports PASS when its own selectors are broken has not earned any of
its green results.

    python tools/roundtrip.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SENTINEL_ALLOW_UNVERIFIED", "1")

import yaml  # noqa: E402

from sentinel.observation import flow_boundaries, parse_log  # noqa: E402
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
from sentinel.report import write_all  # noqa: E402
from sentinel.verdict import decide, summarise  # noqa: E402
from sentinel.verifier import verify  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def build_positive() -> TestPlan:
    """TC-046 shape: read stock, buy 100, read stock again."""
    return TestPlan(
        case_id="TC-046", source_hash="h1", title="Add purchase / stock",
        primary_persona="Site Engineer",
        segments=[Segment(persona="Site Engineer", intent="add a cement purchase", steps=[
            Step(capability=Capability.NAVIGATE, target="material"),
            Step(capability=Capability.READ_VALUE, target="stock_level",
                 observation_key="stock_before"),
            Step(capability=Capability.CREATE_ENTITY, target="material_purchase",
                 args={"item": "SNTL-cement-${RUN_ID}", "quantity": 100}),
            Step(capability=Capability.READ_VALUE, target="stock_level",
                 observation_key="stock_after"),
        ])],
        assertions=[Assertion(
            id="stock-rises", kind=AssertionKind.NUMERIC_DELTA,
            expected_text="Stock increases by the quantity added.",
            consumes=["stock_before", "stock_after"], expected_delta=100)],
    )


def build_negative() -> TestPlan:
    """TC-009 shape: the Engineer must not be offered Add Site."""
    return TestPlan(
        case_id="TC-009", source_hash="h2", title="Create site - Engineer",
        primary_persona="Site Engineer",
        segments=[Segment(persona="Site Engineer", intent="look for Add Site", steps=[
            Step(capability=Capability.NAVIGATE, target="sites_list"),
            Step(capability=Capability.PROBE_CONTROL, target="add_site_button",
                 observation_key="probe"),
        ])],
        assertions=[Assertion(
            id="no-add-site", kind=AssertionKind.CONTROL_ABSENT,
            expected_text="Blocked - no Add Site option for Site Engineer.",
            consumes=["probe"], control="add_site_button")],
    )


def device_log(case_id: str, observations: list[dict]) -> str:
    """What the device would print. Same shape the flows emit."""
    import json

    lines = [
        f"12:00:01.123 [ INFO] maestro: running flow {case_id}",
        f"12:00:01.200 [ INFO] console: @@FLOW start {case_id}",
    ]
    for payload in observations:
        lines.append("12:00:0X.000 [ INFO] console: @@OBS " + json.dumps(payload))
    lines.append(f"12:00:09.900 [ INFO] console: @@FLOW end {case_id}")
    return "\n".join(lines)


SCENARIOS = {
    "healthy": {
        "TC-046": [
            {"key": "stock_before", "raw": "500 bags", "anchor": True},
            {"key": "stock_after", "raw": "600 bags", "anchor": True},
        ],
        "TC-009": [
            {"key": "probe", "raw": "absent", "anchor": True},
            {"key": "probe__control", "raw": "present", "anchor": True},
        ],
    },
    "regressed": {
        "TC-046": [
            {"key": "stock_before", "raw": "500 bags", "anchor": True},
            {"key": "stock_after", "raw": "550 bags", "anchor": True},
        ],
        "TC-009": [
            # The seeded build wrongly offers Add Site to the Engineer.
            {"key": "probe", "raw": "present", "anchor": True},
            {"key": "probe__control", "raw": "present", "anchor": True},
        ],
    },
    "broken": {
        "TC-046": [
            # The stock widget selector no longer matches anything.
            {"key": "stock_before", "raw": "", "anchor": True},
            {"key": "stock_after", "raw": "", "anchor": True},
        ],
        "TC-009": [
            # Neither persona can see the control: our selector is wrong.
            {"key": "probe", "raw": "absent", "anchor": True},
            {"key": "probe__control", "raw": "absent", "anchor": True},
        ],
    },
}

EXPECTED = {
    "healthy":   {"TC-046": ("PASS", None), "TC-009": ("PASS", None)},
    "regressed": {"TC-046": ("FAIL", "application_defect"),
                  "TC-009": ("FAIL", "application_defect")},
    "broken":    {"TC-046": ("BLOCKED", "automation_failure"),
                  "TC-009": ("BLOCKED", "automation_failure")},
}


def main() -> int:
    screen_map = ScreenMap.load()
    personas = yaml.safe_load((ROOT / "config" / "personas.yaml").read_text(encoding="utf-8"))
    renderer = FlowRenderer(screen_map, personas, app_id="com.navicon.bautech", run_id="RT01")

    plans = {p.case_id: p for p in (build_positive(), build_negative())}

    out = ROOT / "flows" / "generated" / "roundtrip"
    written: list[Path] = []
    for plan in plans.values():
        written += renderer.render_plan(plan, out)
    print(f"rendered {len(written)} flow file(s) -> {out.relative_to(ROOT)}")
    for path in written:
        head, body = path.read_text(encoding="utf-8").split("---\n", 1)
        commands = yaml.safe_load(body)
        print(f"  {path.name}: {len(commands)} commands, valid YAML")

    failures = 0
    for scenario, cases in SCENARIOS.items():
        print(f"\n{scenario.upper()}\n{'-' * len(scenario)}")
        results = []
        for case_id, payloads in cases.items():
            plan = plans[case_id]
            log = device_log(case_id, payloads)

            observations, problems = parse_log(log)
            boundaries = flow_boundaries(log)
            for problem in problems:
                print(f"  log problem: {problem}")

            assertion_results = verify(plan, observations, screen_map.refusal_markers)
            evidence = [f"log:{case_id}.log", f"screenshot:{case_id}-0-final.png"]
            result = decide(plan, assertion_results, evidence)
            results.append(result)

            want_verdict, want_class = EXPECTED[scenario][case_id]
            got_class = result.failure_class.value if result.failure_class else None
            ok = result.verdict.value == want_verdict and got_class == want_class
            failures += not ok

            flag = "ok  " if ok else "FAIL"
            print(f"  {flag} {case_id}  {result.verdict.value:<8} {got_class or '-'}")
            print(f"       flow: {boundaries.get(case_id, 'never started')}")
            print(f"       expected: {result.expected}")
            print(f"       actual  : {result.actual}")
            if result.probable_cause:
                print(f"       cause   : {result.probable_cause}")

        counts = summarise(results)
        print(f"  summary: {counts['PASS']} pass, {counts['FAIL']} fail, "
              f"{counts['BLOCKED']} blocked")

        written = write_all(
            results,
            {"run_id": f"roundtrip-{scenario}", "build": scenario, "device": "simulated"},
            ROOT / "results" / f"roundtrip-{scenario}",
        )
        print(f"  report : {', '.join(w.name for w in written)}")

    print("\n" + "=" * 62)
    if failures:
        print(f"{failures} scenario expectation(s) not met.")
        return 1
    print("Pipeline round trip intact across all three app behaviours.")
    print("The seeded regression was caught; broken automation did not pass.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
