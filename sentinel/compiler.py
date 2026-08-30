"""Turn a human test case into a TestPlan.

This is the only place an LLM decides anything about *what* a test means. It
runs once per case, offline, before any device is touched, and the result is
cached on disk. Two consequences worth stating plainly:

*   The LLM is nowhere near the critical path at run time. Execution is
    deterministic Maestro; a re-run of an unchanged suite makes zero LLM calls.

*   Rewording a case is not a special feature. The cache key is a hash of the
    row, so changed wording is simply a cache miss that gets re-planned. No code
    changes, and no list of known phrasings to maintain.

The compiler is deliberately fenced in. It may only choose capabilities from a
closed vocabulary and targets that already exist in the screen map, so it cannot
invent a selector or a gesture that the renderer would not know how to perform.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from sentinel import oracle
from sentinel.schema import (
    Assertion,
    Capability,
    Feasibility,
    Persona,
    RawTestCase,
    Segment,
    Step,
    TestPlan,
)
from sentinel.screen_map import ScreenMap

# Bump when the prompt or the plan contract changes, so stale plans compiled
# under different rules are not silently reused.
PROMPT_VERSION = "1"

DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_CACHE = Path(__file__).resolve().parent.parent / ".plan_cache"


class CompilerError(Exception):
    """A case could not be turned into a usable plan."""


class _PlanDraft(BaseModel):
    """The part of a TestPlan the model is allowed to author.

    Identity fields (case_id, source_hash, title) are filled in from the sheet
    afterwards, so the model cannot mislabel a plan.
    """

    primary_persona: Persona
    segments: list[Segment] = Field(default_factory=list)
    assertions: list[Assertion] = Field(default_factory=list)
    cleanup: list[Step] = Field(default_factory=list)
    feasibility: Feasibility = Feasibility.AUTOMATABLE
    feasibility_reason: str = ""
    compiler_notes: str = ""


SYSTEM_PROMPT = """\
You are the planning stage of an autonomous mobile QA system testing Bautech, a \
construction business-management Android app.

You convert one human-written test case into a structured TestPlan. You do not \
decide whether the test passes. You do not predict what the app will do. You \
describe what to do and what would have to be true for the expectation to hold.

HOW TO READ A CASE

The `expected` text is the contract. Read it literally and completely. It may \
contain more than one claim, and it may contain claims of opposite polarity in \
one sentence - for example "Status updates correctly. Engineer must NOT have \
this option." Emit one assertion per distinct claim.

Decide polarity from the meaning of `expected`, never from labels or formatting. \
Wording such as "blocked", "cannot", "must not", "is not visible", "only Admin \
can", "sees X only, not Y", or "sits as Pending" describes something that must \
NOT be possible or must NOT appear. Those become prohibition assertions \
(control_absent, token_absent, action_rejected). Everything else is a positive \
assertion.

CHOOSING ASSERTIONS

- Quantities that change: use numeric_delta with two observations, before and \
after, and set expected_delta. Never assert an absolute total - the suite runs \
repeatedly against a shared backend and absolutes drift between runs.
- A control that must not exist: control_absent, naming the control.
- Data that must not appear anywhere in scope: token_absent, naming the token, \
consuming capture_screen_text observations from every screen in scope.
- An action that must be refused when attempted: action_rejected, consuming an \
attempt_action observation.
- Something visible/true that resists exact checking: semantic, consuming \
whatever screen text supports it. Use this sparingly; prefer a deterministic kind.

RULES

1. Use only the capabilities listed. Use only target names listed in the screen \
map vocabulary. If the case needs a target that does not exist, say so in \
compiler_notes rather than inventing one.
2. Every assertion must consume observation keys that steps in your plan \
actually produce. Only read_value, capture_screen_text, probe_control and \
attempt_action produce observations, and each must name an observation_key.
3. Do not emit login steps. Each segment declares its persona and the runner \
handles the session. Emit login/logout only when the test is itself about \
signing in or out.
4. A test involving more than one persona becomes several segments in order, \
for example the engineer submits, then the admin approves.
5. Name anything the test creates with the placeholder ${RUN_ID} in it, so \
repeated runs never collide - for example "SNTL-cement-${RUN_ID}". Add cleanup \
steps to remove what you created.
6. If judging the case honestly needs an interface we do not have - a real \
payment, a billing cycle that has not elapsed, waiting for a scheduled alert, \
a database - set feasibility to needs_unavailable_interface and name the \
missing interface in feasibility_reason. Do not fake it, and do not substitute \
a weaker check while claiming the original. This is the correct and expected \
answer for some cases.
7. Prefer observing the state that actually changed over trusting a success \
message. A toast is not evidence that stock moved.
"""


def _inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Flatten pydantic $defs/$ref into a self-contained JSON schema."""
    defs = schema.pop("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                target = defs.get(ref.split("/")[-1], {})
                merged = {**walk(target), **{k: v for k, v in node.items() if k != "$ref"}}
                return merged
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(schema)


class PlanCompiler:
    """Compiles sheet rows into TestPlans, with a disk cache."""

    def __init__(
        self,
        screen_map: ScreenMap,
        client: Any | None = None,
        model: str = DEFAULT_MODEL,
        cache_dir: str | Path | None = None,
    ) -> None:
        self.screen_map = screen_map
        self.model = model
        self.cache_dir = Path(cache_dir) if cache_dir else DEFAULT_CACHE
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._client = client
        self.stats = {"cache_hits": 0, "compiled": 0, "repairs": 0}

    # -- client ------------------------------------------------------------ #

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover
                raise CompilerError("anthropic SDK is not installed") from exc
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise CompilerError(
                    "ANTHROPIC_API_KEY is not set - the compiler needs it to plan new or "
                    "reworded cases. Cached plans still work without it."
                )
            self._client = anthropic.Anthropic()
        return self._client

    # -- cache ------------------------------------------------------------- #

    def _vocabulary_hash(self) -> str:
        payload = json.dumps(self.screen_map.vocabulary(), sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:8]

    def _cache_path(self, case: RawTestCase) -> Path:
        key = f"{case.source_hash}-{PROMPT_VERSION}-{self._vocabulary_hash()}"
        return self.cache_dir / f"{case.case_id}-{key}.json"

    def _load_cached(self, case: RawTestCase) -> TestPlan | None:
        path = self._cache_path(case)
        if not path.exists():
            return None
        try:
            return TestPlan.model_validate_json(path.read_text(encoding="utf-8"))
        except (ValidationError, OSError):
            # A cache entry written under an older contract. Drop and recompile.
            path.unlink(missing_ok=True)
            return None

    # -- prompt ------------------------------------------------------------ #

    def _user_prompt(self, case: RawTestCase) -> str:
        vocab = self.screen_map.vocabulary()
        return "\n".join(
            [
                "CAPABILITIES (the only actions available):",
                ", ".join(c.value for c in Capability),
                "",
                "SCREEN MAP VOCABULARY (the only targets available):",
                *(f"  {kind}: {', '.join(names)}" for kind, names in vocab.items()),
                "",
                "SPEC ACTIONS (for controls, to cross-reference the permission spec):",
                ", ".join(f"{m}.{a}" for m, a in oracle.actions()),
                "",
                "TEST CASE",
                f"  id:       {case.case_id}",
                f"  title:    {case.title}",
                f"  persona:  {case.persona_hint or '(not stated - infer it)'}",
                f"  steps:    {case.steps_text or '(none given - infer them)'}",
                f"  expected: {case.expected_text}",
                "",
                "Emit the plan with the emit_test_plan tool.",
            ]
        )

    def _tool_schema(self) -> dict[str, Any]:
        return _inline_refs(_PlanDraft.model_json_schema())

    # -- compilation ------------------------------------------------------- #

    def compile(self, case: RawTestCase, *, use_cache: bool = True) -> TestPlan:
        if use_cache:
            cached = self._load_cached(case)
            if cached is not None:
                self.stats["cache_hits"] += 1
                return cached

        messages: list[dict[str, Any]] = [{"role": "user", "content": self._user_prompt(case)}]
        tool = {
            "name": "emit_test_plan",
            "description": "Emit the structured plan for this test case.",
            "input_schema": self._tool_schema(),
        }

        last_error = ""
        for attempt in range(2):  # one plan, one repair
            response = self.client.messages.create(
                model=self.model,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                tools=[tool],
                tool_choice={"type": "tool", "name": "emit_test_plan"},
                messages=messages,
            )
            block = next((b for b in response.content if b.type == "tool_use"), None)
            if block is None:
                last_error = "model did not call emit_test_plan"
                continue

            try:
                plan = self._assemble(case, block.input)
            except (ValidationError, ValueError) as exc:
                last_error = str(exc)
                if attempt == 0:
                    self.stats["repairs"] += 1
                    messages += [
                        {"role": "assistant", "content": response.content},
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": block.id,
                                    "is_error": True,
                                    "content": (
                                        f"That plan was rejected:\n{last_error}\n\n"
                                        "Fix it and call emit_test_plan again."
                                    ),
                                }
                            ],
                        },
                    ]
                continue

            self._cache_path(case).write_text(plan.model_dump_json(indent=2), encoding="utf-8")
            self.stats["compiled"] += 1
            return plan

        raise CompilerError(f"{case.case_id}: could not compile a valid plan. {last_error}")

    def _assemble(self, case: RawTestCase, draft_input: dict[str, Any]) -> TestPlan:
        """Validate the model draft and attach identity from the sheet."""
        draft = _PlanDraft.model_validate(draft_input)
        plan = TestPlan(
            case_id=case.case_id,
            source_hash=case.source_hash,
            title=case.title,
            expected_text=case.expected_text,
            primary_persona=draft.primary_persona,
            segments=draft.segments,
            assertions=draft.assertions,
            cleanup=draft.cleanup,
            feasibility=draft.feasibility,
            feasibility_reason=draft.feasibility_reason,
            compiler_notes=draft.compiler_notes,
        )
        self._check_targets(plan)
        return plan

    def _check_targets(self, plan: TestPlan) -> None:
        """Reject a plan that references targets the screen map does not have.

        Caught here rather than at render time so a bad plan never reaches a
        device and never consumes runtime.
        """
        vocab = self.screen_map.vocabulary()
        known = set(vocab["screens"]) | set(vocab["controls"]) | set(vocab["values"]) | set(
            vocab["entities"]
        )
        unknown = sorted(
            {
                step.target
                for segment in plan.segments
                for step in segment.steps
                if step.target and step.target not in known
            }
            | {
                assertion.control
                for assertion in plan.assertions
                if assertion.control and assertion.control not in known
            }
        )
        if unknown:
            raise ValueError(
                f"plan references targets not in the screen map: {', '.join(unknown)}. "
                f"Use an existing target, or note the gap in compiler_notes."
            )


def compile_suite(
    cases: list[RawTestCase],
    screen_map: ScreenMap,
    compiler: PlanCompiler | None = None,
) -> tuple[list[TestPlan], list[tuple[str, str]]]:
    """Compile every case. Returns (plans, failures).

    Compilation failure is not fatal to the run: the case still needs a verdict,
    and the orchestrator turns an uncompilable case into a BLOCKED result that
    names the reason. A missing plan must never become a silent omission.
    """
    compiler = compiler or PlanCompiler(screen_map)
    plans: list[TestPlan] = []
    failures: list[tuple[str, str]] = []
    for case in cases:
        try:
            plans.append(compiler.compile(case))
        except CompilerError as exc:
            failures.append((case.case_id, str(exc)))
    return plans, failures
