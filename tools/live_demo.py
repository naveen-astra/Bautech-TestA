"""The thinking agent, live, on the real phone. No stub, no fixture.

Everything here is real: perception reads the actual screen over adb, actions
tap and type on the actual device, and the brain is whichever provider is
configured in .env (Groq by default - zero cost). This is the same agent
proven against a captured screen in tools/think_offline.py, now given a real
one to navigate.

`clearState: false` on purpose: a fresh install is exactly what today's
documented app defect breaks (OTP verification resets to onboarding on every
persona). Resuming whatever session the phone already holds sidesteps a known
app bug that has nothing to do with the agent, so the demo shows agent
reasoning rather than re-triggering an already-reported issue.

    python tools/live_demo.py
    python tools/live_demo.py --case TC-006
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
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

from sentinel.agent import AgentRun, Step, run_case  # noqa: E402
from sentinel.brains import BrainError, make_brain, preflight  # noqa: E402
from sentinel.demo_report import write_report  # noqa: E402
from sentinel.login import login  # noqa: E402
from sentinel.perception import PerceptionError, capture  # noqa: E402

# Credentials never touch the model - this dict is read directly from .env by
# this script and used only by the deterministic login sequence, which is
# plain Python with no LLM call in it at all.
PERSONA_CREDENTIALS = {
    "Owner": ("BAUTECH_OWNER_PHONE", "BAUTECH_OWNER_OTP"),
    "Admin": ("BAUTECH_ADMIN_PHONE", "BAUTECH_ADMIN_OTP"),
    "Site Engineer": ("BAUTECH_ENGINEER_PHONE", "BAUTECH_ENGINEER_OTP"),
}

PACKAGE = "com.naviconinfra.bautech"


def find_adb() -> str:
    guess = Path.home() / "AppData" / "Local" / "Android" / "Sdk" / "platform-tools" / "adb.exe"
    if guess.exists():
        return str(guess)
    return "adb"


def device_connected(adb: str) -> bool:
    out = subprocess.run([adb, "devices"], capture_output=True, text=True, timeout=15).stdout
    return any(line.strip().endswith("device") for line in out.splitlines()[1:])


@dataclass
class Case:
    case_id: str
    title: str
    persona: str
    steps: str
    expected: str


CASES = {
    "TC-006": Case(
        case_id="TC-006", title="View site list", persona="Owner",
        steps="Open the Sites list.",
        expected="Both sites visible with correct names/details.",
    ),
    "TC-009": Case(
        case_id="TC-009", title="Create site - Engineer", persona="Site Engineer",
        steps="Sites -> look for Add Site.",
        expected="Blocked - no Add Site option for Site Engineer.",
    ),
    "TC-072": Case(
        case_id="TC-072", title="Reports - Engineer scope", persona="Site Engineer",
        steps="Open Reports.",
        expected="Sees Site A data only; no Site B or company-wide figures.",
    ),
    "LOOK": Case(
        case_id="LOOK", title="Report what screen this is", persona="whoever is logged in",
        steps="Look at the current screen and work out what it is.",
        expected="An accurate description of the current screen and what is on it.",
    ),
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", default="LOOK", choices=sorted(CASES))
    parser.add_argument("--max-steps", type=int, default=12)
    parser.add_argument("--no-launch", action="store_true",
                         help="don't relaunch the app - use whatever is already on screen")
    parser.add_argument("--login", action="store_true",
                         help="log in first (deterministic, no LLM), matching the case's persona")
    args = parser.parse_args()

    adb = find_adb()
    if not device_connected(adb):
        print("no phone detected - check the USB cable and that debugging is authorised.")
        return 2

    try:
        brain = make_brain()
    except BrainError as exc:
        print(f"cannot build a brain: {exc}")
        return 2
    for problem in preflight(brain):
        print(f"cannot start: {problem}")
        return 2

    print(f"brain  : {getattr(brain, 'provider', 'claude')}/{getattr(brain, 'model', '?')}")
    print(f"case   : {args.case} - {CASES[args.case].title}")
    print(f"expected: {CASES[args.case].expected}\n")

    if not args.no_launch:
        # clearState: false - resume whatever session is already live. A
        # fresh install is exactly what the documented OTP-reset defect
        # breaks, and that is a known app issue unrelated to the agent.
        print("launching the app (resuming any existing session)...")
        subprocess.run(
            [adb, "shell", "am", "start", "-n", f"{PACKAGE}/.MainActivity"],
            capture_output=True, timeout=20,
        )
        time.sleep(3)

    case = CASES[args.case]

    if args.login:
        env_names = PERSONA_CREDENTIALS.get(case.persona)
        if env_names is None:
            print(f"no credentials configured for persona {case.persona!r}")
            return 2
        phone = os.environ.get(env_names[0], "")
        otp = os.environ.get(env_names[1], "")
        if not phone or not otp:
            print(f"{env_names[0]} / {env_names[1]} not set in .env")
            return 2
        print(f"logging in as {case.persona} (deterministic, no LLM call)...\n")
        start_shot = subprocess.run(
            [adb, "exec-out", "screencap", "-p"], capture_output=True, timeout=15
        ).stdout
        result = login(adb, PACKAGE, phone, otp, log=print)
        if not result.ok:
            print(f"\nlogin did not complete: {result.reason}")
            # A failed login is exactly the kind of real evidence that used
            # to end up as a loose, easy-to-lose screenshot. It gets the same
            # structured report as a full run - login steps as the trace,
            # give_up_reason carrying the real diagnosis.
            fake_run = AgentRun(
                case_id=case.case_id,
                steps=[Step(number=i + 1, action_name="login", description=msg, result="")
                       for i, msg in enumerate(result.steps)],
                gave_up=True, give_up_reason=result.reason,
            )
            try:
                screen_now = capture(adb, PACKAGE)
                rendered = screen_now.render()
            except PerceptionError:
                rendered = "(could not read the screen after the failed login)"
            folder = write_report(
                ROOT / "results", adb, case, fake_run, brain, rendered,
                start_screenshot=start_shot,
            )
            print(f"\nreport           : {(folder / 'report.html').relative_to(ROOT)}")
            try:
                os.startfile(str(folder / "report.html"))  # noqa: S606
            except (AttributeError, OSError):
                pass
            return 1
        print()

    try:
        screen = capture(adb, PACKAGE)
    except PerceptionError as exc:
        print(f"could not read the screen: {exc}")
        return 2

    print(f"real screen right now ({len(screen.elements)} elements):\n")
    print(screen.render())
    print("\n" + "-" * 66)
    print("the agent is now thinking and acting on the real device:\n")

    # Captured now, before the agent touches anything - this is the only
    # moment a "before" screenshot is possible, since the report is written
    # after the run and the device has moved on by then.
    start_shot = subprocess.run(
        [adb, "exec-out", "screencap", "-p"], capture_output=True, timeout=15
    ).stdout

    run = run_case(
        CASES[args.case], brain, adb=adb, package=PACKAGE,
        max_steps=args.max_steps, log=lambda m: print(m),
    )

    print("\n" + "-" * 66)
    print(f"finished         : {run.finished}")
    print(f"gave up          : {run.gave_up}{' - ' + run.give_up_reason if run.gave_up else ''}")
    print(f"confirmed screen : {run.reached_target_screen}")
    for where, why in run.confirmations:
        print(f"   confirmed '{where}': {why}")
    if run.observations:
        print("observations     :")
        for key, obs in run.observations.items():
            print(f"   {key} = {obs.value!r}")
    if run.summary:
        print(f"summary          : {run.summary}")

    usage = getattr(brain, "usage", None)
    if usage:
        print(f"\ncost             : {usage.summary()}")

    # Everything from this run - report, both real screenshots, the full
    # trace - lands in one dedicated, clearly-named folder, and the master
    # index at results/demos/index.html is rebuilt to include it. This is the
    # one place demo output lives now; nothing is scattered loose into
    # results/ anymore.
    folder = write_report(
        ROOT / "results", adb, case, run, brain, screen.render(),
        start_screenshot=start_shot,
    )
    print(f"\nreport           : {(folder / 'report.html').relative_to(ROOT)}")
    print(f"all demo runs    : {(ROOT / 'results' / 'demos' / 'index.html').relative_to(ROOT)}")
    try:
        os.startfile(str(folder / "report.html"))  # noqa: S606 - local file, opened for the user
    except (AttributeError, OSError):
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
