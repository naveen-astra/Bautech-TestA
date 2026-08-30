#!/usr/bin/env python3
"""Bautech Sentinel - run the regression suite.

    python run.py                        # everything, on a local device
    python run.py --only TC-046,TC-009   # a subset, while developing
    python run.py --backend browserstack --app bautech.apk
    python run.py --dry-run              # plan and render, touch no device

The pipeline, in order:

    parse -> compile -> add differential probes -> render
          -> execute -> observe -> verify -> judge -> report

Two rules govern the failure paths, and they are why this file is longer than a
shell script would be:

*   **Every case gets a verdict.** A case that fails to compile, or never runs,
    or comes back with no observations, still appears in the report as BLOCKED
    with the reason. A missing row is indistinguishable from a case quietly
    skipped, so there are none.

*   **We never report verdicts we did not earn.** If the run produced no
    evidence at all, that is reported as a failed run rather than as 85 results.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml

from sentinel import probes
from sentinel.backends.base import BackendError, RunArtifacts
from sentinel.compiler import CompilerError, PlanCompiler
from sentinel.observation import collect, find_evidence, flow_boundaries
from sentinel.parser import SheetError, load_suite
from sentinel.renderer import FlowRenderer, RenderError
from sentinel.report import write_all
from sentinel.schema import CaseResult, FailureClass, Feasibility, RawTestCase, TestPlan
from sentinel.screen_map import ScreenMap
from sentinel.verdict import blocked_case, decide, summarise
from sentinel.verifier import verify

ROOT = Path(__file__).resolve().parent


def _log(message: str) -> None:
    print(message, flush=True)


def build_backend(args: argparse.Namespace):
    if args.backend == "browserstack":
        from sentinel.backends.browserstack import BrowserStackBackend

        return BrowserStackBackend(
            app_path=args.app,
            devices=args.device.split(",") if args.device else None,
        )
    from sentinel.backends.local import LocalBackend

    return LocalBackend(device=args.device)


def persona_env(personas: dict) -> dict[str, str]:
    """Credentials the flows need, read from the environment.

    Returned as Maestro parameters so nothing secret is ever written into a
    flow file, which matters more than usual here: the flows get zipped and
    uploaded to a third party.
    """
    env: dict[str, str] = {}
    missing: list[str] = []

    for name, config in (personas.get("personas") or {}).items():
        for field in ("identifier_env", "otp_env"):
            variable = config.get(field)
            if not variable:
                continue
            value = os.environ.get(variable)
            if value is None:
                missing.append(f"{variable} (for {name})")
            else:
                env[variable] = value

    relay = (personas.get("defaults") or {}).get("relay_url_env")
    if relay and os.environ.get(relay):
        env[relay] = os.environ[relay]

    if missing:
        _log("  credentials not set: " + "; ".join(missing))
    return env


def compile_plans(
    cases: list[RawTestCase], screen_map: ScreenMap, args: argparse.Namespace
) -> tuple[list[TestPlan], list[CaseResult]]:
    """Compile every case; turn failures into BLOCKED results rather than gaps."""
    compiler = PlanCompiler(screen_map, model=args.model)
    plans: list[TestPlan] = []
    blocked: list[CaseResult] = []

    for case in cases:
        try:
            plans.append(compiler.compile(case))
        except CompilerError as exc:
            blocked.append(
                blocked_case(
                    case.case_id,
                    case.persona_hint,
                    case.expected_text,
                    f"could not be planned: {exc}",
                    FailureClass.AUTOMATION_FAILURE,
                )
            )

    _log(
        f"  compiled {compiler.stats['compiled']} case(s), "
        f"{compiler.stats['cache_hits']} from cache, "
        f"{compiler.stats['repairs']} repair round-trip(s), "
        f"{len(blocked)} failed"
    )
    return plans, blocked


def judge(
    plans: list[TestPlan],
    observations: dict,
    boundaries: dict[str, str],
    screen_map: ScreenMap,
    results_dir: Path,
) -> list[CaseResult]:
    """Turn observations into one verdict per plan."""
    results: list[CaseResult] = []

    for plan in plans:
        evidence = find_evidence(results_dir, plan.case_id) or ["log:console.log"]

        if plan.feasibility is Feasibility.NEEDS_UNAVAILABLE_INTERFACE:
            results.append(decide(plan, [], evidence))
            continue

        assertion_results = verify(plan, observations, screen_map.refusal_markers)
        result = decide(plan, assertion_results, evidence)

        # A flow that started and never finished died mid-way. Say so: the
        # observations alone cannot distinguish that from a short flow.
        if boundaries.get(plan.case_id) == "started" and result.verdict.value != "FAIL":
            result = result.model_copy(
                update={
                    "probable_cause": (
                        result.probable_cause
                        + " The flow started but never reached its end marker, so it "
                        "terminated early."
                    ).strip()
                }
            )
        results.append(result)

    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Bautech regression suite.")
    parser.add_argument("--suite", default=str(ROOT / "tests" / "bautech_suite.csv"))
    parser.add_argument("--backend", choices=("local", "browserstack"), default="local")
    parser.add_argument("--app", help="path to the APK (required for browserstack)")
    parser.add_argument("--app-id", help="android package id, overrides the screen map")
    parser.add_argument("--device", help="device id, or comma-separated cloud devices")
    parser.add_argument("--only", help="comma-separated case ids to run")
    parser.add_argument("--model", default="claude-sonnet-5")
    parser.add_argument("--dry-run", action="store_true", help="plan and render, run nothing")
    parser.add_argument("--timeout", type=int, default=1800, help="seconds for the whole run")
    args = parser.parse_args(argv)

    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    results_dir = ROOT / "results" / run_id
    flow_dir = ROOT / "flows" / "generated" / run_id
    started = time.monotonic()

    _log(f"Bautech Sentinel - run {run_id}")

    # -- configuration ----------------------------------------------------- #
    try:
        screen_map = ScreenMap.load()
        personas = yaml.safe_load((ROOT / "config" / "personas.yaml").read_text(encoding="utf-8"))
        cases = load_suite(args.suite)
    except (SheetError, Exception) as exc:  # config problems are fatal and specific
        _log(f"cannot start: {exc}")
        return 2

    if args.only:
        wanted = {c.strip().upper() for c in args.only.split(",")}
        cases = [c for c in cases if c.case_id in wanted]

    _log(f"  {len(cases)} case(s) from {Path(args.suite).name}")
    _log(f"  {screen_map.coverage()}")
    for warning in screen_map.lint():
        _log(f"  warning: {warning}")

    # -- plan -------------------------------------------------------------- #
    plans, blocked = compile_plans(cases, screen_map, args)
    plans, probe_report = probes.augment_all(plans, screen_map)
    _log(f"  {probe_report}")
    for _, assertion_id, why in probe_report.skipped:
        _log(f"    no control probe for {assertion_id}: {why}")

    # -- render ------------------------------------------------------------ #
    try:
        renderer = FlowRenderer(
            screen_map, personas, app_id=args.app_id, run_id=run_id.replace("-", "")[-8:]
        )
    except RenderError as exc:
        _log(f"cannot render: {exc}")
        return 2

    rendered = 0
    for plan in plans:
        if plan.feasibility is Feasibility.NEEDS_UNAVAILABLE_INTERFACE:
            continue
        try:
            rendered += len(renderer.render_plan(plan, flow_dir))
        except (RenderError, Exception) as exc:
            blocked.append(
                blocked_case(
                    plan.case_id, plan.primary_persona, plan.title,
                    f"could not be rendered: {exc}", FailureClass.AUTOMATION_FAILURE,
                )
            )
    _log(f"  rendered {rendered} flow file(s) -> {flow_dir.relative_to(ROOT)}")

    if args.dry_run:
        _log("dry run: stopping before execution")
        return 0

    # -- execute ----------------------------------------------------------- #
    backend = build_backend(args)
    problems = backend.check()
    if problems:
        _log(f"cannot run on {backend.name}:")
        for problem in problems:
            _log(f"  - {problem}")
        return 2

    _log(f"  executing on {backend.name}...")
    try:
        artifacts = backend.run(flow_dir, results_dir, persona_env(personas), args.timeout)
    except BackendError as exc:
        _log(f"execution failed: {exc}")
        return 2

    _log(f"  finished in {artifacts.duration_seconds:.0f}s (exit {artifacts.exit_code})")
    for url in artifacts.session_urls:
        _log(f"  session: {url}")

    # -- observe ----------------------------------------------------------- #
    observations, issues = collect(artifacts.logs)
    for issue in issues:
        _log(f"  log problem: {issue}")
    _log(f"  recovered {len(observations)} observation(s)")

    if not artifacts.produced_evidence:
        _log(
            "\nThe run produced no logs or screenshots at all, so there is nothing to "
            "judge. Reporting this as a failed run rather than as 85 verdicts."
        )
        return 1

    log_text = "\n".join(
        p.read_text(encoding="utf-8", errors="replace") for p in artifacts.logs if p.exists()
    )
    boundaries = flow_boundaries(log_text)

    # -- judge and report -------------------------------------------------- #
    results = judge(plans, observations, boundaries, screen_map, results_dir) + blocked
    counts = summarise(results)

    meta = {
        "run_id": run_id,
        "build": args.app or str(screen_map.meta.get("app_id") or "unknown"),
        "device": args.device or backend.name,
        "duration_seconds": round(time.monotonic() - started, 1),
    }
    written = write_all(results, meta, results_dir)

    _log("")
    _log(f"{counts['total']} cases: {counts['PASS']} pass, {counts['FAIL']} fail, "
         f"{counts['BLOCKED']} blocked")
    _log(f"  {counts['app_defects']} Bautech defect(s), "
         f"{counts['automation_failures']} automation failure(s)")
    if counts["BLOCKED"] > 10:
        _log(f"  warning: {counts['BLOCKED']} blocked is above the 10 the brief allows")
    _log(f"  report: {written[0].parent.relative_to(ROOT)}")

    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
