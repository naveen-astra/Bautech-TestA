"""Run flows on BrowserStack App Automate.

BrowserStack's Maestro integration is a batch pipeline, not a remote control:

    upload the app          -> app_url
    upload the zipped flows -> test_suite_url
    start a build           -> build_id
    poll until it finishes
    fetch the logs

Nothing can intervene between the second and the last step, which is the reason
this whole system plans everything up front and judges everything afterwards.
The flows carry their findings out in the device console log, because that is
the channel BrowserStack preserves and hands back.

Credentials come from BROWSERSTACK_USERNAME and BROWSERSTACK_ACCESS_KEY.

NOT YET EXERCISED. This is written against BrowserStack's documented Maestro
API and has never been run - no account was available. Treat the endpoint
shapes and the log-retrieval path as unverified until a real build has gone
through, and expect the artefact retrieval in particular to need adjusting.
"""

from __future__ import annotations

import io
import os
import time
import zipfile
from pathlib import Path

import requests

from sentinel.backends.base import BackendError, RunArtifacts

API = "https://api-cloud.browserstack.com/app-automate/maestro/v2"

# BrowserStack rejects an app upload silently if the file is not really an APK,
# so the size check below is a cheap way to catch a pointer or an empty file.
_MIN_APK_BYTES = 1_000_000


class BrowserStackBackend:
    """Batch execution on real cloud devices."""

    name = "browserstack"

    def __init__(
        self,
        app_path: str | Path | None = None,
        devices: list[str] | None = None,
        project: str = "bautech-sentinel",
        poll_seconds: int = 20,
    ) -> None:
        self.app_path = Path(app_path) if app_path else None
        self.devices = devices or ["Samsung Galaxy S22-12.0"]
        self.project = project
        self.poll_seconds = poll_seconds
        self._app_url: str | None = None

    # -- plumbing ---------------------------------------------------------- #

    @property
    def _auth(self) -> tuple[str, str]:
        user = os.environ.get("BROWSERSTACK_USERNAME", "")
        key = os.environ.get("BROWSERSTACK_ACCESS_KEY", "")
        if not user or not key:
            raise BackendError(
                "BROWSERSTACK_USERNAME and BROWSERSTACK_ACCESS_KEY must both be set"
            )
        return user, key

    def _post(self, path: str, **kwargs) -> dict:
        response = requests.post(f"{API}/{path}", auth=self._auth, timeout=300, **kwargs)
        if response.status_code >= 400:
            raise BackendError(f"POST {path} failed ({response.status_code}): {response.text}")
        return response.json()

    def _get(self, path: str) -> dict:
        response = requests.get(f"{API}/{path}", auth=self._auth, timeout=120)
        if response.status_code >= 400:
            raise BackendError(f"GET {path} failed ({response.status_code}): {response.text}")
        return response.json()

    # -- preflight --------------------------------------------------------- #

    def check(self) -> list[str]:
        problems: list[str] = []

        for name in ("BROWSERSTACK_USERNAME", "BROWSERSTACK_ACCESS_KEY"):
            if not os.environ.get(name):
                problems.append(f"{name} is not set")

        if self.app_path is None:
            problems.append("no app path given; pass --app path/to/bautech.apk")
        elif not self.app_path.exists():
            problems.append(f"app not found: {self.app_path}")
        elif self.app_path.stat().st_size < _MIN_APK_BYTES:
            problems.append(
                f"{self.app_path} is only {self.app_path.stat().st_size} bytes - "
                "that is not a real APK"
            )

        if not self.devices:
            problems.append("no devices selected")

        return problems

    # -- upload ------------------------------------------------------------ #

    def upload_app(self) -> str:
        """Upload the APK once and reuse the resulting URL for later builds."""
        if self._app_url:
            return self._app_url
        if self.app_path is None:
            raise BackendError("no app path configured")
        with self.app_path.open("rb") as handle:
            payload = self._post("app", files={"file": handle})
        url = payload.get("app_url")
        if not url:
            raise BackendError(f"app upload returned no app_url: {payload}")
        self._app_url = url
        return url

    def upload_suite(self, flow_dir: Path) -> str:
        """Zip the flows in memory and upload them as a test suite."""
        flows = sorted(Path(flow_dir).rglob("*.yaml"))
        if not flows:
            raise BackendError(f"no flows to upload in {flow_dir}")

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for flow in flows:
                archive.write(flow, arcname=flow.relative_to(flow_dir).as_posix())
        buffer.seek(0)

        payload = self._post("test-suite", files={"file": ("flows.zip", buffer, "application/zip")})
        url = payload.get("test_suite_url")
        if not url:
            raise BackendError(f"suite upload returned no test_suite_url: {payload}")
        return url

    # -- execution --------------------------------------------------------- #

    def run(
        self,
        flow_dir: Path,
        out_dir: Path,
        env: dict[str, str],
        timeout_seconds: int = 1800,
    ) -> RunArtifacts:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        started = time.monotonic()
        body = {
            "app": self.upload_app(),
            "testSuite": self.upload_suite(Path(flow_dir)),
            "devices": self.devices,
            "project": self.project,
            "deviceLogs": True,
            "networkLogs": True,
            # Secrets travel as Maestro parameters, the same as locally.
            "envVariables": dict(env),
        }
        build = self._post("android/build", json=body)
        build_id = build.get("build_id")
        if not build_id:
            raise BackendError(f"build did not start: {build}")

        status, payload = self._poll(build_id, timeout_seconds, started)
        logs = self._save_logs(build_id, payload, out_dir)

        return RunArtifacts(
            logs=logs,
            junit=None,
            session_urls=[
                f"https://app-automate.browserstack.com/builds/{build_id}",
                *self._session_urls(payload),
            ],
            exit_code=0 if status == "passed" else 1,
            duration_seconds=time.monotonic() - started,
            notes=[f"browserstack build {build_id} finished as {status}"],
        )

    def _poll(self, build_id: str, timeout_seconds: int, started: float) -> tuple[str, dict]:
        while True:
            payload = self._get(f"builds/{build_id}")
            status = str(payload.get("status", "")).lower()
            if status not in ("running", "queued", ""):
                return status, payload
            if time.monotonic() - started > timeout_seconds:
                raise BackendError(
                    f"build {build_id} still {status!r} after {timeout_seconds}s; "
                    f"see https://app-automate.browserstack.com/builds/{build_id}"
                )
            time.sleep(self.poll_seconds)

    @staticmethod
    def _session_urls(payload: dict) -> list[str]:
        urls: list[str] = []
        for device in payload.get("devices", []) or []:
            for session in device.get("sessions", []) or []:
                url = session.get("public_url") or session.get("id")
                if url:
                    urls.append(str(url))
        return urls

    def _save_logs(self, build_id: str, payload: dict, out_dir: Path) -> list[Path]:
        """Pull down whatever text the build produced.

        The observations live in the device console output, so this is the step
        the whole cloud path depends on. If it comes back empty the run has
        produced no evidence, and the orchestrator will correctly refuse to
        report verdicts rather than inventing them.
        """
        saved: list[Path] = []

        summary = out_dir / "build.json"
        summary.write_text(str(payload), encoding="utf-8")

        for device in payload.get("devices", []) or []:
            for session in device.get("sessions", []) or []:
                session_id = session.get("id")
                if not session_id:
                    continue
                for kind in ("devicelogs", "sessionlogs"):
                    try:
                        response = requests.get(
                            f"{API}/builds/{build_id}/sessions/{session_id}/{kind}",
                            auth=self._auth,
                            timeout=120,
                        )
                    except requests.RequestException as exc:
                        (out_dir / f"{session_id}-{kind}.error").write_text(
                            str(exc), encoding="utf-8"
                        )
                        continue
                    if response.status_code < 400 and response.text.strip():
                        path = out_dir / f"{session_id}-{kind}.log"
                        path.write_text(response.text, encoding="utf-8")
                        saved.append(path)

        return saved
