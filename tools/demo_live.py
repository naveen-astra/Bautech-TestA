"""The full pipeline, for real, against the phone that is actually running
Bautech right now - logged in, with a real populated company.

Unlike tools/demo.py (simulated device log) and tools/demo_login.py (login
only), this runs a real TestPlan through the real LocalBackend against a real
device: render -> execute -> observation.collect() parses the real
maestro.log -> verify -> decide -> a real report.html with real screenshots.

Deliberately reads state rather than acting on it (no create/edit/delete),
since this is someone's live logged-in session, not a disposable test
company - the whole point is to show the pipeline works, not to leave a mess
behind on an account we do not control.

    python tools/demo_live.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SENTINEL_ALLOW_UNVERIFIED", "1")

from sentinel.backends.local import LocalBackend  # noqa: E402
from sentinel.observation import collect, flow_boundaries  # noqa: E402
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


def build_plan() -> TestPlan:
    """Sweep the real home screen for real content, no login, no writes.

    Uses only the `home` anchor - the one screen-map entry confirmed against
    this exact device today. Everything else in the map is still a guess and
    stays out of this demo on purpose.
    """
    return TestPlan(
        case_id="LIVE-01", source_hash="live", title="Home screen shows real site data",
        expected_text="The Sites home list shows real projects with real figures.",
        primary_persona="Owner",
        segments=[Segment(persona="Owner", intent="read what is actually on screen", steps=[
            Step(
                capability=Capability.CAPTURE_SCREEN_TEXT, target="home",
                args={"tokens": "Aqua Line|Total expense: ₹74,113|New Site"},
                observation_key="home_text",
            ),
        ])],
        assertions=[Assertion(
            id="real-data-visible", kind=AssertionKind.TEXT_CONTAINS,
            expected_text="A real site and its real expense figure are on screen.",
            consumes=["home_text"], expected_value="Aqua Line")],
    )


def main() -> int:
    screen_map = ScreenMap.load()
    personas_path = ROOT / "config" / "personas.yaml"
    import yaml
    personas = yaml.safe_load(personas_path.read_text(encoding="utf-8"))

    renderer = FlowRenderer(screen_map, personas, app_id=screen_map.meta["app_id"],
                            run_id="LIVE01")
    plan = build_plan()

    flow_dir = ROOT / "flows" / "generated" / "live_demo"
    results_dir = ROOT / "results" / "live_demo"

    # render_plan/render_segment always inject a login unless the plan has an
    # explicit LOGIN step, which would try to tap a "Phone" tab that does not
    # exist on an already-logged-in screen. So this calls the real per-step
    # renderer (render_step - the actual capability dispatch, unmodified)
    # directly and skips only the login wrapper, since the session this demo
    # runs against is already live.
    import yaml as _yaml

    segment = plan.segments[0]
    commands: list = [
        {"evalScript": "${console.log('@@FLOW start " + plan.case_id + "')}"},
        {"launchApp": {"appId": screen_map.meta["app_id"], "clearState": False}},
    ]
    for step in segment.steps:
        commands += renderer.render_step(step)
    commands.append({"takeScreenshot": f"{plan.case_id}-final"})
    commands.append({"evalScript": "${console.log('@@FLOW end " + plan.case_id + "')}"})

    flow_dir.mkdir(parents=True, exist_ok=True)
    flow_path = flow_dir / f"{plan.case_id}-0-owner.yaml"
    header = {"appId": screen_map.meta["app_id"], "name": plan.case_id,
              "properties": {"testCaseId": plan.case_id}}
    flow_path.write_text(
        _yaml.safe_dump(header, sort_keys=False) + "---\n"
        + _yaml.safe_dump(commands, sort_keys=False),
        encoding="utf-8",
    )
    written = [flow_path]
    print(f"rendered {len(written)} real flow file(s):")
    for path in written:
        print(f"  {path.relative_to(ROOT)}")
        print("  " + "\n  ".join(path.read_text(encoding="utf-8").splitlines()))

    print("\nexecuting against the connected device...")
    backend = LocalBackend()
    problems = backend.check()
    if problems:
        print("cannot run:", problems)
        return 1

    artifacts = backend.run(flow_dir, results_dir, env={}, timeout_seconds=120)
    print(f"finished in {artifacts.duration_seconds:.0f}s, exit code {artifacts.exit_code}")

    observations, issues = collect(artifacts.logs)
    for issue in issues:
        print(f"  log issue: {issue}")
    print(f"recovered {len(observations)} real observation(s) from the device:")
    for key, obs in observations.items():
        print(f"  {key}: anchor_found={obs.anchor_found} screen_texts={obs.screen_texts}")

    log_text = "\n".join(
        p.read_text(encoding="utf-8", errors="replace") for p in artifacts.logs if p.exists()
    )
    boundaries = flow_boundaries(log_text)
    print(f"flow boundary: {boundaries.get(plan.case_id, 'never started')}")

    results = verify(plan, observations, screen_map.refusal_markers)
    evidence = [f"screenshot:{p.name}" for p in artifacts.screenshots] or ["log:console.log"]
    outcome = decide(plan, results, evidence)

    print(f"\n>>> {outcome.verdict.value} <<<")
    print(f"expected: {outcome.expected}")
    print(f"actual:   {outcome.actual}")
    if outcome.probable_cause:
        print(f"cause:    {outcome.probable_cause}")
    print(f"evidence: {outcome.evidence}")

    written_report = write_all(
        [outcome],
        {"run_id": "live-demo", "build": "app-debug.apk (real device)",
         "device": "Samsung Galaxy A22 (RZ8R80CE9VV)"},
        results_dir,
    )
    print(f"\nreport written -> {written_report[0].parent.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
