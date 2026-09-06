"""Order test plans into persona waves, so logins are not repeated per case.

Every login costs a real OTP round-trip - proven today on real hardware to
need real patience even when everything is working. Run 85 cases in sheet
order and the Owner/Admin/Engineer accounts get logged into and out of
dozens of times over. Batch same-persona work together instead, and the
number of logins collapses toward one per persona per run.

The complication is that not every case is single-persona. TC-032 through
TC-045 span two: the Site Engineer submits an eMB, then the Admin approves
it, and the approval genuinely depends on state the Engineer's segment
created - scheduling Admin's segment first would test something that never
happened. So a plan's segments must run in the order the plan declares them,
even while different plans interleave freely around each other.

WHAT THIS MODULE DOES NOT YET DO

This produces an ordering - which segment runs in which wave, and as which
persona. It does not yet merge multiple cases' segments into a single
Maestro flow per wave (today, each segment is still its own flow file with
its own login, regardless of wave). That is the change that actually
collapses login count in a real run, and it is a real change to the
renderer's flow-per-segment model - deliberately not made today without
device time to verify it. This module is the scheduling algorithm, correct
and tested on its own; wiring it into actual flow generation is the next
step, not this one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sentinel.schema import Persona, Segment, TestPlan


@dataclass
class ScheduledSegment:
    """One plan's segment, placed in the run order."""

    plan: TestPlan
    segment: Segment
    segment_index: int
    wave_index: int


@dataclass
class Wave:
    """A run of segments that all execute as the same persona, back to back."""

    persona: Persona
    items: list[ScheduledSegment] = field(default_factory=list)

    @property
    def case_ids(self) -> list[str]:
        return [item.plan.case_id for item in self.items]


def schedule(plans: list[TestPlan]) -> list[Wave]:
    """Order every plan's segments into persona-batched waves.

    Greedy by design, not globally optimal: at each step, prefer continuing
    the current wave's persona if any plan has work ready for it, so a run of
    same-persona work is not broken up just because a different persona's
    work happened to become ready first. Ties are broken by whichever persona
    has the most ready work, since that batches the most logins away. This
    will not always find the fewest possible persona switches for every
    input, but it always respects each plan's own segment order, and it
    reliably beats the naive "run plans in sheet order" baseline that logs in
    fresh for nearly every case.
    """
    # Each plan's own segment order is a precedence chain: segment i cannot
    # run before segment i-1 of the *same* plan. Different plans have no
    # ordering constraint between each other at all.
    next_index: dict[str, int] = {p.case_id: 0 for p in plans}
    plans_by_id = {p.case_id: p for p in plans}
    remaining = {p.case_id for p in plans if p.segments}

    waves: list[Wave] = []
    current: Wave | None = None

    def ready_personas() -> dict[Persona, list[str]]:
        """Which persona each not-yet-exhausted plan is ready to run as next."""
        grouping: dict[Persona, list[str]] = {}
        for case_id in remaining:
            plan = plans_by_id[case_id]
            idx = next_index[case_id]
            persona = plan.segments[idx].persona
            grouping.setdefault(persona, []).append(case_id)
        return grouping

    while remaining:
        by_persona = ready_personas()

        # Keep riding the current wave's persona as long as anything is
        # ready for it - this is what actually produces long same-persona
        # runs instead of ping-ponging between personas case by case.
        if current is not None and current.persona in by_persona:
            persona = current.persona
        else:
            persona = max(by_persona, key=lambda p: len(by_persona[p]))
            current = Wave(persona=persona)
            waves.append(current)

        for case_id in by_persona[persona]:
            plan = plans_by_id[case_id]
            idx = next_index[case_id]
            current.items.append(
                ScheduledSegment(
                    plan=plan, segment=plan.segments[idx],
                    segment_index=idx, wave_index=len(waves) - 1,
                )
            )
            next_index[case_id] += 1
            if next_index[case_id] >= len(plan.segments):
                remaining.discard(case_id)

    return waves


def login_count(waves: list[Wave]) -> int:
    """How many persona switches this schedule costs - a proxy for logins.

    Consecutive waves of the same persona do not happen by construction
    (schedule() only opens a new wave when the current persona has run out of
    ready work), so this is just the wave count - but computed independently
    of that invariant, so a regression in schedule() that broke it would
    still be caught here rather than silently assumed.
    """
    count = 0
    last: Persona | None = None
    for wave in waves:
        if wave.persona != last:
            count += 1
            last = wave.persona
    return count


def describe(waves: list[Wave]) -> str:
    """One line per wave, for a run's own log output."""
    lines = [
        f"wave {i}: {wave.persona} - {', '.join(wave.case_ids)}"
        for i, wave in enumerate(waves)
    ]
    return "\n".join(lines)
