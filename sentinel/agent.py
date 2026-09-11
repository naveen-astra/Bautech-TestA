"""The loop that reads a test case, looks at a real screen, and works.

    observe -> think -> act -> observe -> ...

No flow is compiled ahead of time and no screen map is consulted. The agent
is handed the test case exactly as a human tester receives it - the steps and
the expected result, in the operations person's own words - and then sees only
what is genuinely on the device in front of it.

WHAT THE AGENT IS DELIBERATELY NOT TOLD

It is never told whether a case is meant to pass or to be blocked. The
assessment is explicit that polarity must come from the Expected column text
alone, and the surest way to honour that is to give the model the same words
the tester gets and nothing else - no shading, no flag, no hint from the
permission matrix. It reads "Blocked - no Add Site option for Site Engineer"
and works out for itself that it is looking for an absence.

It is also not allowed to do arithmetic. `note` records a raw value exactly as
displayed; the deterministic verifier subtracts. A case that says stock must
increase by 100 is settled by 600 - 500, never by an impression that the
screen looked about right.

WHAT COMES OUT

An `AgentRun`: every step taken, every value noted, and the full text
inventory of every screen visited. That last part is what makes absence
provable - "Site B appeared on none of the six screens I actually reached" is
evidence, where "my selector found nothing" is not.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from sentinel.actions import (
    Action,
    ActionError,
    ConfirmScreen,
    Finish,
    GiveUp,
    Note,
    ReportAttempt,
    TOOL_SCHEMA,
    build,
)
from sentinel.perception import PerceptionError, Screen, capture

SYSTEM_PROMPT = """\
You are a QA tester driving a real Android app on a real phone. You are \
executing one test case from a regression suite.

You see only what is genuinely on the screen right now, listed as numbered \
elements. Act by index. Anything not in that list is not on the screen - the \
list is filtered to the app under test, so system UI, the status bar and \
other apps are invisible to you and cannot be interacted with.

How to work:

- Read the test case's steps and its expected result. Work out for yourself \
what the case is asking you to establish. Some cases expect an action to \
succeed; others expect it to be refused, or expect something to be absent. \
Nobody will tell you which kind this is - decide from the expected result.

- Before concluding that something is absent, look properly. Scroll down, and \
check any obvious place it would live. A control below the fold is not a \
missing control, and reporting one as the other is the single worst mistake \
you can make here.

- Before drawing any conclusion about what a screen does or does not contain, \
call `confirm_screen` and say what you can actually see that makes you sure \
you are in the right place. Name something only that screen shows. If you \
cannot honestly do that, keep navigating or call `give_up` - an absence \
noticed on the wrong screen proves nothing at all, and claiming otherwise is \
the worst error available to you.

- If the case asks you to attempt something, actually attempt it, then report \
what happened with `report_attempt`. Say what the app really did - refused, \
appeared to accept but nothing changed, errored, or went through. If \
something the case expected to be blocked goes through instead, report that \
plainly. That is a real finding and hiding it would be worse than useless.

- When the case turns on a value - a stock level, a total, a count - use \
`note` to record it exactly as displayed, both before and after you act. Do \
not calculate anything and do not judge whether the number is right. That \
comparison is made elsewhere, deterministically.

- Take one action at a time and look again after each one. If the screen did \
not change when you expected it to, do not simply repeat yourself - work out \
why.

- When the steps are done, call `finish`. If you are genuinely stuck, call \
`give_up` and say exactly what stopped you. Never invent a result.
"""


class Brain(Protocol):
    """Whatever decides the next action. A model, or a script in tests."""

    def decide(self, system: str, messages: list[dict[str, Any]]) -> tuple[str, dict]:
        ...


@dataclass
class Step:
    number: int
    action_name: str
    description: str
    result: str
    screen_texts: list[str] = field(default_factory=list)


@dataclass
class Observation:
    """One value the agent read, and the standing it had when it read it."""

    key: str
    value: str
    screen_texts: list[str] = field(default_factory=list)
    screen_confirmed: bool = False
    confirmed_as: str = ""
    outcome: str = ""
    detail: str = ""


@dataclass
class AgentRun:
    """Everything one case's execution left behind."""

    case_id: str
    steps: list[Step] = field(default_factory=list)
    observations: dict[str, Observation] = field(default_factory=dict)
    screens_seen: list[list[str]] = field(default_factory=list)
    confirmations: list[tuple[str, str]] = field(default_factory=list)
    finished: bool = False
    reached_target_screen: bool = False
    summary: str = ""
    gave_up: bool = False
    give_up_reason: str = ""
    exhausted_budget: bool = False

    @property
    def all_screen_text(self) -> list[str]:
        """Every distinct piece of text seen anywhere during the run."""
        seen: list[str] = []
        for texts in self.screens_seen:
            for text in texts:
                if text not in seen:
                    seen.append(text)
        return seen

    def saw(self, needle: str) -> bool:
        lowered = needle.lower()
        return any(lowered in text.lower() for text in self.all_screen_text)

    def trace(self) -> str:
        lines = [f"{s.number:>2}. {s.description} -> {s.result}" for s in self.steps]
        return "\n".join(lines)


class ClaudeBrain:
    """The real thing: Claude picks the next action as a tool call.

    `tool_choice: any` forces a tool call every turn, so the model cannot
    drift into narrating instead of acting. Sonnet is the default because
    this loop runs on every step of every case and the reasoning it needs -
    read a screen listing, follow a test case - does not require Opus. Cost
    per run is a graded constraint here, not a footnote.
    """

    def __init__(self, model: str = "claude-sonnet-5", max_tokens: int = 1024) -> None:
        import anthropic

        self.client = anthropic.Anthropic()
        self.model = model
        self.max_tokens = max_tokens
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0

    def decide(self, system: str, messages: list[dict[str, Any]]) -> tuple[str, dict]:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            tools=TOOL_SCHEMA,
            tool_choice={"type": "any"},
            messages=messages,
        )
        self.calls += 1
        self.input_tokens += response.usage.input_tokens
        self.output_tokens += response.usage.output_tokens
        for block in response.content:
            if block.type == "tool_use":
                return block.name, dict(block.input)
        raise ActionError("the model returned no action")


def _case_brief(case: Any, mission: dict[str, str] | None = None) -> str:
    """The case in the tester's own words, plus what must be recorded.

    The wording is the sheet's own - the same sentences a human tester reads,
    with nothing added about whether this case is meant to succeed or be
    refused. That judgment is the agent's to make from the expected result.

    What IS supplied is the list of values the verdict will later be computed
    from. This is not a hint about the answer; it is the difference between a
    tester who knows to write the stock level down before touching anything
    and one who realises too late. Note that it says what to record, never
    where to find it or how to get there - the navigation is entirely the
    agent's problem, which is exactly what makes a case nobody has ever run
    before cost nothing extra.
    """
    text = (
        f"Test case {case.case_id}: {case.title}\n"
        f"Performed by: {case.persona}\n\n"
        f"Steps to perform:\n{case.steps}\n\n"
        f"Expected result:\n{case.expected}"
    )
    if mission:
        lines = "\n".join(f"  - {key}: {what}" for key, what in mission.items())
        text += (
            "\n\nRecord these, exactly as displayed, using `note` "
            "(or `report_attempt` where it names an attempt):\n" + lines
        )
    return text


def run_case(
    case: Any,
    brain: Brain,
    adb: str,
    package: str,
    max_steps: int = 40,
    log=print,
    mission: dict[str, str] | None = None,
) -> AgentRun:
    """Execute one case live, and bring back everything that happened."""
    run = AgentRun(case_id=case.case_id)
    messages: list[dict[str, Any]] = []
    brief = _case_brief(case, mission)
    confirmed_screen = ""

    for number in range(1, max_steps + 1):
        try:
            screen = capture(adb, package)
        except PerceptionError as exc:
            run.gave_up = True
            run.give_up_reason = f"could not read the screen: {exc}"
            log(f"  {number:>2}. cannot see the screen: {exc}")
            return run

        run.screens_seen.append(screen.texts)

        if number == 1:
            messages.append({
                "role": "user",
                "content": f"{brief}\n\nThe screen right now:\n\n{screen.render()}",
            })
        else:
            messages.append({
                "role": "user",
                "content": f"The screen now:\n\n{screen.render()}",
            })

        try:
            name, arguments = brain.decide(SYSTEM_PROMPT, messages)
            action = build(name, arguments)
        except ActionError as exc:
            # Tell the model precisely what was wrong and let it correct
            # itself; a malformed action is not a reason to abandon a case.
            log(f"  {number:>2}. rejected: {exc}")
            messages.append({"role": "assistant", "content": f"(invalid action: {exc})"})
            continue

        result = ""
        try:
            result = action.execute(adb, screen)
        except (ActionError, PerceptionError) as exc:
            result = f"failed: {exc}"

        run.steps.append(Step(
            number=number,
            action_name=action.name,
            description=action.describe(),
            result=result,
            screen_texts=screen.texts,
        ))
        log(f"  {number:>2}. {action.describe()} -> {result}")

        messages.append({"role": "assistant", "content": f"{action.describe()}"})

        if isinstance(action, ConfirmScreen):
            # From here on, what the agent records was seen from a screen it
            # has stated its reasons for believing it is on. That standing is
            # what the prohibition ladder needs and what stops an absence
            # noticed in the wrong place from ever becoming a pass.
            confirmed_screen = action.screen
            run.confirmations.append((action.screen, action.evidence))
        elif isinstance(action, Note):
            run.observations[action.key] = Observation(
                key=action.key,
                value=action.value,
                screen_texts=screen.texts,
                screen_confirmed=bool(confirmed_screen),
                confirmed_as=confirmed_screen,
            )
        elif isinstance(action, ReportAttempt):
            run.observations[action.key] = Observation(
                key=action.key,
                value=action.outcome,
                screen_texts=screen.texts,
                screen_confirmed=bool(confirmed_screen),
                confirmed_as=confirmed_screen,
                outcome=action.outcome,
                detail=action.detail,
            )
        elif isinstance(action, Finish):
            run.finished = True
            run.reached_target_screen = action.reached_target_screen
            run.summary = action.summary
            return run
        elif isinstance(action, GiveUp):
            run.gave_up = True
            run.give_up_reason = action.reason
            return run

        messages.append({"role": "user", "content": f"Result: {result}"})
        # Keep only the most recent screens in context - older ones are
        # superseded and paying to resend them every step is the difference
        # between an affordable run and an unaffordable one.
        messages = _trim(messages)

    run.exhausted_budget = True
    return run


def _trim(messages: list[dict[str, Any]], keep: int = 12) -> list[dict[str, Any]]:
    """Keep the opening brief plus a recent window of the conversation."""
    if len(messages) <= keep + 1:
        return messages
    return messages[:1] + messages[-keep:]
