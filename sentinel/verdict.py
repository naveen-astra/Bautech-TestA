"""Combine assertion results into one verdict per case.

Every case gets exactly one of PASS, FAIL or BLOCKED - no blanks, including
cases that never ran, failed to compile, or turned out to need an interface we
do not have. A missing row in the report is indistinguishable from a case
quietly skipped, so there are none.

PRECEDENCE

    FAIL     something the app did was wrong, and we saw it cleanly
    BLOCKED  we could not get a trustworthy answer
    PASS     every claim held, on evidence

FAIL outranks BLOCKED deliberately. A defect we observed properly is a real
finding, and it does not stop being one because a second assertion in the same
case was inconclusive. The report still says which parts were inconclusive, so
nothing is hidden by the promotion.

The failure class travels separately from the verdict, because the question the
defect list has to answer is not "did it pass" but "was this Bautech's fault or
ours". A BLOCKED caused by our broken selector and a BLOCKED caused by a device
losing network are the same verdict and completely different problems.
"""

from __future__ import annotations

from sentinel.schema import (
    AssertionResult,
    CaseResult,
    FailureClass,
    Feasibility,
    TestPlan,
    Verdict,
)

# Failure classes that mean "we did not get a trustworthy answer", in the order
# we would rather report them.
_INCONCLUSIVE = (
    FailureClass.MISSING_INTERFACE,
    FailureClass.ENVIRONMENT_BLOCK,
    FailureClass.AUTOMATION_FAILURE,
)


def _expected_summary(plan: TestPlan) -> str:
    return (
        " ".join(a.expected_text for a in plan.assertions)
        or plan.expected_text
        or plan.title
    )


def _probable_cause(results: list[AssertionResult]) -> str:
    """Why this case did not pass, in the words of the assertion that failed."""
    for result in results:
        if not result.passed and result.reasoning:
            return result.reasoning
    return ""


def decide(
    plan: TestPlan,
    results: list[AssertionResult],
    evidence: list[str],
    *,
    duration_seconds: float = 0.0,
    started_at: str = "",
) -> CaseResult:
    """Fold assertion results into the case verdict."""
    persona = " -> ".join(plan.personas) if plan.is_cross_persona else plan.primary_persona

    # A case the compiler judged unautomatable never ran. Say so plainly.
    if plan.feasibility is Feasibility.NEEDS_UNAVAILABLE_INTERFACE:
        return CaseResult(
            case_id=plan.case_id,
            persona=persona,
            verdict=Verdict.BLOCKED,
            expected=_expected_summary(plan),
            actual="not attempted - no interface available to observe this",
            failure_class=FailureClass.MISSING_INTERFACE,
            probable_cause=plan.feasibility_reason,
            assertions=results,
            evidence=evidence or ["log:compile"],
            duration_seconds=duration_seconds,
            started_at=started_at,
        )

    if not results:
        return CaseResult(
            case_id=plan.case_id,
            persona=persona,
            verdict=Verdict.BLOCKED,
            expected=_expected_summary(plan),
            actual="no assertion produced a result",
            failure_class=FailureClass.AUTOMATION_FAILURE,
            probable_cause="The case ran but nothing was judged, which is a defect in us.",
            assertions=results,
            evidence=evidence or ["log:execution"],
            duration_seconds=duration_seconds,
            started_at=started_at,
        )

    failed = [r for r in results if not r.passed]

    if not failed:
        return CaseResult(
            case_id=plan.case_id,
            persona=persona,
            verdict=Verdict.PASS,
            expected=_expected_summary(plan),
            actual="; ".join(r.actual for r in results),
            assertions=results,
            evidence=evidence,
            duration_seconds=duration_seconds,
            started_at=started_at,
        )

    defects = [r for r in failed if r.failure_class is FailureClass.APPLICATION_DEFECT]

    if defects:
        note = ""
        inconclusive = [r for r in failed if r.failure_class in _INCONCLUSIVE]
        if inconclusive:
            note = (
                f" ({len(inconclusive)} further assertion(s) were inconclusive and are "
                "listed separately)"
            )
        return CaseResult(
            case_id=plan.case_id,
            persona=persona,
            verdict=Verdict.FAIL,
            expected=_expected_summary(plan),
            actual="; ".join(r.actual for r in defects) + note,
            failure_class=FailureClass.APPLICATION_DEFECT,
            probable_cause=_probable_cause(defects),
            assertions=results,
            evidence=evidence,
            duration_seconds=duration_seconds,
            started_at=started_at,
        )

    # Nothing was cleanly wrong with the app; we simply could not tell.
    worst = next(
        (cls for cls in _INCONCLUSIVE if any(r.failure_class is cls for r in failed)),
        FailureClass.AUTOMATION_FAILURE,
    )
    return CaseResult(
        case_id=plan.case_id,
        persona=persona,
        verdict=Verdict.BLOCKED,
        expected=_expected_summary(plan),
        actual="; ".join(r.actual for r in failed),
        failure_class=worst,
        probable_cause=_probable_cause(failed),
        assertions=results,
        evidence=evidence,
        duration_seconds=duration_seconds,
        started_at=started_at,
    )


def blocked_case(
    case_id: str,
    persona: str,
    expected: str,
    reason: str,
    failure_class: FailureClass = FailureClass.AUTOMATION_FAILURE,
    evidence: list[str] | None = None,
) -> CaseResult:
    """A verdict for a case that never got as far as producing assertions.

    Used when compilation failed or a flow never ran. It exists so that such a
    case appears in the report with a stated reason instead of going missing.
    """
    return CaseResult(
        case_id=case_id,
        persona=persona or "unknown",
        verdict=Verdict.BLOCKED,
        expected=expected,
        actual="the case did not reach execution",
        failure_class=failure_class,
        probable_cause=reason,
        evidence=evidence or ["log:orchestrator"],
    )


def summarise(results: list[CaseResult]) -> dict[str, int]:
    """Counts for the run banner and the report header."""
    summary = {v.value: 0 for v in Verdict}
    for result in results:
        summary[result.verdict.value] += 1
    summary["total"] = len(results)
    summary["app_defects"] = sum(
        1 for r in results if r.failure_class is FailureClass.APPLICATION_DEFECT
    )
    summary["automation_failures"] = sum(
        1 for r in results if r.failure_class is FailureClass.AUTOMATION_FAILURE
    )
    return summary
