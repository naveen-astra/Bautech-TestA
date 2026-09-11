"""Prove the brain is real: one live call, end to end, before any device time.

Cheap to run and worth running every time the provider or model changes. It
asks the configured model to make one genuine decision about a made-up screen
and checks that what comes back is an action this system can actually execute.

A model that talks instead of acting, or names a verb that does not exist,
fails here in two seconds rather than forty minutes into a suite run.

    python tools/brain_check.py
    python tools/brain_check.py --provider ollama --model qwen2.5:7b-instruct
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SENTINEL_ALLOW_UNVERIFIED", "1")

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent

# Load .env the same way run.py does, so a key put there is honoured here.
env_file = ROOT / ".env"
if env_file.exists():
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))

from sentinel.actions import build  # noqa: E402
from sentinel.agent import SYSTEM_PROMPT  # noqa: E402
from sentinel.brains import BrainError, OpenAICompatibleBrain, make_brain, preflight  # noqa: E402

# A screen the model has never seen, with an obvious right answer. The point
# is not to test its cleverness but to confirm the whole path works: schema
# out, tool call back, action built.
SCREEN = """\
  1. TEXT   'RBAC Testing and Bauchat'
  2. BUTTON '10\\nApprovals Pending'  [tappable]
  3. BUTTON 'New Site'  [tappable]
  4. BUTTON 'Aqua Line\\nMumbai\\n13%\\nOngoing'  [tappable]
  5. BUTTON 'Sites\\nTab 1 of 4'  [tappable]
  6. BUTTON 'Parties\\nTab 2 of 4'  [tappable]
  7. BUTTON 'Report\\nTab 3 of 4'  [tappable]"""

TASK = """\
Test case TC-DEMO: Open the Reports section
Performed by: Admin

Steps to perform:
Open Reports.

Expected result:
The Reports section opens."""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider")
    parser.add_argument("--model")
    args = parser.parse_args()

    if args.provider:
        os.environ["SENTINEL_BRAIN"] = args.provider
    if args.model:
        os.environ["SENTINEL_BRAIN_MODEL"] = args.model

    try:
        brain = make_brain()
    except BrainError as exc:
        print(f"cannot build a brain: {exc}")
        return 2

    where = getattr(brain, "url", "anthropic api")
    print(f"provider : {getattr(brain, 'provider', 'claude')}")
    print(f"model    : {getattr(brain, 'model', '?')}")
    print(f"endpoint : {where}\n")

    for problem in preflight(brain):
        print(f"cannot start: {problem}")
        return 2

    print("asking it to decide one action...\n")
    started = time.monotonic()
    try:
        name, arguments = brain.decide(
            SYSTEM_PROMPT,
            [{"role": "user", "content": f"{TASK}\n\nThe screen right now:\n\n{SCREEN}"}],
        )
    except BrainError as exc:
        print(f"the model did not produce a usable action: {exc}")
        return 1
    elapsed = time.monotonic() - started

    print(f"  it chose: {name}({arguments})")

    try:
        action = build(name, arguments)
    except Exception as exc:
        print(f"  ...but that is not an action this system can run: {exc}")
        return 1
    print(f"  which means: {action.describe()}")

    usage = getattr(brain, "usage", None)
    if usage and usage.calls:
        print(f"\n  {usage.summary()}")
    print(f"  round trip: {elapsed:.1f}s")

    # Not a correctness requirement - a small model may reasonably confirm the
    # screen first - but a useful signal about how sensible it is being.
    sensible = name in ("tap", "confirm_screen", "scroll")
    print(f"\n{'OK' if sensible else 'WORKS, but odd'}: the brain returns runnable actions.")
    if name == "tap" and arguments.get("index") == 7:
        print("It also picked the right element, unprompted.")
    elif not sensible:
        print(f"'{name}' as a first move is unusual here - worth watching in a real run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
