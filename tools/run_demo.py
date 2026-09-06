"""The single command for a live demo: plug in the phone, run this, watch it work.

Wraps run.py with the choices a demo needs and a human doesn't want to type:
the local backend, the two cases already proven end-to-end on real hardware
(TC-046, TC-009), the unverified-target override (the screen map is 4/41
verified - honest development state, not a secret to hide during a demo),
and opens the resulting report the moment it's done instead of leaving
someone to go find it.

    python tools/run_demo.py                # TC-046, TC-009 (default)
    python tools/run_demo.py --only TC-001   # any cached or compilable case

This is not a separate code path - it is run.py, with the exact same
compile -> render -> execute -> observe -> verify -> adjudicate -> report
pipeline, called the way a demo needs it called. Whatever verdict comes back
is real: a PASS here is a PASS earned by watching the real app, and a BLOCKED
here is exactly what it says - no dial is turned to make a demo look better
than the real device just showed.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _adb() -> str | None:
    found = shutil.which("adb")
    if found:
        return found
    guess = Path.home() / "AppData" / "Local" / "Android" / "Sdk" / "platform-tools" / "adb.exe"
    return str(guess) if guess.exists() else None


def check_device() -> str | None:
    """A clear, specific reason the demo can't start - or None if it can."""
    adb = _adb()
    if adb is None:
        return "adb not found - install Android platform-tools and put it on PATH"
    try:
        out = subprocess.run([adb, "devices"], capture_output=True, text=True, timeout=15).stdout
    except (subprocess.SubprocessError, OSError) as exc:
        return f"could not run `adb devices`: {exc}"
    devices = [
        line.split()[0] for line in out.splitlines()[1:]
        if line.strip().endswith("device")
    ]
    if not devices:
        return (
            "no phone detected. Check: USB cable plugged in, USB debugging enabled, "
            "and the \"Allow USB debugging\" prompt accepted on the phone screen."
        )
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default="TC-046,TC-009",
                         help="comma-separated case ids (default: the two proven on real hardware)")
    parser.add_argument("--no-open", action="store_true", help="don't open the report automatically")
    args = parser.parse_args()

    print("Bautech Sentinel - live demo")
    print("=" * 40)

    problem = check_device()
    if problem:
        print(f"\ncannot start: {problem}")
        return 2
    print("phone detected.\n")

    env = {**os.environ, "SENTINEL_ALLOW_UNVERIFIED": "1"}
    command = [
        sys.executable, str(ROOT / "run.py"),
        "--backend", "local",
        "--only", args.only,
    ]
    result = subprocess.run(command, cwd=ROOT, env=env)

    # run.py prints the exact results directory of the run it just did; find
    # the newest one under results/ rather than re-parsing that output, so
    # this stays correct even if run.py's own logging changes.
    results_root = ROOT / "results"
    candidates = [
        p for p in results_root.iterdir()
        if p.is_dir() and (p / "report.html").exists()
    ] if results_root.exists() else []
    latest = max(candidates, key=lambda p: p.stat().st_mtime, default=None)

    if latest and not args.no_open:
        print(f"\nopening {latest / 'report.html'}")
        webbrowser.open((latest / "report.html").resolve().as_uri())
    elif latest:
        print(f"\nreport: {latest / 'report.html'}")

    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
