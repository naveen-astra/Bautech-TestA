"""The TestPlan contract.

Every other component keys off the types in this module:

    parser    -> RawTestCase          (what the human wrote)
    compiler  -> TestPlan             (what we intend to do about it)
    renderer  -> Maestro YAML         (how the device will do it)
    observer  -> ObservedValue        (what actually happened)
    verifier  -> AssertionResult      (whether it matched)
    verdict   -> CaseResult           (and what we are willing to claim)

Two rules are enforced structurally rather than by convention:

1.  A plan expresses *intent*, never mechanics. Steps name a logical target
    ("stock_level", "add_site_button"); only `config/screen_map.yaml` knows what
    that looks like on screen. The compiler therefore cannot invent selectors,
    and a UI change is a data edit rather than a recompile.

2.  An assertion must declare the observations it consumes. If an observation is
    missing at verification time we did not see enough to judge, and the result
    is an automation failure - never a PASS. This is what stops "we could not do
    it" from being mistaken for "the app stopped us".
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator

Persona = Literal["Owner", "Admin", "Site Engineer"]

# Personas ordered by authority. Used to pick the control persona for a
# differential probe: to prove Site Engineer cannot see a control, we check that
# someone who should see it does.
PERSONA_AUTHORITY: dict[str, int] = {"Site Engineer": 0, "Admin": 1, "Owner": 2}


# --------------------------------------------------------------------------- #
# Input: what the human tester wrote
# --------------------------------------------------------------------------- #


class RawTestCase(BaseModel):
    """One row of the tester sheet, unmodified.

    `source_hash` is the compile-cache key. Reword the row and the hash changes,
    so the case is re-planned automatically - that is the whole mechanism behind
    "the evaluator rewords a case and it still runs".
    """

    case_id: str
    title: str
    persona_hint: str = ""
    steps_text: str = ""
    expected_text: str

    @property
    def source_hash(self) -> str:
        payload = "\x1f".join(
            [self.case_id, self.title, self.persona_hint, self.steps_text, self.expected_text]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Capabilities: the closed vocabulary a plan may use
# --------------------------------------------------------------------------- #


class Capability(StrEnum):
    """Actions the renderer knows how to turn into Maestro commands.

    Closed on purpose. If the compiler wants something outside this list the
    plan is rejected, which surfaces a genuine gap in the capability layer
    instead of silently producing a flow that cannot run.
    """

    LOGIN = "login"
    LOGOUT = "logout"
    NAVIGATE = "navigate"
    OPEN_ENTITY = "open_entity"
    CREATE_ENTITY = "create_entity"
    EDIT_ENTITY = "edit_entity"
    DELETE_ENTITY = "delete_entity"
    INPUT_VALUE = "input_value"
    SELECT_OPTION = "select_option"
    SEARCH = "search"
    SUBMIT = "submit"
    APPROVE = "approve"
    REJECT = "reject"
    READ_VALUE = "read_value"
    CAPTURE_SCREEN_TEXT = "capture_screen_text"
    PROBE_CONTROL = "probe_control"
    ATTEMPT_ACTION = "attempt_action"
    SCREENSHOT = "screenshot"

    @property
    def produces_observation(self) -> bool:
        return self in _OBSERVING


_OBSERVING = frozenset(
    {
        Capability.READ_VALUE,
        Capability.CAPTURE_SCREEN_TEXT,
        Capability.PROBE_CONTROL,
        Capability.ATTEMPT_ACTION,
    }
)


class Step(BaseModel):
    """One capability invocation.

    `target` is a logical key resolved through the screen map, never a selector.
    `observation_key` names the slot an observing capability writes into; the
    verifier reads assertions against those keys.
    """

    capability: Capability
    target: str = ""
    args: dict[str, str | int | float | bool] = Field(default_factory=dict)
    observation_key: str = ""
    label: str = ""

    @model_validator(mode="after")
    def _observation_key_matches_capability(self) -> Step:
        if self.capability.produces_observation and not self.observation_key:
            raise ValueError(
                f"{self.capability} produces an observation and must name an observation_key"
            )
        if not self.capability.produces_observation and self.observation_key:
            raise ValueError(f"{self.capability} does not produce an observation")
        return self


class Segment(BaseModel):
    """A run of steps performed by one persona.

    Segments are the unit the scheduler batches into persona waves, and the unit
    that becomes a single Maestro flow file. A cross-persona test (engineer
    submits, admin approves) is one plan holding several segments.
    """

    persona: Persona
    intent: str
    steps: list[Step]

    @model_validator(mode="after")
    def _non_empty(self) -> Segment:
        if not self.steps:
            raise ValueError("a segment must contain at least one step")
        return self


# --------------------------------------------------------------------------- #
# Assertions: what we will judge
# --------------------------------------------------------------------------- #


class AssertionKind(StrEnum):
    NUMERIC_DELTA = "numeric_delta"      # after - before == expected_delta
    NUMERIC_EQUALS = "numeric_equals"
    TEXT_CONTAINS = "text_contains"
    CONTROL_PRESENT = "control_present"
    CONTROL_ABSENT = "control_absent"    # prohibition
    TOKEN_ABSENT = "token_absent"        # prohibition: text absent everywhere in scope
    ACTION_REJECTED = "action_rejected"  # prohibition: the attempt was refused
    SEMANTIC = "semantic"                # judged by LLM over captured evidence

    @property
    def is_prohibition(self) -> bool:
        return self in _PROHIBITIONS

    @property
    def is_deterministic(self) -> bool:
        return self is not AssertionKind.SEMANTIC


_PROHIBITIONS = frozenset(
    {
        AssertionKind.CONTROL_ABSENT,
        AssertionKind.TOKEN_ABSENT,
        AssertionKind.ACTION_REJECTED,
    }
)


class Assertion(BaseModel):
    """One checkable claim drawn from the Expected column.

    `expected_text` keeps the wording of the tester so the report can show what
    was promised alongside what happened.
    """

    id: str
    kind: AssertionKind
    expected_text: str
    consumes: list[str] = Field(default_factory=list)
    expected_delta: float | None = None
    expected_value: str | float | None = None
    token: str = ""
    control: str = ""

    @model_validator(mode="after")
    def _well_formed(self) -> Assertion:
        if not self.consumes:
            raise ValueError(f"assertion {self.id} must consume at least one observation")
        if self.kind is AssertionKind.NUMERIC_DELTA:
            if self.expected_delta is None:
                raise ValueError(f"assertion {self.id}: numeric_delta needs expected_delta")
            if len(self.consumes) != 2:
                raise ValueError(
                    f"assertion {self.id}: numeric_delta consumes exactly [before, after]"
                )
        if self.kind is AssertionKind.TOKEN_ABSENT and not self.token:
            raise ValueError(f"assertion {self.id}: token_absent needs a token")
        if self.kind in (AssertionKind.CONTROL_ABSENT, AssertionKind.CONTROL_PRESENT):
            if not self.control:
                raise ValueError(f"assertion {self.id}: {self.kind} needs a control")
        return self

    @property
    def is_prohibition(self) -> bool:
        return self.kind.is_prohibition


class Feasibility(StrEnum):
    """Whether the case can be judged through interfaces we actually have."""

    AUTOMATABLE = "automatable"
    NEEDS_UNAVAILABLE_INTERFACE = "needs_unavailable_interface"


class TestPlan(BaseModel):
    """The compiled intent of one test case."""

    case_id: str
    source_hash: str
    title: str
    # The Expected column verbatim. Kept so a case that is blocked before it
    # produces any assertion can still report what it was meant to prove.
    expected_text: str = ""
    primary_persona: Persona
    segments: list[Segment] = Field(default_factory=list)
    assertions: list[Assertion] = Field(default_factory=list)
    cleanup: list[Step] = Field(default_factory=list)
    feasibility: Feasibility = Feasibility.AUTOMATABLE
    feasibility_reason: str = ""
    compiler_notes: str = ""

    @model_validator(mode="after")
    def _coherent(self) -> TestPlan:
        if self.feasibility is Feasibility.NEEDS_UNAVAILABLE_INTERFACE:
            if not self.feasibility_reason:
                raise ValueError(
                    f"{self.case_id}: an unautomatable case must say which interface is missing"
                )
            return self

        if not self.segments:
            raise ValueError(f"{self.case_id}: an automatable plan needs at least one segment")
        if not self.assertions:
            raise ValueError(f"{self.case_id}: a plan with no assertion cannot produce a verdict")

        produced = {
            step.observation_key
            for segment in self.segments
            for step in segment.steps
            if step.observation_key
        }
        for assertion in self.assertions:
            missing = sorted(set(assertion.consumes) - produced)
            if missing:
                raise ValueError(
                    f"{self.case_id}: assertion {assertion.id} consumes observation(s) "
                    f"{missing} that no step produces"
                )
        return self

    @property
    def personas(self) -> list[str]:
        seen: list[str] = []
        for segment in self.segments:
            if segment.persona not in seen:
                seen.append(segment.persona)
        return seen

    @property
    def is_cross_persona(self) -> bool:
        return len(self.personas) > 1

    @property
    def prohibitions(self) -> list[Assertion]:
        """Assertions that must clear the negative-case adjudication ladder."""
        return [a for a in self.assertions if a.is_prohibition]


# --------------------------------------------------------------------------- #
# Output: what happened, and what we are willing to claim
# --------------------------------------------------------------------------- #


# An observation captured by the *control* persona carries this suffix. It is
# the other half of a differential probe: the same look, performed by someone
# the spec says should succeed, which is what turns "we saw nothing" into
# "the app hid it from this role".
CONTROL_SUFFIX = "__control"


class ActionOutcome(StrEnum):
    """What became of a deliberately attempted action."""

    SUCCEEDED = "succeeded"    # it went through - for a prohibition, that is a defect
    REFUSED = "refused"        # the app explicitly said no
    NO_CHANGE = "no_change"    # accepted in appearance, but nothing moved
    ERROR = "error"            # the app errored
    STALLED = "stalled"        # we could not complete the attempt - our problem
    UNKNOWN = "unknown"


class ObservedValue(BaseModel):
    """One thing the device actually reported back.

    `anchor_found` records whether the screen we meant to be on proved itself.
    It is the reachability rung: without it, an absence tells us nothing, since
    not-being-there looks exactly like not-being-shown.
    """

    key: str
    raw: str
    numeric: float | None = None
    screen_texts: list[str] = Field(default_factory=list)
    captured_at: str = ""
    source: Literal["ui", "api", "log"] = "ui"
    anchor_found: bool | None = None
    outcome: ActionOutcome | None = None

    @property
    def is_control(self) -> bool:
        return self.key.endswith(CONTROL_SUFFIX)

    def all_text(self) -> str:
        """Everything we read on screen, lowercased, for token searches."""
        return " \n ".join([self.raw, *self.screen_texts]).lower()


class Verdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


class FailureClass(StrEnum):
    """Why a case did not pass - kept separate from the verdict.

    The report has to tell a real Bautech defect apart from our own automation
    breaking. Both can end in a non-PASS verdict; only the first is a defect.
    """

    APPLICATION_DEFECT = "application_defect"
    AUTOMATION_FAILURE = "automation_failure"
    ENVIRONMENT_BLOCK = "environment_block"
    MISSING_INTERFACE = "missing_interface"


class AssertionResult(BaseModel):
    id: str
    kind: AssertionKind
    passed: bool
    expected: str
    actual: str
    evidence: list[str] = Field(default_factory=list)
    failure_class: FailureClass | None = None
    ladder: dict[str, bool] = Field(default_factory=dict)
    reasoning: str = ""


class CaseResult(BaseModel):
    case_id: str
    persona: str
    verdict: Verdict
    expected: str
    actual: str
    failure_class: FailureClass | None = None
    probable_cause: str = ""
    assertions: list[AssertionResult] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    duration_seconds: float = 0.0
    started_at: str = ""

    @model_validator(mode="after")
    def _defensible(self) -> CaseResult:
        """Every verdict must be defensible. No evidence, no claim."""
        if not self.evidence:
            raise ValueError(f"{self.case_id}: a verdict requires at least one piece of evidence")
        if self.verdict is not Verdict.PASS and self.failure_class is None:
            raise ValueError(f"{self.case_id}: a non-PASS verdict must be classified")
        return self
