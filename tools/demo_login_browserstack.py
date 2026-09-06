"""Run the real Owner login flow on real BrowserStack infrastructure.

Mirrors tools/demo_login.py exactly, but through BrowserStackBackend instead
of a local device - this needs no LLM compilation (render_login is called
directly, the same way demo_login.py does locally), so it works without
ANTHROPIC_API_KEY. The point is narrow and specific: prove that the fixed
login flow (language screen, Phone tab, OTP entry, "Verify OTP") reaches the
real post-login home screen on a cloud device, for a persona already
confirmed to be a real member of the test company - unlike the Site Engineer
account used in TC-046/TC-009, which lands on "Create Company / Join
Company" instead, a separate, real account-provisioning gap.

    python tools/demo_login_browserstack.py --app app-release_2.apk
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

import run as sentinel_run  # noqa: E402
from sentinel.backends.browserstack import BrowserStackBackend  # noqa: E402
from sentinel.renderer import FlowRenderer  # noqa: E402
from sentinel.screen_map import ScreenMap  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", required=True)
    parser.add_argument("--device", default="Samsung Galaxy S22-12.0")
    args = parser.parse_args()

    sentinel_run.load_dotenv()

    screen_map = ScreenMap.load()
    personas = yaml.safe_load((ROOT / "config" / "personas.yaml").read_text(encoding="utf-8"))
    renderer = FlowRenderer(
        screen_map, personas, app_id=screen_map.meta["app_id"], run_id="OWNLOGIN"
    )

    commands = [
        {"evalScript": "${console.log('@@FLOW start owner-login-proof')}"},
        # clearState: true - see sentinel/renderer.py's render_segment for
        # the real, live-confirmed reason: a stale session left over from
        # earlier testing resumes on launch otherwise, and this script's
        # whole point is to prove a fresh login, not accidentally skip it.
        {"launchApp": {"appId": screen_map.meta["app_id"], "clearState": True}},
        *renderer.render_login("Owner"),
    ]
    doc = {"appId": screen_map.meta["app_id"], "name": "owner-login-proof"}
    flow_text = yaml.safe_dump(doc, sort_keys=False) + "---\n" + yaml.safe_dump(
        commands, sort_keys=False
    )

    flow_dir = ROOT / "flows" / "generated" / "_owner_login_proof"
    flow_dir.mkdir(parents=True, exist_ok=True)
    flow_path = flow_dir / "owner-login-proof.yaml"
    flow_path.write_text(flow_text, encoding="utf-8")
    print(f"rendered -> {flow_path.relative_to(ROOT)}")
    print(flow_text)

    env = sentinel_run.persona_env(personas)

    backend = BrowserStackBackend(app_path=args.app, devices=[args.device])
    problems = backend.check()
    if problems:
        print("preflight problems:")
        for p in problems:
            print(f"  - {p}")
        return 2

    out_dir = ROOT / "results" / "_owner_login_proof"
    print("\nexecuting on browserstack...")
    artifacts = backend.run(flow_dir, out_dir, env=env, timeout_seconds=1800)

    print(f"exit_code: {artifacts.exit_code}")
    for url in artifacts.session_urls:
        print(f"  {url}")
    for log in artifacts.logs:
        print(f"  log: {log}")

    return 0 if artifacts.exit_code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
