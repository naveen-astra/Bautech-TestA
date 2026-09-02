"""Learn what we can about a build without installing it.

Answers the Phase 1 environment questions from the APK alone: package id, SDK
levels, whether it is Flutter, whether it uses Firebase, which CPU architectures
it supports (so we know if a fast emulator will do), and - the useful part - the
app's entire English UI vocabulary.

WHY THE VOCABULARY MATTERS

Bautech localises every label through Flutter's gen-l10n. Nothing in the UI is a
hardcoded string, so guessing selectors from screenshots means guessing at
translations. A debug build embeds the generated localisation class, including
the English text as doc comments:

    /// In en, this message translates to:
    /// **'Add Site'**
    String get homeScreenAddSiteTitle;

Parsing those gives the exact label for every string the app can display, which
turns the screen map from guesswork into a shortlist grounded in the build.

WHAT THIS STILL DOES NOT TELL US

That a string exists is not evidence of where it appears. "Create Site", "New
Site" and "Add Site" all exist in this build; which one is on the Sites screen
is a question only a running app answers. So this narrows the candidates - it
does not verify them. `maestro hierarchy` still has to confirm each selector
before its target is marked verified.

    python tools/inspect_apk.py app-debug.apk
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# gen-l10n writes the English text as a doc comment above each getter.
_L10N = re.compile(r"\*\*'(.*?)'\*\*.*?String get (\w+)", re.S)

# Firebase config lands in compiled resources; these are the values worth having.
_FIREBASE_KEYS = (
    "project_id",
    "google_app_id",
    "google_api_key",
    "gcm_defaultSenderId",
    "google_storage_bucket",
    "firebase_database_url",
)


def find_aapt() -> str | None:
    """Locate aapt from the Android SDK, wherever it happens to live."""
    for name in ("aapt", "aapt2"):
        found = shutil.which(name)
        if found:
            return found

    roots = [
        os.environ.get("ANDROID_HOME"),
        os.environ.get("ANDROID_SDK_ROOT"),
        os.path.expandvars(r"%LOCALAPPDATA%\Android\Sdk"),
        os.path.expanduser("~/Library/Android/sdk"),
        os.path.expanduser("~/Android/Sdk"),
    ]
    for root in roots:
        if not root:
            continue
        build_tools = Path(root) / "build-tools"
        if not build_tools.is_dir():
            continue
        # Newest build-tools first.
        for version in sorted(build_tools.iterdir(), reverse=True):
            for name in ("aapt.exe", "aapt"):
                candidate = version / name
                if candidate.exists():
                    return str(candidate)
    return None


def _run(args: list[str], timeout: int) -> str:
    """subprocess.run, decoding output as UTF-8 regardless of the console codepage.

    aapt emits UTF-8 (resource strings include non-ASCII currency symbols and
    other scripts); on Windows the default text-mode decode uses the active
    codepage instead and dies on the first byte outside it. Capture bytes and
    decode ourselves, replacing anything that still will not decode rather than
    losing the whole report over one bad string.
    """
    completed = subprocess.run(args, capture_output=True, timeout=timeout)
    return completed.stdout.decode("utf-8", errors="replace")


def badging(apk: Path, aapt: str) -> dict:
    out = _run([aapt, "dump", "badging", str(apk)], timeout=180)

    def grab(pattern: str) -> str | None:
        match = re.search(pattern, out)
        return match.group(1) if match else None

    return {
        "package": grab(r"package: name='([^']+)'"),
        "version_name": grab(r"versionName='([^']+)'"),
        "version_code": grab(r"versionCode='([^']+)'"),
        "min_sdk": grab(r"sdkVersion:'([^']+)'"),
        "target_sdk": grab(r"targetSdkVersion:'([^']+)'"),
        "permissions": sorted(set(re.findall(r"uses-permission: name='([^']+)'", out))),
    }


def firebase_config(apk: Path, aapt: str) -> dict:
    """Pull the Firebase project details out of compiled resources.

    `aapt dump --values resources` prints each entry as a header line naming
    the resource, then its value indented on the line(s) that follow -
    "string/project_id: ..." on one line, `(string8) "navicon-erp"` on the
    next. So the value is found by locating the next quoted string *after* the
    key's header line, not on the same line as the key.
    """
    out = _run([aapt, "dump", "--values", "resources", str(apk)], timeout=300)

    config: dict[str, str] = {}
    for key in _FIREBASE_KEYS:
        # aapt lists every resource twice: a "spec resource" line with no
        # value, then later the real "resource" line the value follows. Skip
        # straight past the spec occurrence to the one carrying data.
        header = re.search(rf"^\s*resource 0x\S+ \S+:string/{re.escape(key)}:.*$", out, re.M)
        if header is None:
            continue
        value = re.search(r'"([^"]{2,150})"', out[header.end():header.end() + 300])
        if value:
            config[key] = value.group(1)
    return config


def structure(apk: Path) -> dict:
    with zipfile.ZipFile(apk) as archive:
        names = archive.namelist()

    abis = sorted({n.split("/")[1] for n in names if n.startswith("lib/") and "/" in n[4:]})
    return {
        "entries": len(names),
        "is_flutter": any("libflutter.so" in n for n in names),
        "flutter_mode": (
            "debug (JIT)"
            if any("kernel_blob.bin" in n for n in names)
            else "release (AOT)" if any("libapp.so" in n for n in names) else "unknown"
        ),
        "abis": abis,
        "emulator_friendly": any(a.startswith("x86") for a in abis),
        "uses_firebase": any("firebase" in n.lower() for n in names),
        "firebase_auth": any("firebase-auth" in n.lower() for n in names),
    }


def vocabulary(apk: Path) -> dict[str, str]:
    """Every English label the app can display, keyed by its l10n name."""
    with zipfile.ZipFile(apk) as archive:
        blob = next(
            (n for n in archive.namelist() if n.endswith("flutter_assets/kernel_blob.bin")), None
        )
        if blob is None:
            return {}
        text = archive.read(blob).decode("utf-8", "replace")

    labels: dict[str, str] = {}
    for value, key in _L10N.findall(text):
        if key not in labels and len(value) < 200:
            labels[key] = value.replace("\\'", "'")
    return labels


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2

    apk = Path(sys.argv[1])
    if not apk.exists():
        print(f"error: no such file: {apk}")
        return 1

    print(f"{apk.name}  ({apk.stat().st_size / 1e6:.0f} MB)\n")

    report: dict = {"apk": apk.name, "size_bytes": apk.stat().st_size}

    aapt = find_aapt()
    if aapt is None:
        print("warning: aapt not found; skipping manifest and resource inspection")
        print("         (it ships with the Android SDK build-tools)\n")
    else:
        report["manifest"] = badging(apk, aapt)
        report["firebase"] = firebase_config(apk, aapt)
        manifest = report["manifest"]
        print("Manifest")
        print(f"  package      {manifest['package']}")
        print(f"  version      {manifest['version_name']} (code {manifest['version_code']})")
        print(f"  sdk          min {manifest['min_sdk']}, target {manifest['target_sdk']}")
        print(f"  permissions  {len(manifest['permissions'])}")
        if report["firebase"]:
            print("\nFirebase")
            for key, value in report["firebase"].items():
                print(f"  {key:22s} {value}")

    report["structure"] = structure(apk)
    shape = report["structure"]
    print("\nBuild")
    print(f"  flutter      {shape['is_flutter']}  ({shape['flutter_mode']})")
    print(f"  abis         {', '.join(shape['abis']) or 'none'}")
    print(f"  emulator     {'x86 present - a standard emulator will run this'
                            if shape['emulator_friendly']
                            else 'ARM only - needs a physical device or slow emulation'}")
    print(f"  firebase     auth={shape['firebase_auth']}")

    labels = vocabulary(apk)
    report["label_count"] = len(labels)
    if labels:
        out = ROOT / "config" / "app_labels.json"
        out.write_text(json.dumps(labels, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"\nUI vocabulary\n  {len(labels)} English labels -> {out.relative_to(ROOT)}")
        print("  These are candidates for the screen map, not verified selectors:")
        print("  a string existing in the build says nothing about which screen shows it.")
    else:
        print("\nUI vocabulary\n  none recovered "
              "(a release build strips the localisation source)")

    summary = ROOT / "config" / "apk_report.json"
    summary.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nfull report -> {summary.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
