"""Run flows on a local emulator or attached device via the Maestro CLI.

The development backend. Everything the cloud backend has to do over HTTP,
this one does by invoking `maestro test` and reading the files it leaves behind.

Secrets reach the flows as Maestro parameters (`-e KEY=value`), never as
literals in the YAML, so generated flows stay safe to commit and to upload.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from sentinel.backends.base import BackendError, RunArtifacts


def _resolve_adb() -> str | None:
    """`adb`, or a real fallback if PATH doesn't have it.

    Real environment gap, not hypothetical: on the actual dev machine this
    was built and tested on, Maestro ended up on PATH but the Android SDK's
    platform-tools directory never did - every use of `adb` this whole
    project ran was a manually-typed absolute path, which meant `check()`'s
    own `shutil.which("adb")` failed the preflight even with a real phone
    attached and everything else working. A user-PATH fix does not reach a
    shell that was already running when it was made, so a demo launched from
    that same long-lived session would keep failing regardless - this
    fallback is what actually makes `python tools/run_demo.py` (or the
    `run_demo.bat` double-click it wraps) work without that timing trap.
    """
    found = shutil.which("adb")
    if found:
        return found
    guess = Path.home() / "AppData" / "Local" / "Android" / "Sdk" / "platform-tools" / "adb.exe"
    return str(guess) if guess.exists() else None


class LocalBackend:
    """Maestro CLI against whatever device `adb` can see."""

    name = "local"

    def __init__(self, maestro: str = "maestro", device: str | None = None) -> None:
        self.maestro = maestro
        self.device = device

    def _resolve_maestro(self) -> str:
        """The actual path to invoke, not just the bare command name.

        On Windows, `maestro` is a `.bat` wrapper, and CreateProcess (what
        subprocess.run uses without shell=True) does not apply PATHEXT
        resolution the way a shell does - `subprocess.run(["maestro", ...])`
        fails with WinError 2 even though `shutil.which("maestro")` finds it
        fine. Resolving here once means the caller never has to know.
        """
        resolved = shutil.which(self.maestro)
        return resolved or self.maestro

    # -- preflight --------------------------------------------------------- #

    def check(self) -> list[str]:
        problems: list[str] = []

        if shutil.which(self.maestro) is None:
            problems.append(
                f"{self.maestro!r} is not on PATH. Install it from "
                "https://docs.maestro.dev/maestro-cli/how-to-install-maestro-cli"
            )

        adb = _resolve_adb()
        if adb is None:
            problems.append(
                "adb is not on PATH and no Android SDK install was found under "
                "~/AppData/Local/Android/Sdk. It ships with the Android SDK "
                "platform-tools; add that directory to PATH."
            )
        else:
            try:
                out = subprocess.run(
                    [adb, "devices"], capture_output=True, text=True, timeout=20
                ).stdout
                # First line is a header; a usable device is any line ending in "device".
                devices = [
                    line.split()[0]
                    for line in out.splitlines()[1:]
                    if line.strip().endswith("device")
                ]
                if not devices:
                    problems.append(
                        "no Android device or emulator is attached (`adb devices` is empty)"
                    )
            except (subprocess.SubprocessError, OSError) as exc:
                problems.append(f"could not run `adb devices`: {exc}")

        return problems

    # -- execution --------------------------------------------------------- #

    def run(
        self,
        flow_dir: Path,
        out_dir: Path,
        env: dict[str, str],
        timeout_seconds: int = 1800,
    ) -> RunArtifacts:
        flow_dir, out_dir = Path(flow_dir), Path(out_dir)
        if not any(flow_dir.glob("*.yaml")):
            raise BackendError(f"no flows to run in {flow_dir}")
        out_dir.mkdir(parents=True, exist_ok=True)

        debug_dir = out_dir / "debug"
        junit = out_dir / "report.xml"

        command = [
            self._resolve_maestro(),
            *(["--device", self.device] if self.device else []),
            "test",
            "--format", "junit",
            "--output", str(junit),
            "--debug-output", str(debug_dir),
            "--test-output-dir", str(out_dir / "artifacts"),
        ]
        for key, value in env.items():
            command += ["-e", f"{key}={value}"]
        command.append(str(flow_dir))

        # Maestro shells out to adb itself once it's actually running, using
        # whatever PATH this subprocess inherits - passing preflight via the
        # resolved-fallback in check() does not help Maestro find adb if the
        # real system PATH is still missing it. Prepending the resolved
        # adb's own directory covers exactly that gap without needing PATH
        # itself to be correct.
        subprocess_env = {**os.environ}
        adb = _resolve_adb()
        if adb:
            adb_dir = str(Path(adb).parent)
            if adb_dir not in subprocess_env.get("PATH", ""):
                subprocess_env["PATH"] = adb_dir + os.pathsep + subprocess_env.get("PATH", "")

        # Real, live finding: Maestro's own background analytics "heartbeat"
        # writes to a shared key-value store under ~/.maestro every ~5s, and
        # on this machine that write kept losing a file-lock race (repeated
        # `Failed to record heartbeat` IOExceptions in maestro.log) - harmless
        # on its own, but the very next tapOn after a burst of them gave up
        # after 0.4s instead of the several seconds every prior successful
        # run took, meaning something about the exception storm was
        # disrupting Maestro's own retry timing. Opting out (the documented
        # MAESTRO_CLI_NO_ANALYTICS variable) removes the contention rather
        # than trying to out-guess its effect on internal scheduling.
        subprocess_env["MAESTRO_CLI_NO_ANALYTICS"] = "true"

        started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                env=subprocess_env,
            )
            exit_code, stdout, stderr = completed.returncode, completed.stdout, completed.stderr
            notes: list[str] = []
        except subprocess.TimeoutExpired as exc:
            exit_code = 124
            stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            notes = [f"maestro timed out after {timeout_seconds}s"]

        # Maestro prints the console output the flows produced. Keep it even on
        # failure - a run that died half way still carries the observations it
        # managed to emit, and those are what tell us how far we got.
        console = out_dir / "console.log"
        console.write_text(stdout + "\n" + stderr, encoding="utf-8")

        logs = [console]
        logs += sorted(debug_dir.rglob("maestro.log"))

        return RunArtifacts(
            logs=logs,
            screenshots=sorted(out_dir.rglob("*.png")),
            junit=junit if junit.exists() else None,
            exit_code=exit_code,
            duration_seconds=time.monotonic() - started,
            notes=notes,
        )
