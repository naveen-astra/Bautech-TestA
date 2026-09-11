"""One case, end to end: a compiled intent, a thinking agent, a hard verdict.

This is the seam where the two halves of the system meet, and the division
between them is the whole design:

    WHAT MUST BE ESTABLISHED          compiled once from the sheet row,
                                      cached, auditable, deterministic

    HOW TO GET THERE                  worked out live by the agent, on the
                                      real screen, differently each time if
                                      the app differs

    WHETHER IT HELD                   arithmetic and the prohibition ladder,
                                      over what the agent actually recorded

The agent is never handed a route. It is handed the tester's own sentences
and a list of values it must come back with, and it navigates a real app to
find them. That is what makes case 86 cost nothing: nobody has to write a
path for it, because nobody wrote a path for case 1 either.

Equally, the agent never decides whether it passed. It reports what it saw;
subtraction decides. An agent permitted to both act and grade its own work
will eventually talk itself into being right, and a suite that can do that is
worth less than no suite at all.

WHY THE PATH MAY VARY BUT THE VERDICT MAY NOT

Two runs of a thinking agent can legitimately reach the Materials screen by
different routes. That is not a repeatability problem, because the verdict is
not computed from the route - it is computed from `stock_before` and
`stock_after`. Same observations, same verdict, whatever order the taps came
in. The suite is reproducible at the level that matters and honest about
being alive at the level that does not.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from sentinel.agent import AgentRun, Brain, Observation, run_case
from sentinel.schema import (
    ActionOutcome,
    Assertion,
    AssertionKind,
    CaseResult,
    Feasibility,
    ObservedValue,
    RawTestCase,
    TestPlan,
    Verdict,
)
from sentinel.verdict import blocked_case, decide
from sentinel.verifier import verify


def mission_for(plan: TestPlan) -> dict[str, str]:
    """What the agent must come back with, derived from the plan's assertions.

    Every assertion declares the observations it consumes, so the union of
    those is exactly the evidence the verdict will need. Describing each key
    in the assertion's own words gives the agent enough to know what it is
    looking for without being told where to look.
    """
    wanted: dict[str, str] = {}
    for assertion in plan.assertions:
        for key in assertion.consumes:
            if key in wanted:
                continue
            wanted[key] = _describe(key, assertion)

    # Steps the compiler produced are not a route the agent must follow, but
    # where one names an observation the assertions did not describe well,
    # its target is a better hint than nothing.
    for segment in plan.segments:
        for step in segment.steps:
            key = step.observation_key
            if key and key in wanted and step.target and "(" not in wanted[key]:
                wanted[key] += f" (relates to: {step.target})"
    return wanted


def _describe(key: str, assertion: Assertion) -> str:
    kind = assertion.kind
    if kind is AssertionKind.NUMERIC_DELTA:
        which = "before you act" if key == assertion.consumes[0] else "after you act"
        return f"the number this case turns on, read {which}"
    if kind is AssertionKind.NUMERIC_EQUALS:
        return f"the number that should be {assertion.expected_value}"
    if kind is AssertionKind.CONTROL_ABSENT:
        return (
            f"whether the control {assertion.control!r} is present - look "
            f"properly, then record 'present' or 'absent'"
        )
    if kind is AssertionKind.CONTROL_PRESENT:
        return f"whether the control {assertion.control!r} is present"
    if kind is AssertionKind.TOKEN_ABSENT:
        return (
            f"whether {assertion.token!r} appears anywhere you can reach - "
            f"record 'present' or 'absent'"
        )
    if kind is AssertionKind.ACTION_REJECTED:
        return "attempt the action this case names, then report what the app did"
    return assertion.expected_text


def to_observed(run: AgentRun, plan: TestPlan) -> dict[str, ObservedValue]:
    """Turn what the agent recorded into what the verifier consumes.

    Two fields carry the weight here. `anchor_found` is the agent's own
    confirmation that it reached the screen - without it the prohibition
    ladder refuses to pass anything, which is precisely the intended
    behaviour. `screen_texts` carries the full inventory of what was on
    screen, so an absence claim can be checked against everything that was
    actually visible rather than against one selector's opinion.
    """
    observed: dict[str, ObservedValue] = {}
    captured_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for key, note in run.observations.items():
        observed[key] = ObservedValue(
            key=key,
            raw=note.value,
            screen_texts=note.screen_texts or run.all_screen_text,
            captured_at=captured_at,
            source="ui",
            anchor_found=note.screen_confirmed,
            outcome=_outcome(note),
        )
    return observed


def _outcome(note: Observation) -> ActionOutcome | None:
    if not note.outcome:
        return None
    try:
        return ActionOutcome(note.outcome)
    except ValueError:
        return ActionOutcome.UNKNOWN


def execute(
    case: RawTestCase,
    plan: TestPlan,
    brain: Brain,
    adb: str,
    package: str,
    refusal_markers: list[str] | None = None,
    max_steps: int = 40,
    log=print,
) -> tuple[CaseResult, AgentRun]:
    """Run one case with the agent, then judge it without the agent.

    A case the compiler already judged unautomatable is not attempted at all -
    spending device time proving that a payment gateway is still not present
    helps nobody, and `decide` reports it as BLOCKED with the reason.
    """
    started = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if plan.feasibility is Feasibility.NEEDS_UNAVAILABLE_INTERFACE:
        return decide(plan, [], [], started_at=started_at), AgentRun(case_id=case.case_id)

    agent_case = _AgentView(case, plan)
    run = run_case(
        agent_case, brain, adb=adb, package=package,
        max_steps=max_steps, log=log, mission=mission_for(plan),
    )

    duration = time.monotonic() - started

    # The agent could not work at all. That is our failure, not a verdict on
    # the app, and it must never be reported as one.
    #
    # Deliberately not routed through blocked_case(): that helper is for a
    # case which never reached execution, and says so. This case did reach
    # execution - the agent went and tried, and could not get there. The
    # report has to be able to tell those apart, because "we never ran it"
    # and "we ran it and got stuck at the login screen" are different facts
    # about the run, and the assessment requires every BLOCKED to carry a
    # real, written reason.
    if run.gave_up and not run.observations:
        return CaseResult(
            case_id=plan.case_id,
            persona=plan.primary_persona,
            verdict=Verdict.BLOCKED,
            expected=plan.expected_text or plan.title,
            actual=f"the agent could not carry out this case: {run.give_up_reason}",
            failure_class=_automation_failure(),
            probable_cause=run.give_up_reason,
            evidence=_evidence(run),
            duration_seconds=duration,
            started_at=started_at,
        ), run

    observations = to_observed(run, plan)
    results = verify(plan, observations, refusal_markers)
    result = decide(
        plan, results, _evidence(run),
        duration_seconds=duration, started_at=started_at,
    )
    return result, run


def _automation_failure():
    from sentinel.schema import FailureClass

    return FailureClass.AUTOMATION_FAILURE


def _evidence(run: AgentRun) -> list[str]:
    """What backs this verdict up, in a form a reader can check.

    The trace is evidence in its own right - every action the agent took and
    what came of it - and it is the thing that makes a verdict auditable
    rather than merely asserted.
    """
    evidence: list[str] = []
    for screen, why in run.confirmations:
        evidence.append(f"confirmed on {screen}: {why}")
    for step in run.steps:
        evidence.append(f"step {step.number}: {step.description} -> {step.result}")
    if not evidence:
        evidence.append("the agent recorded no steps at all")
    return evidence


class _AgentView:
    """The case as the agent sees it: the sheet's words, nothing added."""

    def __init__(self, case: RawTestCase, plan: TestPlan) -> None:
        self.case_id = case.case_id
        self.title = case.title
        self.persona = plan.primary_persona
        self.steps = case.steps_text
        self.expected = case.expected_text
