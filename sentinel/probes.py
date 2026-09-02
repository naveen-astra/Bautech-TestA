"""Add the control half of every differential probe.

A prohibition claims something is not available to a persona. On its own that is
unfalsifiable by observation: a control we cannot find and a control that is not
there look identical. What makes the claim testable is running the same probe as
somebody who *should* succeed.

So before rendering, every prohibition that names a control or a token gets a
second segment appended - same probe, different persona - writing into
`<key>__control`. The adjudicator then has both halves and can tell enforcement
from a broken selector.

Who the control persona is comes from the permission spec, not from a table
written here. `config/screen_map.yaml` records which spec action each control
implements; `sentinel/oracle` knows which roles that action is granted to. The
lowest-authority permitted persona is chosen, to keep the comparison as close to
the case under test as possible.

When the spec grants the action to nobody - "no DMs in V1" - there is no control
persona to be had. We add nothing, and the adjudicator reports the case as
BLOCKED with that reason rather than passing it on evidence we do not have.
"""

from __future__ import annotations

from sentinel import oracle
from sentinel.schema import (
    CONTROL_SUFFIX,
    Assertion,
    AssertionKind,
    Capability,
    Segment,
    Step,
    TestPlan,
)
from sentinel.screen_map import ScreenMap


class ProbeReport:
    """What the augmentation did, so a run can explain itself."""

    def __init__(self) -> None:
        self.added: list[tuple[str, str, str]] = []      # (case, assertion, persona)
        self.skipped: list[tuple[str, str, str]] = []    # (case, assertion, why)

    def __str__(self) -> str:
        return (
            f"differential probes: {len(self.added)} added, {len(self.skipped)} not possible"
        )


def _spec_for_assertion(
    assertion: Assertion, plan: TestPlan, screen_map: ScreenMap
) -> tuple[tuple[str, str] | None, str]:
    """The (module, action) governing this prohibition, and how we found it.

    control_absent names a specific tappable control directly. token_absent
    does not - what is being probed is a whole screen's content, so the spec
    comes from the screen the sweep step targets instead. Getting this branch
    wrong is exactly how the mechanism silently no-ops: with no case matching
    a prohibition kind, `_control_persona_for` used to require `assertion.
    control` unconditionally, which token_absent assertions never set, so the
    differential probe was quietly skipped for every "prove X is invisible"
    case - the most safety-critical kind in the suite - without a single test
    catching it.
    """
    if assertion.kind is AssertionKind.CONTROL_ABSENT:
        if not assertion.control:
            return None, "assertion names no control to probe"
        spec = screen_map.spec_for_control(assertion.control)
        if spec is None:
            return None, (
                f"control {assertion.control!r} has no spec mapping in the screen map"
            )
        return spec, f"control {assertion.control!r}"

    if assertion.kind is AssertionKind.TOKEN_ABSENT:
        origin = _producing_step(plan, assertion.consumes[0])
        if origin is None:
            return None, "no step produces the observation this assertion consumes"
        screen = screen_map.screen_of(origin.target)
        if screen is None:
            return None, f"target {origin.target!r} does not resolve to a screen"
        spec = screen_map.spec_for_screen(screen)
        if spec is None:
            return None, (
                f"screen {screen!r} has no spec mapping in the screen map yet, so we do "
                "not know who is permitted to view its content"
            )
        return spec, f"screen {screen!r}"

    return None, f"{assertion.kind} does not use a differential probe"


def _control_persona_for(
    assertion: Assertion, plan: TestPlan, screen_map: ScreenMap
) -> tuple[str | None, str]:
    """Who should legitimately succeed here, and why we think so."""
    spec, source = _spec_for_assertion(assertion, plan, screen_map)
    if spec is None:
        return None, source

    module, action = spec
    try:
        persona = oracle.control_persona(plan.primary_persona, module, action)
    except oracle.OracleError as exc:
        return None, str(exc)

    if persona is None:
        return None, (
            f"the spec grants {module}.{action} to no other persona we can sign in as, "
            "so the selector cannot be validated by comparison"
        )
    return persona, f"{module}.{action} ({source}) is granted to {persona}"


def _producing_step(plan: TestPlan, key: str) -> Step | None:
    """The step that captured an observation, so we can repeat it exactly."""
    for segment in plan.segments:
        for step in segment.steps:
            if step.observation_key == key:
                return step
    return None


def _control_steps(
    plan: TestPlan, assertion: Assertion, key: str, screen_map: ScreenMap
) -> list[Step]:
    """The mirrored probe, writing into the control observation key.

    The probe is a copy of the step that produced the original observation, not
    a fresh one built from the assertion. Anything else risks the two halves
    diverging - a control probe that looked somewhere slightly different would
    make the comparison meaningless, which is the one thing it exists to avoid.

    ACTION_REJECTED gets nothing: it does not rest on absence. We attempt the
    action and observe what came back, so there is no selector to validate.
    """
    if assertion.kind is AssertionKind.ACTION_REJECTED:
        return []

    origin = _producing_step(plan, assertion.consumes[0])
    if origin is None:
        return []

    steps: list[Step] = []

    # Log-in leaves us on the home screen, so the mirror has to navigate to the
    # same place before it looks. Without this the probe reports on whatever
    # screen it happened to land on.
    screen = screen_map.screen_of(origin.target)
    if screen:
        steps.append(
            Step(
                capability=Capability.NAVIGATE,
                target=screen,
                label=f"control probe: go to {screen}",
            )
        )

    steps.append(
        origin.model_copy(
            update={
                "observation_key": key,
                "label": f"control probe: {origin.target}",
            }
        )
    )
    return steps


def augment(
    plan: TestPlan, screen_map: ScreenMap, report: ProbeReport | None = None
) -> TestPlan:
    """Return the plan with control-persona probes attached.

    The original is not modified. Assertions gain the control key in `consumes`,
    which is what makes the evidence mandatory: if the control probe does not
    run, the verifier finds a missing observation and refuses to pass the case.
    """
    report = report if report is not None else ProbeReport()

    if not plan.prohibitions:
        return plan

    by_persona: dict[str, list[Step]] = {}
    assertions: list[Assertion] = []

    for assertion in plan.assertions:
        if not assertion.is_prohibition or assertion.kind is AssertionKind.ACTION_REJECTED:
            assertions.append(assertion)
            continue

        persona, why = _control_persona_for(assertion, plan, screen_map)
        if persona is None:
            report.skipped.append((plan.case_id, assertion.id, why))
            assertions.append(assertion)
            continue

        key = assertion.consumes[0] + CONTROL_SUFFIX
        steps = _control_steps(plan, assertion, key, screen_map)
        if not steps:
            report.skipped.append(
                (plan.case_id, assertion.id, "no step to mirror for this assertion")
            )
            assertions.append(assertion)
            continue

        by_persona.setdefault(persona, []).extend(steps)
        assertions.append(
            assertion.model_copy(update={"consumes": [*assertion.consumes, key]})
        )
        report.added.append((plan.case_id, assertion.id, persona))

    if not by_persona:
        return plan.model_copy(update={"assertions": assertions})

    segments = list(plan.segments)
    for persona, steps in by_persona.items():
        segments.append(
            Segment(
                persona=persona,
                intent=(
                    f"differential probe: confirm the same selectors resolve for "
                    f"{persona}, who the spec permits"
                ),
                steps=steps,
            )
        )

    return plan.model_copy(update={"segments": segments, "assertions": assertions})


def augment_all(
    plans: list[TestPlan], screen_map: ScreenMap
) -> tuple[list[TestPlan], ProbeReport]:
    report = ProbeReport()
    return [augment(plan, screen_map, report) for plan in plans], report
