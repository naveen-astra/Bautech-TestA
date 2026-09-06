"""Render the real login flow for all three personas, using real credentials.

This is not a synthetic demo - it uses the actual test phone numbers and OTP
codes registered in the navicon-erp Firebase console, read from .env exactly
the way run.py does. The generated YAML is the literal file that would be
handed to Maestro on a real run; nothing here is illustrative.

    python tools/demo_login.py [--execute]

Without --execute, only renders and prints the flows. With --execute, also
runs the Owner login against whatever device adb currently sees - useful for
proving the chain works up to wherever it currently stops.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

import run as sentinel_run  # noqa: E402
from sentinel.renderer import FlowRenderer  # noqa: E402
from sentinel.screen_map import ScreenMap  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    sentinel_run.load_dotenv()

    screen_map = ScreenMap.load()
    personas = yaml.safe_load((ROOT / "config" / "personas.yaml").read_text(encoding="utf-8"))
    renderer = FlowRenderer(
        screen_map, personas, app_id=screen_map.meta["app_id"], run_id="DEMO01"
    )

    out_dir = ROOT / "flows" / "generated" / "demo_login"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"app id: {screen_map.meta['app_id']}")
    print(f"{screen_map.coverage()}\n")

    env = sentinel_run.persona_env(personas)
    missing = [
        name for name in ("BAUTECH_OWNER_PHONE", "BAUTECH_ADMIN_PHONE", "BAUTECH_ENGINEER_PHONE")
        if name not in env
    ]
    if missing:
        print(f"error: missing credentials for {', '.join(missing)} - check .env")
        return 1
    print("credentials resolved from .env for all 3 personas (values not printed)\n")

    rendered: dict[str, Path] = {}
    for persona in ("Owner", "Admin", "Site Engineer"):
        # render_login assumes the app is already open on the login screen -
        # true inside a full case flow (render_segment adds launchApp once at
        # the top), but this is a standalone flow, so it needs its own.
        # clearState: true - a stale session from earlier testing (a
        # different persona, even the phone owner's own account) resumes on
        # launch otherwise, and this script's whole point is to prove a
        # fresh login, not accidentally skip it.
        commands = [
            {"launchApp": {"appId": screen_map.meta["app_id"], "clearState": True}},
            *renderer.render_login(persona),
        ]
        doc = {"appId": screen_map.meta["app_id"], "name": f"login-{persona}"}
        flow = yaml.safe_dump(doc, sort_keys=False) + "---\n" + yaml.safe_dump(
            commands, sort_keys=False
        )
        path = out_dir / f"login-{persona.lower().replace(' ', '-')}.yaml"
        path.write_text(flow, encoding="utf-8")
        rendered[persona] = path

        print(f"=== {persona} -> {path.relative_to(ROOT)} ===")
        print(flow)
        literal = any(v in flow for v in env.values())
        print(f"credential values NOT baked into the YAML: {not literal}")
        print()

    if not args.execute:
        print("(pass --execute to actually run the Owner flow against a connected device)")
        return 0

    print("=" * 62)
    print("Executing the Owner login against the currently attached device...")
    print("=" * 62)

    # subprocess can't exec the shell-script wrapper directly on Windows -
    # CreateProcess needs something the OS itself knows how to launch.
    maestro_bat = Path.home() / ".maestro" / "bin" / "maestro.bat"
    maestro = str(maestro_bat) if maestro_bat.exists() else str(
        Path.home() / ".maestro" / "bin" / "maestro"
    )
    command = [maestro, "test"]
    for key, value in env.items():
        command += ["-e", f"{key}={value}"]
    command.append(str(rendered["Owner"]))

    result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    print(result.stdout)
    if result.stderr:
        print("--- stderr ---")
        print(result.stderr)
    print(f"exit code: {result.returncode}")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
