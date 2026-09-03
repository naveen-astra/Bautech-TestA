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

        adb = shutil.which("adb")
        if adb is None:
            problems.append(
                "adb is not on PATH. It ships with the Android SDK platform-tools; "
                "add that directory to PATH."
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

        started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                env={**os.environ},
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
