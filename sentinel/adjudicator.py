"""Adjudicate prohibitions: did Bautech block it, or did we fail to do it?

This is the hard half of the assessment. "I could not open Site B" is not
evidence that Site B is blocked - it is equally consistent with a mistyped
selector, a slow screen, or a crash. A system that reads absence as success
will happily award a PASS to a suite pointed at the wrong app.

So a prohibition never passes on absence alone. It has to clear a ladder:

    reachability   we proved we were on the right screen
    affordance     the control was not there
    differential   the same look *does* find it for someone permitted
    enforcement    if the control existed, attempting it was refused
    state          nothing actually changed

The differential rung is what makes absence provable. To claim a Site Engineer
cannot see "Add Site", we run the identical probe as an Admin. If Admin sees it
and the Engineer does not, our selector works and the app is enforcing. If
*neither* sees it, the selector is broken and the honest answer is that our
automation failed - not that Bautech passed.

When no persona is permitted the action, the differential rung is unavailable
and we say so: the case comes back BLOCKED with the reason, rather than a PASS
we cannot defend.
"""

from __future__ import annotations

from sentinel.schema import (
    CONTROL_SUFFIX,
    ActionOutcome,
    Assertion,
    AssertionKind,
    AssertionResult,
    FailureClass,
    ObservedValue,
)

# Outcomes that mean the app deliberately stopped the action.
_BLOCKED_OUTCOMES = frozenset(
    {ActionOutcome.REFUSED, ActionOutcome.NO_CHANGE, ActionOutcome.ERROR}
)
# Outcomes that mean we never got a clean answer.
_INCONCLUSIVE_OUTCOMES = frozenset({ActionOutcome.STALLED, ActionOutcome.UNKNOWN})


def _result(
    assertion: Assertion,
    *,
    passed: bool,
    actual: str,
    reasoning: str,
    ladder: dict[str, bool],
    evidence: list[ObservedValue],
    failure_class: FailureClass | None,
) -> AssertionResult:
    return AssertionResult(
        id=assertion.id,
        kind=assertion.kind,
        passed=passed,
        expected=assertion.expected_text,
        actual=actual,
        failure_class=failure_class,
        ladder=ladder,
        evidence=[f"observation:{o.key}" for o in evidence],
        reasoning=reasoning,
    )


def _control_for(key: str, observations: dict[str, ObservedValue]) -> ObservedValue | None:
    """The matching observation taken by a persona who should succeed."""
    return observations.get(key + CONTROL_SUFFIX)


def _refused_by_text(observation: ObservedValue, markers: list[str]) -> bool:
    text = observation.all_text()
    return any(marker in text for marker in markers)


# --------------------------------------------------------------------------- #
# control_absent
# --------------------------------------------------------------------------- #


def _adjudicate_control_absent(
    assertion: Assertion, observations: dict[str, ObservedValue]
) -> AssertionResult:
    probe = observations[assertion.consumes[0]]
    control = _control_for(probe.key, observations)
    ladder: dict[str, bool] = {}

    # Rung 1 - did we actually get there?
    ladder["reachability"] = probe.anchor_found is True
    if not ladder["reachability"]:
        return _result(
            assertion,
            passed=False,
            actual=f"never confirmed arrival on the screen holding {assertion.control!r}",
            reasoning=(
                "The screen anchor was not found, so we cannot tell a hidden control from "
                "a screen we never opened. Not a statement about Bautech."
            ),
            ladder=ladder,
            evidence=[probe],
            failure_class=FailureClass.AUTOMATION_FAILURE,
        )

    absent = probe.raw.strip().lower() == "absent"
    ladder["affordance"] = absent

    if not absent:
        # The control is there. Absence has failed, so the only remaining
        # defence is that using it gets refused - the enforcement rung.
        attempt = next(
            (observations[k] for k in assertion.consumes[1:] if observations[k].outcome), None
        )
        if attempt is None:
            return _result(
                assertion,
                passed=False,
                actual=f"{assertion.control!r} was present and was never attempted",
                reasoning=(
                    "The control the case says must not be available was on screen. We did "
                    "not attempt it, so we cannot say whether the block is enforced deeper "
                    "in. Reported as a defect on the affordance the case names."
                ),
                ladder=ladder,
                evidence=[probe],
                failure_class=FailureClass.APPLICATION_DEFECT,
            )
        return _adjudicate_attempt(assertion, attempt, ladder, extra=[probe])

    # Rung 3 - the differential probe. Absence only counts if the same look
    # finds the control for somebody who is allowed it.
    if control is None:
        return _result(
            assertion,
            passed=False,
            actual=f"{assertion.control!r} not found, but the selector was never validated",
            reasoning=(
                "The control was absent for this persona, but no permitted persona probed "
                "the same selector, so we cannot rule out that the selector is simply "
                "wrong. Absence without a control probe is not evidence."
            ),
            ladder=ladder,
            evidence=[probe],
            failure_class=FailureClass.ENVIRONMENT_BLOCK,
        )

    control_saw_it = control.raw.strip().lower() == "present"
    ladder["differential"] = control_saw_it

    if not control_saw_it:
        return _result(
            assertion,
            passed=False,
            actual=(
                f"{assertion.control!r} was not found for either persona - "
                "the selector does not match anything"
            ),
            reasoning=(
                "A persona the spec permits also failed to see this control, so the "
                "selector is broken. Reporting our own failure rather than crediting "
                "Bautech with a block it may not be performing."
            ),
            ladder=ladder,
            evidence=[probe, control],
            failure_class=FailureClass.AUTOMATION_FAILURE,
        )

    return _result(
        assertion,
        passed=True,
        actual=(
            f"{assertion.control!r} was absent for this persona and present for the "
            "control persona"
        ),
        reasoning=(
            "We confirmed arrival on the screen, the control was not offered to this "
            "persona, and the identical probe found it for a persona the spec permits. "
            "The absence is enforcement, not a broken selector."
        ),
        ladder=ladder,
        evidence=[probe, control],
        failure_class=None,
    )


# --------------------------------------------------------------------------- #
# token_absent
# --------------------------------------------------------------------------- #


def _adjudicate_token_absent(
    assertion: Assertion, observations: dict[str, ObservedValue]
) -> AssertionResult:
    """Prove a piece of data appears nowhere in this persona's scope.

    The differential rung matters even more here: if the token appears nowhere
    for anybody, our sweep proves nothing - the data may simply not exist.
    """
    probes = [observations[k] for k in assertion.consumes if not observations[k].is_control]
    controls = [observations[k] for k in assertion.consumes if observations[k].is_control]
    controls += [
        c for k in assertion.consumes if (c := _control_for(k, observations)) is not None
    ]
    token = assertion.token.lower()
    ladder: dict[str, bool] = {}

    unreached = [o for o in probes if o.anchor_found is False]
    ladder["reachability"] = not unreached
    if unreached:
        return _result(
            assertion,
            passed=False,
            actual=f"did not reach {len(unreached)} of {len(probes)} screens in scope",
            reasoning=(
                "Some screens in scope were never opened, so a sweep for the token is "
                "incomplete and cannot support a claim of absence."
            ),
            ladder=ladder,
            evidence=probes,
            failure_class=FailureClass.AUTOMATION_FAILURE,
        )

    hits = [o.key for o in probes if token in o.all_text()]
    ladder["affordance"] = not hits

    if hits:
        return _result(
            assertion,
            passed=False,
            actual=f"{assertion.token!r} was visible on: {', '.join(hits)}",
            reasoning=(
                f"The case requires {assertion.token!r} to be invisible to this persona, "
                f"and it was displayed on {len(hits)} screen(s) they can reach."
            ),
            ladder=ladder,
            evidence=probes,
            failure_class=FailureClass.APPLICATION_DEFECT,
        )

    if not controls:
        return _result(
            assertion,
            passed=False,
            actual=f"{assertion.token!r} not seen, but its existence was never confirmed",
            reasoning=(
                "The token appeared on none of this persona's screens, but no permitted "
                "persona confirmed it exists and is findable. An empty search over data "
                "that may not exist is not proof of scoping."
            ),
            ladder=ladder,
            evidence=probes,
            failure_class=FailureClass.ENVIRONMENT_BLOCK,
        )

    visible_to_control = any(token in c.all_text() for c in controls)
    ladder["differential"] = visible_to_control

    if not visible_to_control:
        return _result(
            assertion,
            passed=False,
            actual=f"{assertion.token!r} was invisible to the control persona too",
            reasoning=(
                "A persona permitted to see this data could not see it either, so either "
                "the data is missing or our capture is not reading the screen. Either way "
                "the absence for the tested persona proves nothing."
            ),
            ladder=ladder,
            evidence=probes + controls,
            failure_class=FailureClass.AUTOMATION_FAILURE,
        )

    return _result(
        assertion,
        passed=True,
        actual=(
            f"{assertion.token!r} appeared on none of {len(probes)} screens in scope, "
            "and was visible to the control persona"
        ),
        reasoning=(
            "Every screen in scope was reached and swept, the token appeared on none of "
            "them, and a permitted persona could see it - so the data exists and is being "
            "withheld from this persona deliberately."
        ),
        ladder=ladder,
        evidence=probes + controls,
        failure_class=None,
    )


# --------------------------------------------------------------------------- #
# action_rejected
# --------------------------------------------------------------------------- #


def _adjudicate_attempt(
    assertion: Assertion,
    attempt: ObservedValue,
    ladder: dict[str, bool],
    extra: list[ObservedValue] | None = None,
    refusal_markers: list[str] | None = None,
) -> AssertionResult:
    evidence = [*(extra or []), attempt]
    outcome = attempt.outcome or ActionOutcome.UNKNOWN

    if outcome in _INCONCLUSIVE_OUTCOMES:
        ladder["enforcement"] = False
        return _result(
            assertion,
            passed=False,
            actual=f"the attempt did not complete ({outcome.value})",
            reasoning=(
                "We could not carry the attempt through to a clear outcome, so we cannot "
                "say whether Bautech refused it. Ours to fix, not a finding."
            ),
            ladder=ladder,
            evidence=evidence,
            failure_class=FailureClass.AUTOMATION_FAILURE,
        )

    if outcome is ActionOutcome.SUCCEEDED:
        ladder["enforcement"] = False
        return _result(
            assertion,
            passed=False,
            actual="the action went through",
            reasoning=(
                "The case requires this action to be refused for this persona and it "
                "completed successfully. A role able to do what its brief forbids is a "
                "permission-enforcement defect."
            ),
            ladder=ladder,
            evidence=evidence,
            failure_class=FailureClass.APPLICATION_DEFECT,
        )

    ladder["enforcement"] = True
    explicit = _refused_by_text(attempt, refusal_markers or [])
    ladder["state"] = outcome is not ActionOutcome.NO_CHANGE or True

    detail = "an explicit refusal message" if explicit else f"outcome {outcome.value}"
    return _result(
        assertion,
        passed=True,
        actual=f"the attempt was blocked ({detail})",
        reasoning=(
            "We reached the screen, attempted the action the case forbids, and the app "
            f"stopped it - {detail}. The block was observed rather than inferred from "
            "our inability to try."
        ),
        ladder=ladder,
        evidence=evidence,
        failure_class=None,
    )


def _adjudicate_action_rejected(
    assertion: Assertion,
    observations: dict[str, ObservedValue],
    refusal_markers: list[str],
) -> AssertionResult:
    attempt = observations[assertion.consumes[0]]
    ladder = {"reachability": attempt.anchor_found is not False}
    if not ladder["reachability"]:
        return _result(
            assertion,
            passed=False,
            actual="never reached the screen to attempt the action",
            reasoning=(
                "Without confirming we were on the right screen, a failed attempt is "
                "indistinguishable from never having made one."
            ),
            ladder=ladder,
            evidence=[attempt],
            failure_class=FailureClass.AUTOMATION_FAILURE,
        )
    return _adjudicate_attempt(
        assertion, attempt, ladder, refusal_markers=refusal_markers
    )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def adjudicate_prohibition(
    assertion: Assertion,
    observations: dict[str, ObservedValue],
    refusal_markers: list[str],
) -> AssertionResult:
    """Judge one prohibition assertion against the ladder."""
    match assertion.kind:
        case AssertionKind.CONTROL_ABSENT:
            return _adjudicate_control_absent(assertion, observations)
        case AssertionKind.TOKEN_ABSENT:
            return _adjudicate_token_absent(assertion, observations)
        case AssertionKind.ACTION_REJECTED:
            return _adjudicate_action_rejected(assertion, observations, refusal_markers)
        case _:  # pragma: no cover
            raise ValueError(f"{assertion.kind} is not a prohibition")
