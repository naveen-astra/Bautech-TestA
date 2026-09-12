"""Prove the persona-wave scheduler batches logins without breaking order.

Two properties matter, and both are checked directly rather than assumed from
the algorithm looking right:

*   **Order is never violated.** A plan's second segment must never be
    scheduled before its first, no matter how the greedy batching shuffles
    everything else around it.
*   **Batching actually reduces login count.** Not just "the code runs" -
    the number of persona switches for a realistic mixed batch is checked
    against what naive sheet-order execution would have cost, and has to be
    meaningfully smaller.

    python tools/scheduler_tests.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SENTINEL_ALLOW_UNVERIFIED", "1")

from sentinel.scheduler import login_count, schedule  # noqa: E402
from sentinel.schema import (  # noqa: E402
    Assertion,
    AssertionKind,
    Capability,
    Feasibility,
    Persona,
    Segment,
    Step,
    TestPlan,
)

_failures: list[str] = []
_checks = 0


def check(name: str, got, want) -> None:
    global _checks
    _checks += 1
    if got == want:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}: got {got!r}, want {want!r}")
        _failures.append(name)


def section(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


def dummy_step(key: str) -> Step:
    return Step(capability=Capability.READ_VALUE, target="stock_level", observation_key=key)


def dummy_assertion(key: str = "v") -> Assertion:
    return Assertion(id="a", kind=AssertionKind.TEXT_CONTAINS, expected_text="x",
                     consumes=[key], expected_value="x")


def single_persona_plan(case_id: str, persona: Persona) -> TestPlan:
    return TestPlan(
        case_id=case_id, source_hash="h", title=case_id, primary_persona=persona,
        segments=[Segment(persona=persona, intent="i", steps=[dummy_step("v")])],
        assertions=[dummy_assertion("v")],
    )


def cross_persona_plan(case_id: str, personas: list[Persona]) -> TestPlan:
    return TestPlan(
        case_id=case_id, source_hash="h", title=case_id, primary_persona=personas[0],
        segments=[
            Segment(persona=p, intent=f"segment {i}", steps=[dummy_step(f"v{i}")])
            for i, p in enumerate(personas)
        ],
        # Only the first segment's observation needs to be real for the plan
        # to validate - the scheduler orders segments, it does not care what
        # each one observes.
        assertions=[dummy_assertion("v0")],
    )


def unautomatable_plan(case_id: str) -> TestPlan:
    return TestPlan(
        case_id=case_id, source_hash="h", title=case_id, primary_persona="Owner",
        feasibility=Feasibility.NEEDS_UNAVAILABLE_INTERFACE,
        feasibility_reason="needs a real payment",
    )


# --------------------------------------------------------------------------- #
section("Same-persona plans batch into one wave, regardless of sheet order")

# Deliberately shuffled: Owner, Engineer, Owner, Engineer, Owner in the sheet.
plans = [
    single_persona_plan("TC-001", "Owner"),
    single_persona_plan("TC-002", "Site Engineer"),
    single_persona_plan("TC-003", "Owner"),
    single_persona_plan("TC-004", "Site Engineer"),
    single_persona_plan("TC-005", "Owner"),
]
waves = schedule(plans)
check("shuffled single-persona plans collapse to 2 waves", len(waves), 2)
owner_wave = next(w for w in waves if w.persona == "Owner")
engineer_wave = next(w for w in waves if w.persona == "Site Engineer")
check("the Owner wave holds all three Owner cases",
      sorted(owner_wave.case_ids), ["TC-001", "TC-003", "TC-005"])
check("the Engineer wave holds both Engineer cases",
      sorted(engineer_wave.case_ids), ["TC-002", "TC-004"])


# --------------------------------------------------------------------------- #
section("Cross-persona order is never violated")

cross = cross_persona_plan("TC-032", ["Site Engineer", "Admin"])
waves = schedule([cross])
check("two waves for a two-segment cross-persona plan", len(waves), 2)
check("Engineer's segment comes first", waves[0].persona, "Site Engineer")
check("Admin's segment comes second", waves[1].persona, "Admin")

# Mixed batch: a cross-persona plan alongside single-persona ones. The
# Engineer segment of TC-032 must still land before its own Admin segment,
# even while other Engineer and Admin work from unrelated plans is happening.
mixed = [
    single_persona_plan("TC-046", "Site Engineer"),
    cross_persona_plan("TC-032", ["Site Engineer", "Admin"]),
    single_persona_plan("TC-067", "Admin"),
]
waves = schedule(mixed)


def position_of(case_id: str, segment_index: int) -> int:
    for i, wave in enumerate(waves):
        for item in wave.items:
            if item.plan.case_id == case_id and item.segment_index == segment_index:
                return i
    raise AssertionError(f"{case_id} segment {segment_index} not scheduled at all")


check("TC-032's Engineer segment (index 0) still precedes its Admin segment (index 1)",
      position_of("TC-032", 0) < position_of("TC-032", 1), True)
check("every segment of every plan was scheduled exactly once",
      sum(len(w.items) for w in waves), 4)  # 1 + 2 + 1


# --------------------------------------------------------------------------- #
section("Login count is meaningfully reduced versus naive sheet order")

# A realistic-shaped batch: 15 cases, personas shuffled the way a real sheet
# naturally would be (grouped by section, not by who runs them).
realistic = (
    [single_persona_plan(f"TC-{i:03d}", "Owner") for i in range(1, 6)]
    + [single_persona_plan(f"TC-{i:03d}", "Site Engineer") for i in range(6, 11)]
    + [cross_persona_plan(f"TC-{i:03d}", ["Site Engineer", "Admin"]) for i in range(11, 16)]
)
waves = schedule(realistic)
scheduled_switches = login_count(waves)

# Naive baseline: run every plan's segments in sheet order, logging in fresh
# whenever the persona changes from the previous segment.
naive_sequence: list[Persona] = []
for plan in realistic:
    naive_sequence += [seg.persona for seg in plan.segments]
naive_switches = sum(
    1 for i in range(1, len(naive_sequence)) if naive_sequence[i] != naive_sequence[i - 1]
) + 1

check(f"scheduled switches ({scheduled_switches}) beat naive sheet order ({naive_switches})",
      scheduled_switches < naive_switches, True)
print(f"  (naive: {naive_switches} logins, scheduled: {scheduled_switches} logins)")


# --------------------------------------------------------------------------- #
section("Edge cases")

check("an empty plan list schedules to zero waves", schedule([]), [])

no_segments = unautomatable_plan("TC-027")
waves = schedule([no_segments, single_persona_plan("TC-046", "Owner")])
check("a plan with no segments (needs_unavailable_interface) is skipped, not crashed on",
      sum(len(w.items) for w in waves), 1)
check("the automatable plan alongside it still schedules normally",
      waves[0].case_ids, ["TC-046"])

single = schedule([single_persona_plan("TC-046", "Owner")])
check("a single plan schedules to exactly one wave", len(single), 1)


# --------------------------------------------------------------------------- #
section("The schedule actually reaches the renderer, not just the algorithm")

# Neither backend takes an explicit run order: LocalBackend hands Maestro a
# directory and browserstack.py sorts the directory before zipping. So the
# only thing that can make wave order matter is the filename itself sorting
# in wave order - this proves that property on the real FlowRenderer, not on
# a mock, using the same screen map and personas.yaml a real run loads.
from sentinel.renderer import FlowRenderer  # noqa: E402
from sentinel.screen_map import ScreenMap  # noqa: E402
import tempfile  # noqa: E402
import yaml  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
screen_map = ScreenMap.load()
personas_cfg = yaml.safe_load((ROOT / "config" / "personas.yaml").read_text(encoding="utf-8"))
renderer = FlowRenderer(screen_map, personas_cfg, run_id="SCHEDTEST")

# Deliberately shuffled sheet order, same as the very first section - a
# realistic three-persona mix including a cross-persona plan.
mixed = [
    single_persona_plan("TC-005", "Owner"),
    single_persona_plan("TC-009", "Site Engineer"),
    cross_persona_plan("TC-032", ["Site Engineer", "Admin"]),
    single_persona_plan("TC-046", "Site Engineer"),
    single_persona_plan("TC-002", "Owner"),
]
waves = schedule(mixed)

with tempfile.TemporaryDirectory() as tmp:
    written, failures = renderer.render_scheduled(waves, tmp)
    check("nothing failed to render", failures, [])
    check("one file per segment", len(written),
          sum(len(w.items) for w in waves))

    names_as_written = [p.name for p in written]
    names_sorted = sorted(names_as_written)

    # The property that actually matters is about what a directory scan sees,
    # not the order `written` lists them in - within one wave, items follow
    # the scheduler's own ready-set order, not alphabetical, and that is
    # fine, because neither backend reads `written`'s order either. Reading
    # the wave number back out of each *sorted* filename recovering a
    # non-decreasing sequence is what both backends actually depend on.
    # each filename recovers a non-decreasing sequence. That is exactly what
    # both backends rely on, since neither reads wave_index directly.
    wave_numbers = [int(name.split("-", 1)[0][1:]) for name in names_sorted]
    check("wave numbers recovered from sorted filenames are non-decreasing",
          wave_numbers, sorted(wave_numbers))

    check("every plan's segment index is still recoverable from its filename",
          all(f"-{it.plan.case_id}-{it.segment_index}.yaml" in "".join(names_as_written)
              for w in waves for it in w.items),
          True)


# --------------------------------------------------------------------------- #
section("A plan that fails to render is reported, not left silently missing")

from sentinel.schema import Capability as _Capability  # noqa: E402


def broken_plan(case_id: str, persona: Persona) -> TestPlan:
    """A plan whose one step names a target the screen map has never heard of."""
    return TestPlan(
        case_id=case_id, source_hash="h", title=case_id, primary_persona=persona,
        segments=[Segment(
            persona=persona, intent="i",
            steps=[Step(capability=_Capability.READ_VALUE,
                        target="a_target_that_does_not_exist_anywhere",
                        observation_key="v")],
        )],
        assertions=[dummy_assertion("v")],
    )

mixed_with_break = [
    single_persona_plan("TC-002", "Owner"),
    broken_plan("TC-999", "Owner"),
]
waves2 = schedule(mixed_with_break)
with tempfile.TemporaryDirectory() as tmp2:
    written2, failures2 = renderer.render_scheduled(waves2, tmp2)
    check("the good plan still renders despite the broken one alongside it",
          any("TC-002" in p.name for p in written2), True)
    check("the broken plan is reported as a failure, not silently dropped",
          [f[0] for f in failures2], ["TC-999"])
    check("the failure names the persona, for the caller's BLOCKED entry",
          failures2[0][1], "Owner")


# --------------------------------------------------------------------------- #
print(f"\n{'=' * 60}")
if _failures:
    print(f"FAILED {len(_failures)}/{_checks}:")
    for name in _failures:
        print(f"  - {name}")
    sys.exit(1)
print(f"All {_checks} checks passed.")
