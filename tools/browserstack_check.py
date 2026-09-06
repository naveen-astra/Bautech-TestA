"""Verify BROWSERSTACK_USERNAME/BROWSERSTACK_ACCESS_KEY against the real
account, without spending a single App Automate minute.

`GET app-automate/plan.json` is a plain account-status read - it costs
nothing and starts no session, so it is safe to run as many times as needed
while getting the credentials right, unlike an actual build.

    python tools/browserstack_check.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
env_path = ROOT / ".env"
if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())

import requests  # noqa: E402

user = os.environ.get("BROWSERSTACK_USERNAME", "")
key = os.environ.get("BROWSERSTACK_ACCESS_KEY", "")

if not user or not key:
    print("BROWSERSTACK_USERNAME and/or BROWSERSTACK_ACCESS_KEY not set (checked .env and env).")
    sys.exit(1)

# plan.json lives directly under app-automate/, not under the maestro/v2
# sub-path the rest of this backend uses - it is account-level, not
# Maestro-specific.
url = "https://api-cloud.browserstack.com/app-automate/plan.json"
response = requests.get(url, auth=(user, key), timeout=30)

if response.status_code == 200:
    plan = response.json()
    print(f"OK - credentials for {user!r} are valid.")
    print(f"  plan               : {plan.get('automate_plan')}")
    print(f"  parallel sessions  : {plan.get('parallel_sessions_running')}"
          f" / {plan.get('parallel_sessions_max_allowed')} max")
    print(f"  team parallel max  : {plan.get('team_parallel_sessions_max_allowed')}")
    print("No App Automate minutes were spent by this check.")
elif response.status_code == 401:
    print(f"REJECTED (401) - username/access key are wrong or not yet active.")
    print(f"  body: {response.text}")
    sys.exit(1)
else:
    print(f"Unexpected response {response.status_code} from {url}")
    print(f"  body: {response.text}")
    sys.exit(1)
