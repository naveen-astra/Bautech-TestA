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
from sentinel.junit import failed_case_ids
from sentinel.observation import collect, find_evidence, flow_boundaries
from sentinel.parser import SheetError, load_suite
from sentinel.renderer import FlowRenderer, RenderError
from sentinel.report import write_all
from sentinel.schema import CaseResult, FailureClass, Feasibility, RawTestCase, TestPlan
from sentinel.screen_map import ScreenMap
from sentinel.verdict import blocked_case, decide, summarise
from sentinel.verifier import verify

DEFAULT_MAX_RETRIES = 1

ROOT = Path(__file__).resolve().parent


def load_dotenv(path: Path | None = None) -> None:
    """Read .env into the environment, if it is there.

    Hand-rolled rather than a dependency: the format we need is KEY=value, one
    per line. An already-exported variable wins, so a single override on the
    command line does not mean editing the file.
    """
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


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
    retry_attempt_of: dict[str, int] | None = None,
) -> list[CaseResult]:
    """Turn observations into one verdict per plan."""
    results: list[CaseResult] = []
    retry_attempt_of = retry_attempt_of or {}

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

        # Surfaced in the report itself, not just the console - a verdict
        # that only came together on a retry is a materially different claim
        # than one that worked cleanly the first time, and a reviewer should
        # be able to see that without re-reading the run's console output.
        attempt = retry_attempt_of.get(plan.case_id, 0)
        if attempt > 0:
            result = result.model_copy(
                update={
                    "probable_cause": (
                        result.probable_cause
                        + f" This verdict is from retry {attempt}: the flow's first "
                        "attempt crashed before producing a result, and a bounded "
                        "retry re-ran it from scratch. The observations above are "
                        "from the retry, not the original attempt."
                    ).strip()
                }
            )
        results.append(result)

    return results


def execute_with_bounded_retries(
    backend,
    plans: list[TestPlan],
    renderer: FlowRenderer,
    flow_dir: Path,
    results_dir: Path,
    env: dict[str, str],
    timeout_seconds: int,
    max_retries: int,
) -> tuple[RunArtifacts, dict[str, int]]:
    """Run every flow; re-run only the ones whose flow itself never finished.

    A case that completed and reported a real FAIL is never touched again -
    that observation is the finding, and retrying it would be exactly the
    "retries must not mask a defect" failure mode the plan explicitly rules
    out. Only a case whose flow crashed or errored out before producing a
    verdict - the JUnit-level signal, entirely separate from what it observed
    - is eligible, and only up to `max_retries` times.

    Returns the merged artifacts (logs from every attempt, so a case that
    only succeeded on retry N still has its observations counted) and a map
    of case_id -> which attempt it finally completed on, 0 meaning the first
    try - the transparency the plan asks retries to carry, rather than
    silently reporting a retried case identically to a clean first pass.
    """
    plans_by_id = {p.case_id: p for p in plans}
    attempt_of: dict[str, int] = {p.case_id: 0 for p in plans}

    artifacts = backend.run(flow_dir, results_dir, env, timeout_seconds)
    all_logs = list(artifacts.logs)
    all_screenshots = list(artifacts.screenshots)
    all_notes = list(artifacts.notes)

    junit = artifacts.junit or (results_dir / "report.xml")
    pending = failed_case_ids(junit) & plans_by_id.keys()

    for attempt in range(1, max_retries + 1):
        if not pending:
            break
        _log(f"  retry {attempt}/{max_retries}: re-running {len(pending)} case(s) "
             f"whose flow did not finish: {', '.join(sorted(pending))}")

        retry_flow_dir = flow_dir / f"retry-{attempt}"
        retry_results_dir = results_dir / f"retry-{attempt}"
        for case_id in pending:
            renderer.render_plan(plans_by_id[case_id], retry_flow_dir)

        try:
            retry_artifacts = backend.run(
                retry_flow_dir, retry_results_dir, env, timeout_seconds
            )
        except BackendError as exc:
            _log(f"  retry {attempt} failed to execute at all: {exc}")
            break

        # Appended, not replacing: a case that crashed on try 1 produced
        # incomplete or no observations, and the retry's logs are what
        # supply the real ones when this is parsed - collect() lets later
        # entries for the same key win, which is exactly what is wanted here.
        all_logs += retry_artifacts.logs
        all_screenshots += retry_artifacts.screenshots
        all_notes += retry_artifacts.notes

        retry_junit = retry_artifacts.junit or (retry_results_dir / "report.xml")
        still_failing = failed_case_ids(retry_junit) & pending
        resolved = pending - still_failing
        for case_id in resolved:
            attempt_of[case_id] = attempt
        pending = still_failing

    if pending:
        _log(f"  {len(pending)} case(s) never completed after {max_retries} "
             f"retr{'y' if max_retries == 1 else 'ies'}: {', '.join(sorted(pending))}")

    merged = RunArtifacts(
        logs=all_logs,
        screenshots=all_screenshots,
        junit=artifacts.junit,
        session_urls=artifacts.session_urls,
        exit_code=artifacts.exit_code,
        duration_seconds=artifacts.duration_seconds,
        notes=all_notes,
    )
    return merged, attempt_of


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
    parser.add_argument(
        "--max-retries", type=int, default=DEFAULT_MAX_RETRIES,
        help="times to re-run a case whose flow crashed before finishing "
             "(never a case that finished and reported FAIL - that is a real "
             "observation, not a flake)",
    )
    args = parser.parse_args(argv)
    load_dotenv()

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
        artifacts, retry_attempt_of = execute_with_bounded_retries(
            backend, plans, renderer, flow_dir, results_dir,
            persona_env(personas), args.timeout, args.max_retries,
        )
    except BackendError as exc:
        _log(f"execution failed: {exc}")
        return 2

    retried = {cid: n for cid, n in retry_attempt_of.items() if n > 0}
    if retried:
        _log(f"  {len(retried)} case(s) needed a retry to complete: "
             + ", ".join(f"{cid} (try {n})" for cid, n in sorted(retried.items())))

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
    results = judge(
        plans, observations, boundaries, screen_map, results_dir, retry_attempt_of
    ) + blocked
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
