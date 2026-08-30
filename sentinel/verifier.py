"""Compare what the app did against what the case promised.

Everything here is deterministic arithmetic and string work. No model is
consulted, because none is needed: "600 - 500 == 100" does not benefit from
being reasoned about, and a system that asks an LLM to do subtraction has put
a source of hallucination directly into its verdicts.

The one rule that shapes the whole module: **a missing observation is never a
pass.** If an assertion cannot find the evidence it declared it would consume,
we did not see enough to judge, and the result is an automation failure. This
is the difference between "the app behaved" and "we failed to look".

Prohibition assertions are handed to `adjudicator`, which applies the fuller
ladder they require.
"""

from __future__ import annotations

import re

from sentinel.schema import (
    Assertion,
    AssertionKind,
    AssertionResult,
    FailureClass,
    ObservedValue,
    TestPlan,
)

# Numbers as Bautech renders them: "600", "1,300", "Rs 1,300.50", "600 bags".
_NUMBER = re.compile(r"-?\d[\d,]*\.?\d*")


def parse_number(raw: str) -> float | None:
    """Pull a number out of a UI string, or None if there is not exactly one.

    Deliberately strict about ambiguity. "Stock: 600" parses; "500 of 600" does
    not, because guessing which number was meant is how a verifier starts
    inventing results.
    """
    if raw is None:
        return None
    matches = _NUMBER.findall(raw.replace("₹", " ").strip())
    if len(matches) != 1:
        return None
    try:
        return float(matches[0].replace(",", ""))
    except ValueError:
        return None


def numeric_of(observation: ObservedValue) -> float | None:
    """Prefer a number the observer already parsed; else read the raw text."""
    return observation.numeric if observation.numeric is not None else parse_number(observation.raw)


def _missing(assertion: Assertion, keys: list[str]) -> AssertionResult:
    return AssertionResult(
        id=assertion.id,
        kind=assertion.kind,
        passed=False,
        expected=assertion.expected_text,
        actual=f"never captured: {', '.join(keys)}",
        failure_class=FailureClass.AUTOMATION_FAILURE,
        reasoning=(
            "The observation this assertion depends on was not captured, so the app was "
            "not actually checked. This is our failure to look, not a statement about "
            "Bautech."
        ),
    )


def _unreachable(assertion: Assertion, key: str) -> AssertionResult:
    return AssertionResult(
        id=assertion.id,
        kind=assertion.kind,
        passed=False,
        expected=assertion.expected_text,
        actual=f"never reached the screen for {key}",
        failure_class=FailureClass.AUTOMATION_FAILURE,
        ladder={"reachability": False},
        reasoning=(
            "The anchor proving we were on the right screen was not found, so anything "
            "read there is meaningless."
        ),
    )


def _evidence(observations: list[ObservedValue]) -> list[str]:
    return [f"observation:{o.key}" for o in observations]


# --------------------------------------------------------------------------- #
# Deterministic checks, one per positive assertion kind
# --------------------------------------------------------------------------- #


def _check_numeric_delta(
    assertion: Assertion, before: ObservedValue, after: ObservedValue
) -> AssertionResult:
    lhs, rhs = numeric_of(before), numeric_of(after)
    if lhs is None or rhs is None:
        unreadable = before.key if lhs is None else after.key
        raw = before.raw if lhs is None else after.raw
        return AssertionResult(
            id=assertion.id,
            kind=assertion.kind,
            passed=False,
            expected=assertion.expected_text,
            actual=f"could not read a number from {unreadable}: {raw!r}",
            failure_class=FailureClass.AUTOMATION_FAILURE,
            evidence=_evidence([before, after]),
            reasoning="A value we needed to do arithmetic on did not parse as a number.",
        )

    actual_delta = rhs - lhs
    expected_delta = float(assertion.expected_delta or 0.0)
    passed = abs(actual_delta - expected_delta) < 1e-6
    return AssertionResult(
        id=assertion.id,
        kind=assertion.kind,
        passed=passed,
        expected=f"{assertion.expected_text} (delta {expected_delta:+g})",
        actual=f"{lhs:g} -> {rhs:g} (delta {actual_delta:+g})",
        failure_class=None if passed else FailureClass.APPLICATION_DEFECT,
        evidence=_evidence([before, after]),
        reasoning=(
            f"Read {lhs:g} before the action and {rhs:g} after; the case requires a change "
            f"of {expected_delta:+g} and the app moved by {actual_delta:+g}."
        ),
    )


def _check_numeric_equals(assertion: Assertion, observation: ObservedValue) -> AssertionResult:
    actual = numeric_of(observation)
    expected = assertion.expected_value
    if actual is None:
        return AssertionResult(
            id=assertion.id,
            kind=assertion.kind,
            passed=False,
            expected=assertion.expected_text,
            actual=f"could not read a number from {observation.raw!r}",
            failure_class=FailureClass.AUTOMATION_FAILURE,
            evidence=_evidence([observation]),
        )
    passed = expected is not None and abs(actual - float(expected)) < 1e-6
    return AssertionResult(
        id=assertion.id,
        kind=assertion.kind,
        passed=passed,
        expected=f"{assertion.expected_text} ({expected})",
        actual=f"{actual:g}",
        failure_class=None if passed else FailureClass.APPLICATION_DEFECT,
        evidence=_evidence([observation]),
    )


def _check_text_contains(
    assertion: Assertion, observations: list[ObservedValue]
) -> AssertionResult:
    needle = str(assertion.expected_value or assertion.token or "").lower().strip()
    haystack = " \n ".join(o.all_text() for o in observations)
    passed = bool(needle) and needle in haystack
    return AssertionResult(
        id=assertion.id,
        kind=assertion.kind,
        passed=passed,
        expected=f"{assertion.expected_text} (text containing {needle!r})",
        actual=("found" if passed else "not found") + f" across {len(observations)} capture(s)",
        failure_class=None if passed else FailureClass.APPLICATION_DEFECT,
        evidence=_evidence(observations),
    )


def _check_control_present(
    assertion: Assertion, observation: ObservedValue
) -> AssertionResult:
    if observation.anchor_found is False:
        return _unreachable(assertion, observation.key)
    passed = observation.raw.strip().lower() == "present"
    return AssertionResult(
        id=assertion.id,
        kind=assertion.kind,
        passed=passed,
        expected=f"{assertion.expected_text} ({assertion.control} should be available)",
        actual=f"{assertion.control} was {observation.raw}",
        failure_class=None if passed else FailureClass.APPLICATION_DEFECT,
        evidence=_evidence([observation]),
        ladder={"reachability": True},
    )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def verify(
    plan: TestPlan,
    observations: dict[str, ObservedValue],
    refusal_markers: list[str] | None = None,
) -> list[AssertionResult]:
    """Judge every assertion in a plan against what came back from the device."""
    from sentinel.adjudicator import adjudicate_prohibition

    results: list[AssertionResult] = []

    for assertion in plan.assertions:
        missing = [key for key in assertion.consumes if key not in observations]
        if missing:
            results.append(_missing(assertion, missing))
            continue

        consumed = [observations[key] for key in assertion.consumes]

        # A prohibition needs more than a comparison; hand it to the ladder.
        if assertion.is_prohibition:
            results.append(
                adjudicate_prohibition(
                    assertion, observations, refusal_markers or []
                )
            )
            continue

        # Any positive check still requires that we got where we were going.
        unreached = next((o for o in consumed if o.anchor_found is False), None)
        if unreached is not None:
            results.append(_unreachable(assertion, unreached.key))
            continue

        match assertion.kind:
            case AssertionKind.NUMERIC_DELTA:
                results.append(_check_numeric_delta(assertion, consumed[0], consumed[1]))
            case AssertionKind.NUMERIC_EQUALS:
                results.append(_check_numeric_equals(assertion, consumed[0]))
            case AssertionKind.TEXT_CONTAINS:
                results.append(_check_text_contains(assertion, consumed))
            case AssertionKind.CONTROL_PRESENT:
                results.append(_check_control_present(assertion, consumed[0]))
            case AssertionKind.SEMANTIC:
                # Left for the semantic adjudicator; recorded as pending so it
                # can never be mistaken for a silent pass.
                results.append(
                    AssertionResult(
                        id=assertion.id,
                        kind=assertion.kind,
                        passed=False,
                        expected=assertion.expected_text,
                        actual="awaiting semantic adjudication",
                        failure_class=FailureClass.AUTOMATION_FAILURE,
                        evidence=_evidence(consumed),
                        reasoning="Deferred to the semantic adjudicator.",
                    )
                )
            case _:  # pragma: no cover - the enum is exhaustive above
                raise ValueError(f"no verifier for assertion kind {assertion.kind}")

    return results
