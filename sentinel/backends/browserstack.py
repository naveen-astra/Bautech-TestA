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

FIRST LIVE CONTACT: 2026-09-03, a real account, a real 167MB release APK, two
real cases (TC-046, TC-009) on a real Samsung Galaxy S22. The build genuinely
ran on real hardware (89s device session, real teardown, a real build URL) -
proof the upload/build/poll path works end to end. What live contact actually
taught, in the order it was found:

  1. A 300s flat timeout was too short for a 150+MB upload on this
     connection - the socket's own send() timed out. Fixed: the app-upload
     timeout now scales with file size (up to a 1-hour cap).
  2. The retried upload then hit a *different* failure: the connection was
     reset by the remote host mid-transfer - no HTTP response at all, just a
     transport failure. This is exactly the transient case bounded retries
     exist for (it happens before any device work or verdict exists, so it
     can never mask a defect). Fixed: `_post`/`_get` now retry up to 3 times
     on a connection-level exception, rewinding any file-like body first.
  3. With the upload finally through, the build itself ran for real - and
     log retrieval 404'd on every kind. The path this file had been
     hand-constructing (`sessions/tests/{id}/{kind}` on
     api-cloud.browserstack.com/.../v2/...) was wrong in three compounding
     ways: wrong host, wrong version segment, and wrong id (a *session* id
     was being used where a *test case* id was needed). BrowserStack's own
     documentation pages disagree with each other on this exact path across
     different pages, which is itself the reason to stop guessing it.
     Fixed properly: `_save_logs` now calls the dedicated session-details
     endpoint (`builds/{id}/sessions/{session_id}`, confirmed - still under
     v2, matching `_get`) and uses the ready-made absolute log URLs
     (`maestro_log`, `device_log`) it returns verbatim, rather than
     templating a path itself. See `_save_logs`'s own docstring for the
     full account.

  Everything below was CONFIRMED by directly fetching BrowserStack's own
  documentation pages (not a search summary, not a guess) before first
  contact, and held up under it except where corrected above:
    - the base URL and auth scheme (HTTP Basic, username:access-key)
    - the test-suite upload field name ("file") and the build-start body
      shape (app, testSuite, devices, project - all as already written)
    - the env-var field is `setEnvVariables`, not `envVariables` - the
      original name would have been silently dropped by the API rather
      than rejected, so a cloud run would have started, looked healthy,
      and every login would have failed on an empty credential with
      nothing to explain why. Confirmed live: the real build.json shows
      `setEnvVariables` carrying the real credentials through correctly.
    - GET builds/{id} returns devices[].sessions[], each with an `id` -
      confirmed live, though that id turned out to identify a session, not
      a test case (see point 3 above)
    - the real log kinds are maestrologs, devicelogs, networklogs,
      commandlogs, screenshot, video - NOT "sessionlogs", which this file
      originally guessed and does not exist
    - no per-session dashboard URL is documented in the build-status
      response; _session_urls() does not pretend a bare id might be one

  STILL UNCONFIRMED:
    - the full vocabulary of build status values beyond running/queued/
      passed/failed
    - whether `custom_id` (seen on the test-suite upload example) is worth
      setting for readability in the dashboard - cosmetic, not load-bearing
    - whether the real Bautech login flow actually completes on a cloud
      device - the one live build so far produced zero observations, and
      the most likely reason is unrelated to any of the above: the
      post-login anchor this file's own render_login() checks for
      ("New Site") was already flagged elsewhere as an unconfirmed guess,
      never exercised for real before this run

A documentation page and a live API do not always agree, sometimes even with
another page of the same documentation - first real contact remains the only
way to be certain, and three rounds of it each turned up something the
previous round could not have.
"""

from __future__ import annotations

import io
import json
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
        # `devices or [default]` would silently overrule an explicit empty
        # list (a caller-side bug) with the default device, making check()'s
        # "no devices selected" complaint dead code. Distinguish "not given"
        # from "given as empty" instead, so that complaint can actually fire.
        self.devices = ["Samsung Galaxy S22-12.0"] if devices is None else devices
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

    def _post(self, path: str, timeout: int = 300, attempts: int = 3, **kwargs) -> dict:
        """POST with a bounded retry for connection-level failures only.

        Real, seen on first live contact: a 150+MB app upload got its
        connection reset by the remote host mid-transfer - no HTTP response
        was ever received, so there was no status code to react to, just a
        transport failure. A single flat attempt cannot tell that apart from
        a permanent problem, and a real network hiccup on a large upload is
        exactly the transient case bounded retries exist for - this happens
        before any device work or verdict exists, so retrying it can never
        mask a defect the way retrying a judged case would.

        Any file-like value in `files` is rewound with `.seek(0)` before each
        attempt, since a stream partially sent on a failed attempt cannot
        simply be resent from wherever it stopped.
        """
        files = kwargs.get("files")
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            if files:
                for value in files.values():
                    stream = value[1] if isinstance(value, tuple) else value
                    seek = getattr(stream, "seek", None)
                    if seek:
                        seek(0)
            try:
                response = requests.post(f"{API}/{path}", auth=self._auth, timeout=timeout, **kwargs)
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < attempts:
                    time.sleep(5 * attempt)
                continue
            if response.status_code >= 400:
                raise BackendError(f"POST {path} failed ({response.status_code}): {response.text}")
            return response.json()
        raise BackendError(
            f"POST {path} failed after {attempts} attempt(s), each ending in a connection "
            f"error before any response arrived: {last_exc}"
        )

    def _get(self, path: str, timeout: int = 120, attempts: int = 3) -> dict:
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = requests.get(f"{API}/{path}", auth=self._auth, timeout=timeout)
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < attempts:
                    time.sleep(5 * attempt)
                continue
            if response.status_code >= 400:
                raise BackendError(f"GET {path} failed ({response.status_code}): {response.text}")
            return response.json()
        raise BackendError(
            f"GET {path} failed after {attempts} attempt(s), each ending in a connection "
            f"error before any response arrived: {last_exc}"
        )

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
            # A real APK is tens to hundreds of MB - the 300s default that
            # suits a small JSON call is not enough to push that much data
            # over anything short of a fast uplink. Real failure, seen on
            # first live contact: the socket's own send() timed out, not a
            # slow server response. Scaled to the actual file size rather
            # than a second flat guess, with a floor so a small file still
            # gets a reasonable timeout and a cap so a corrupt/huge file
            # cannot hang the run indefinitely.
            size_mb = self.app_path.stat().st_size / (1024 * 1024)
            upload_timeout = min(max(int(size_mb * 15), 300), 3600)
            payload = self._post("app", files={"file": handle}, timeout=upload_timeout)
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
            # Confirmed by direct doc lookup: the real field is
            # setEnvVariables. This was "envVariables" - a real, serious bug
            # rather than a naming quibble, since an unrecognised JSON field
            # is typically just dropped rather than rejected. A cloud run
            # would have started, looked healthy, and every login would have
            # failed on an empty ${BAUTECH_OWNER_PHONE} with nothing to
            # explain why - caught here, before that ever happened for real.
            "setEnvVariables": dict(env),
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
        """Dashboard links for each test session.

        No per-session dashboard URL is documented in the build-status
        response (confirmed by direct doc lookup: "no public URL field").
        Returning the bare session id here would have looked like a URL
        without being one - listed explicitly as an id instead, alongside
        the one link that is confirmed real: the build's own dashboard page.
        """
        ids: list[str] = []
        for device in payload.get("devices", []) or []:
            for session in device.get("sessions", []) or []:
                session_id = session.get("id")
                if session_id:
                    ids.append(f"session: {session_id}")
        return ids

    def _save_logs(self, build_id: str, payload: dict, out_dir: Path) -> list[Path]:
        """Pull down whatever text the build produced.

        First real live contact (2026-09-03) proved every log URL this
        method had been hand-constructing was wrong, in three compounding
        ways at once: wrong host (`api-cloud.browserstack.com` instead of
        the real `api.browserstack.com`), wrong path (`/maestro/v2/...`
        instead of `/maestro/...` - no v2 - for log retrieval specifically,
        even though the build-status and build-start endpoints genuinely do
        use v2), and wrong id (the top-level `sessions[].id` from the
        build-status payload identifies a *session*, not a *test case* -
        BrowserStack's own docs disagree with each other on the exact log
        path across different pages, which is itself the reason to stop
        guessing).

        The actual, real build response (`build.json` from that first
        contact) confirmed the fix: `GET builds/{id}/sessions/{session_id}`
        (singular `sessions/{id}`, still under v2, exactly matching
        `_get`'s own base) returns `testcases.data[].testcases[]`, and each
        entry there already carries a ready-to-use absolute URL under
        `maestro_log`, `device_log`, etc. Fetching those verbatim, rather
        than templating a path from a kind name and an id, removes the
        entire class of host/version/id-shape guesswork this method used to
        depend on - the API is the one source of truth for its own URLs.

        `maestro_log` is where a flow's own console.log output lands, which
        is where every `@@OBS` line this whole system depends on actually
        gets written; `device_log` is Android's system log, useful for crash
        diagnosis but not where our flows print anything. maestro_log is
        fetched first and deliberately, for the same reason as before: if
        only one kind survives, it has to be this one.
        """
        saved: list[Path] = []

        # str(payload) on a Python dict produces repr syntax (single-quoted,
        # not valid JSON) - a real bug from before first contact, caught by
        # re-reading rather than by a live account.
        summary = out_dir / "build.json"
        summary.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

        for device in payload.get("devices", []) or []:
            for session in device.get("sessions", []) or []:
                session_id = session.get("id")
                if not session_id:
                    continue
                try:
                    detail = self._get(f"builds/{build_id}/sessions/{session_id}")
                except BackendError as exc:
                    (out_dir / f"{session_id}-session.error").write_text(
                        str(exc), encoding="utf-8"
                    )
                    continue
                (out_dir / f"{session_id}-session.json").write_text(
                    json.dumps(detail, indent=2, default=str), encoding="utf-8"
                )

                testcases = (detail.get("testcases") or {}).get("data") or []
                for group in testcases:
                    for testcase in group.get("testcases", []) or []:
                        test_id = testcase.get("id") or session_id
                        for field, kind in (("maestro_log", "maestrologs"),
                                             ("device_log", "devicelogs")):
                            url = testcase.get(field)
                            if not url:
                                continue
                            try:
                                response = requests.get(url, auth=self._auth, timeout=120)
                            except requests.RequestException as exc:
                                (out_dir / f"{test_id}-{kind}.error").write_text(
                                    str(exc), encoding="utf-8"
                                )
                                continue
                            if response.status_code < 400 and response.text.strip():
                                path = out_dir / f"{test_id}-{kind}.log"
                                path.write_text(response.text, encoding="utf-8")
                                saved.append(path)
                            elif response.status_code >= 400:
                                (out_dir / f"{test_id}-{kind}.error").write_text(
                                    f"HTTP {response.status_code}: {response.text}",
                                    encoding="utf-8",
                                )

        return saved
