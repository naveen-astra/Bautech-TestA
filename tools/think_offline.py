"""Let the real model think about a real captured Bautech screen.

No device and no phone required: the screen is a hierarchy dump genuinely
captured from the app, replayed to the agent exactly as perception would
render it live. Device actions are inert, so this cannot navigate - but it
can show whether the model reasons correctly about what is in front of it,
which is the part worth knowing before spending device time.

TC-009 is the default because it is the assessment's hardest kind of case and
the one worth twenty points: the Site Engineer must find no way to add a site.
The captured screen genuinely has no Add Site control, so a correct agent
should confirm where it is, look properly, and report an absence. A careless
one will either hallucinate a control or declare absence without ever
establishing it was in the right place - and this harness makes which of
those happened obvious.

    python tools/think_offline.py
    python tools/think_offline.py --case TC-006
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SENTINEL_ALLOW_UNVERIFIED", "1")

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent

env_file = ROOT / ".env"
if env_file.exists():
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))

from sentinel import actions as actions_module  # noqa: E402
from sentinel import agent as agent_module  # noqa: E402
from sentinel.agent import run_case  # noqa: E402
from sentinel.brains import make_brain  # noqa: E402
from sentinel.perception import capture_from_file  # noqa: E402

PACKAGE = "com.naviconinfra.bautech"
DUMP = ROOT / "tests" / "fixtures" / "bautech_sites_screen.xml"


@dataclass
class Case:
    case_id: str
    title: str
    persona: str
    steps: str
    expected: str


CASES = {
    # The real row from the sheet, word for word. Nothing marks it negative.
    "TC-009": Case(
        case_id="TC-009", title="Create site - Engineer", persona="Site Engineer",
        steps="Sites -> look for Add Site.",
        expected="Blocked - no Add Site option for Site Engineer.",
    ),
    "TC-006": Case(
        case_id="TC-006", title="View site list", persona="Owner",
        steps="Open the Sites list.",
        expected="Both sites visible with correct names/details.",
    ),
    "TC-073": Case(
        case_id="TC-073", title="Open Site B - Engineer", persona="Site Engineer",
        steps="Sites -> try to open Site B.",
        expected="Blocked - Site B is not visible at all.",
    ),
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", default="TC-009", choices=sorted(CASES))
    parser.add_argument("--max-steps", type=int, default=8)
    args = parser.parse_args()

    case = CASES[args.case]
    screen = capture_from_file(DUMP, PACKAGE)

    # The device is inert: the same real screen every look, actions do nothing.
    agent_module.capture = lambda adb, package, attempts=3: screen
    actions_module._shell = lambda adb, *a, timeout=30: ""

    brain = make_brain()
    print(f"model    : {getattr(brain, 'provider', '?')}/{getattr(brain, 'model', '?')}")
    print(f"case     : {case.case_id} - {case.title}")
    print(f"persona  : {case.persona}")
    print(f"expected : {case.expected}")
    print(f"\nthe real screen it is looking at ({len(screen.elements)} elements):\n")
    print(screen.render())
    print("\n" + "-" * 66)
    print("thinking:\n")

    run = run_case(
        case, brain, adb="adb", package=PACKAGE,
        max_steps=args.max_steps, log=lambda m: print(m),
    )

    print("\n" + "-" * 66)
    print(f"finished          : {run.finished}")
    print(f"gave up           : {run.gave_up}{' - ' + run.give_up_reason if run.gave_up else ''}")
    print(f"confirmed screen  : {run.reached_target_screen}")
    for where, why in run.confirmations:
        print(f"   confirmed '{where}' because: {why}")
    print(f"observations      : {({k: o.value for k, o in run.observations.items()}) or 'none'}")
    if run.summary:
        print(f"summary           : {run.summary}")

    usage = getattr(brain, "usage", None)
    if usage:
        print(f"\ncost              : {usage.summary()}")

    # What actually matters, checked rather than eyeballed.
    print("\n" + "=" * 66)
    truth_add_site = screen.contains("Add Site")
    print(f"ground truth: 'Add Site' really on this screen? {truth_add_site}")
    hallucinated = any(
        o.value.lower() in ("present", "yes", "visible")
        for o in run.observations.values()
    ) and not truth_add_site
    if hallucinated:
        print("VERDICT: the agent claimed something that is NOT on the screen.")
        return 1
    if run.observations and not run.confirmations:
        print("VERDICT: it recorded findings without ever establishing where it was.")
        return 1
    print("VERDICT: nothing was claimed that the screen does not support.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
