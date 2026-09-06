"""Prove the BrowserStack backend's contract with the API is correct, without
spending a live account's App Automate minutes on it.

Live credentials now exist and are verified (see `tools/browserstack_check.py`,
which hits the free `plan.json` endpoint), but a full build still costs real
trial minutes and real upload time, so this file stays mock-only. See the
module docstring in `sentinel/backends/browserstack.py` for what is confirmed
by direct documentation lookup versus still unconfirmed, and for what first
live contact itself has already turned up (a 300s flat timeout too short for
a 150+MB upload; a mid-transfer connection reset that a bounded retry now
recovers from).
What this file CAN do, and does, is prove that the code correctly implements
the contract it claims to: the right fields go out, the right paths get
requested, and the responses get turned into artefacts correctly - the same
way `tools/retry_tests.py` proves the retry loop without a device and
`tools/scheduler_tests.py` proves the scheduler without a run.

The module's own `requests` reference is replaced with a fake object for each
test (not the real `requests` package, and not even monkeypatched onto it -
`sentinel.backends.browserstack.requests` is reassigned to a throwaway
namespace and restored at the end), so this suite makes zero network calls
and needs no environment beyond what it sets itself.

    python tools/browserstack_tests.py
"""

from __future__ import annotations

import io
import json
import os
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SENTINEL_ALLOW_UNVERIFIED", "1")

from sentinel.backends import browserstack as bs  # noqa: E402
from sentinel.backends.base import BackendError  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = ROOT / "results" / "_bstest"
_failures: list[str] = []
_checks = 0


def check(name: str, got, want) -> None:
    global _checks
    _checks += 1
    if got == want:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}: got {got!r}, want {want!r}")
        _failures.append(name)


def check_true(name: str, condition: bool) -> None:
    check(name, condition, True)


def section(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


# --------------------------------------------------------------------------- #
# Fakes: a throwaway stand-in for the `requests` module, not the real thing.


class FakeRequestException(Exception):
    """Stands in for requests.RequestException in the fake module below."""


class FakeResponse:
    def __init__(self, status_code: int = 200, json_data: dict | None = None,
                 text: str | None = None) -> None:
        self.status_code = status_code
        self._json = json_data
        if text is not None:
            self.text = text
        elif json_data is not None:
            self.text = json.dumps(json_data)
        else:
            self.text = ""

    def json(self) -> dict:
        return self._json


class FakeRequestsModule:
    """Replaces `browserstack.requests` for one test. Real `requests` is
    never touched - this is a separate object, not a monkeypatch of the
    shared library, so nothing here can leak into other code in the process.
    """

    RequestException = FakeRequestException

    def __init__(self, post_fn=None, get_fn=None) -> None:
        self.post = post_fn or self._unscripted("POST")
        self.get = get_fn or self._unscripted("GET")

    @staticmethod
    def _unscripted(verb: str):
        def _raise(url, **kwargs):
            raise AssertionError(f"unscripted {verb} {url}")
        return _raise


REAL_REQUESTS = bs.requests


def install(post_fn=None, get_fn=None) -> FakeRequestsModule:
    fake = FakeRequestsModule(post_fn, get_fn)
    bs.requests = fake
    return fake


def restore() -> None:
    bs.requests = REAL_REQUESTS


def make_app_file(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)
    return path


def with_env(**kv):
    """Context manager: set env vars, restore whatever was there before."""
    class _Ctx:
        def __enter__(self):
            self._prev = {k: os.environ.get(k) for k in kv}
            for k, v in kv.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            return self

        def __exit__(self, *exc):
            for k, prev in self._prev.items():
                if prev is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = prev
    return _Ctx()


SCRATCH.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- #
section("check() preflight catches problems before a run starts")

with with_env(BROWSERSTACK_USERNAME=None, BROWSERSTACK_ACCESS_KEY=None):
    backend = bs.BrowserStackBackend(app_path=None)
    problems = backend.check()
    check_true("missing username is reported", any("USERNAME" in p for p in problems))
    check_true("missing access key is reported", any("ACCESS_KEY" in p for p in problems))
    check_true("missing app path is reported", any("no app path" in p for p in problems))

with with_env(BROWSERSTACK_USERNAME="u", BROWSERSTACK_ACCESS_KEY="k"):
    tiny_app = make_app_file(SCRATCH / "tiny.apk", 100)
    backend = bs.BrowserStackBackend(app_path=tiny_app)
    problems = backend.check()
    check_true("a file too small to be a real APK is caught",
                any("not a real APK" in p for p in problems))

    missing_app = SCRATCH / "does_not_exist.apk"
    backend = bs.BrowserStackBackend(app_path=missing_app)
    problems = backend.check()
    check_true("a nonexistent app path is caught", any("not found" in p for p in problems))

    real_app = make_app_file(SCRATCH / "real.apk", bs._MIN_APK_BYTES + 1)
    backend = bs.BrowserStackBackend(app_path=real_app, devices=[])
    problems = backend.check()
    check_true("no devices selected is caught", any("no devices" in p for p in problems))

    backend = bs.BrowserStackBackend(app_path=real_app)
    check("a fully valid config reports zero problems", backend.check(), [])


# --------------------------------------------------------------------------- #
section("upload_app() uploads once and caches the URL")

real_app = SCRATCH / "real.apk"
calls = []


def post_app_ok(url, auth=None, timeout=None, **kwargs):
    calls.append((url, kwargs))
    return FakeResponse(json_data={"app_url": "bs://app/abc123"})


install(post_fn=post_app_ok)
with with_env(BROWSERSTACK_USERNAME="u", BROWSERSTACK_ACCESS_KEY="k"):
    backend = bs.BrowserStackBackend(app_path=real_app)
    url1 = backend.upload_app()
    url2 = backend.upload_app()
check("upload_app returns the app_url from the response", url1, "bs://app/abc123")
check("a second call reuses the cached URL", url2, url1)
check("only one POST was actually made", len(calls), 1)
check("the upload field name is 'file'", "file" in calls[0][1].get("files", {}), True)
restore()

install(post_fn=lambda *a, **k: FakeResponse(json_data={}))
with with_env(BROWSERSTACK_USERNAME="u", BROWSERSTACK_ACCESS_KEY="k"):
    backend = bs.BrowserStackBackend(app_path=real_app)
    try:
        backend.upload_app()
        check_true("a response with no app_url raises BackendError", False)
    except BackendError:
        check_true("a response with no app_url raises BackendError", True)
restore()

with with_env(BROWSERSTACK_USERNAME="u", BROWSERSTACK_ACCESS_KEY="k"):
    backend = bs.BrowserStackBackend(app_path=None)
    try:
        backend.upload_app()
        check_true("no app_path configured raises BackendError", False)
    except BackendError:
        check_true("no app_path configured raises BackendError", True)


# --------------------------------------------------------------------------- #
section("upload_suite() zips exactly the flows present, nothing else")

flow_dir = SCRATCH / "flows"
(flow_dir / "sub").mkdir(parents=True, exist_ok=True)
(flow_dir / "TC-001.yaml").write_text("appId: x", encoding="utf-8")
(flow_dir / "sub" / "TC-002.yaml").write_text("appId: x", encoding="utf-8")
(flow_dir / "notes.txt").write_text("ignore me", encoding="utf-8")

captured_files = {}


def post_suite_ok(url, auth=None, timeout=None, **kwargs):
    captured_files.update(kwargs.get("files", {}))
    return FakeResponse(json_data={"test_suite_url": "bs://suite/xyz"})


install(post_fn=post_suite_ok)
with with_env(BROWSERSTACK_USERNAME="u", BROWSERSTACK_ACCESS_KEY="k"):
    backend = bs.BrowserStackBackend(app_path=real_app)
    suite_url = backend.upload_suite(flow_dir)
check("upload_suite returns the test_suite_url", suite_url, "bs://suite/xyz")
name, buffer, content_type = captured_files["file"]
check("the zip is offered under the name flows.zip", name, "flows.zip")
check("the zip is offered as application/zip", content_type, "application/zip")
buffer.seek(0)
with zipfile.ZipFile(buffer) as archive:
    entries = sorted(archive.namelist())
check("only the two .yaml flows are zipped, not notes.txt",
      entries, ["TC-001.yaml", "sub/TC-002.yaml"])
restore()

empty_dir = SCRATCH / "empty_flows"
empty_dir.mkdir(parents=True, exist_ok=True)
with with_env(BROWSERSTACK_USERNAME="u", BROWSERSTACK_ACCESS_KEY="k"):
    backend = bs.BrowserStackBackend(app_path=real_app)
    try:
        backend.upload_suite(empty_dir)
        check_true("an empty flow dir raises BackendError", False)
    except BackendError:
        check_true("an empty flow dir raises BackendError", True)

install(post_fn=lambda *a, **k: FakeResponse(json_data={}))
with with_env(BROWSERSTACK_USERNAME="u", BROWSERSTACK_ACCESS_KEY="k"):
    backend = bs.BrowserStackBackend(app_path=real_app)
    try:
        backend.upload_suite(flow_dir)
        check_true("a response with no test_suite_url raises BackendError", False)
    except BackendError:
        check_true("a response with no test_suite_url raises BackendError", True)
restore()


# --------------------------------------------------------------------------- #
section("run() end to end: the field names and paths that would break silently")

# This shape - a session-details call returning ready-made absolute log URLs,
# rather than this file templating a path itself - is what first live contact
# (2026-09-03) proved correct after the templated version 404'd on every log
# kind. See _save_logs's own docstring for the full account of what was wrong
# with the templated version (wrong host, wrong version segment, wrong id).

BUILD_ID = "build-777"
build_body_seen = {}
poll_queue = [
    {"status": "queued"},
    {"status": "running"},
    {
        "status": "passed",
        "devices": [
            {"sessions": [{"id": "sess-1"}, {"id": "sess-2"}, {"id": "sess-3"}]},
        ],
    },
]

LOG_HOST = "https://api.browserstack.com/app-automate/maestro"  # note: no /v2/


def log_url(test_id: str, kind: str) -> str:
    return f"{LOG_HOST}/builds/{BUILD_ID}/sessions/tests/{test_id}/{kind}"


def post_run(url, auth=None, timeout=None, **kwargs):
    if url.endswith("/app"):
        return FakeResponse(json_data={"app_url": "bs://app/abc"})
    if url.endswith("/test-suite"):
        return FakeResponse(json_data={"test_suite_url": "bs://suite/abc"})
    if url.endswith("/android/build"):
        build_body_seen.update(kwargs.get("json", {}))
        return FakeResponse(json_data={"build_id": BUILD_ID})
    raise AssertionError(f"unscripted POST {url}")


def get_run(url, auth=None, timeout=None):
    if url.endswith(f"/builds/{BUILD_ID}"):
        return FakeResponse(json_data=poll_queue.pop(0))
    if url.endswith("/sessions/sess-1"):
        return FakeResponse(json_data={"testcases": {"data": [{"testcases": [
            {"id": "tc-1", "maestro_log": log_url("tc-1", "maestrologs"),
             "device_log": log_url("tc-1", "devicelogs")},
        ]}]}})
    if url.endswith("/sessions/sess-2"):
        return FakeResponse(json_data={"testcases": {"data": [{"testcases": [
            {"id": "tc-2", "maestro_log": log_url("tc-2", "maestrologs"),
             "device_log": log_url("tc-2", "devicelogs")},
        ]}]}})
    if url.endswith("/sessions/sess-3"):
        # sess-3's own session-details call fails outright - never even gets
        # to a testcase id, let alone a log url.
        return FakeResponse(status_code=500, text="session detail unavailable")
    if url == log_url("tc-1", "maestrologs"):
        return FakeResponse(text="@@OBS {\"key\": \"v\"}\n")
    if url == log_url("tc-1", "devicelogs"):
        # 200 but empty - must be silently skipped, not saved, not errored.
        return FakeResponse(status_code=200, text="")
    if url == log_url("tc-2", "maestrologs"):
        return FakeResponse(status_code=404, text="not found")
    if url == log_url("tc-2", "devicelogs"):
        raise bs.requests.RequestException("connection reset")
    raise AssertionError(f"unscripted GET {url}")


install(post_fn=post_run, get_fn=get_run)
with with_env(BROWSERSTACK_USERNAME="u", BROWSERSTACK_ACCESS_KEY="k"):
    backend = bs.BrowserStackBackend(app_path=real_app, poll_seconds=0)
    out_dir = SCRATCH / "run_out"
    artifacts = backend.run(flow_dir, out_dir, env={"BAUTECH_OWNER_PHONE": "555"},
                             timeout_seconds=60)

check_true("the build-start body uses setEnvVariables, not envVariables",
           "setEnvVariables" in build_body_seen and "envVariables" not in build_body_seen)
check("setEnvVariables carries the real credential through",
      build_body_seen["setEnvVariables"], {"BAUTECH_OWNER_PHONE": "555"})
check("passed status maps to exit_code 0", artifacts.exit_code, 0)
check("the build dashboard URL is included",
      artifacts.session_urls[0], f"https://app-automate.browserstack.com/builds/{BUILD_ID}")
check_true("sessions are listed as labelled ids, never bare/URL-shaped",
           all(u.startswith("session: ") for u in artifacts.session_urls[1:]))
check("all three sessions' ids appear in session_urls",
      sorted(u.split(": ")[1] for u in artifacts.session_urls[1:]), ["sess-1", "sess-2", "sess-3"])

log_names = sorted(p.name for p in artifacts.logs)
check("tc-1's maestrologs (with the @@OBS line), fetched from the API's own URL, was saved",
      "tc-1-maestrologs.log" in log_names, True)
check("tc-1's empty devicelogs (200, blank) produced no log and no error file",
      (out_dir / "tc-1-devicelogs.log").exists()
      or (out_dir / "tc-1-devicelogs.error").exists(), False)
check("tc-2's 404 maestrologs produced an error file, not a log",
      (out_dir / "tc-2-maestrologs.error").exists(), True)
check("tc-2's connection failure on devicelogs produced an error file",
      (out_dir / "tc-2-devicelogs.error").exists(), True)
check_true("the connection-failure error file records the exception text",
           "connection reset" in (out_dir / "tc-2-devicelogs.error").read_text(encoding="utf-8"))
check("sess-3's own session-details failure produces a session error file, not a crash",
      (out_dir / "sess-3-session.error").exists(), True)
check_true("no log files exist for sess-3 - it never reached a testcase id",
           not any(p.name.startswith("sess-3") and p.suffix == ".log" for p in out_dir.iterdir()))

summary_path = out_dir / "build.json"
check_true("build.json was written", summary_path.exists())
parsed = json.loads(summary_path.read_text(encoding="utf-8"))
check("build.json round-trips as real JSON matching the final poll payload",
      parsed["status"], "passed")
check_true("sess-1's session-details response was also saved for later inspection",
           (out_dir / "sess-1-session.json").exists())
restore()


# --------------------------------------------------------------------------- #
section("run() maps a failed build to exit_code 1")

poll_queue2 = [{"status": "failed", "devices": []}]


def post_run2(url, auth=None, timeout=None, **kwargs):
    if url.endswith("/app"):
        return FakeResponse(json_data={"app_url": "bs://app/abc"})
    if url.endswith("/test-suite"):
        return FakeResponse(json_data={"test_suite_url": "bs://suite/abc"})
    if url.endswith("/android/build"):
        return FakeResponse(json_data={"build_id": "build-fail"})
    raise AssertionError(f"unscripted POST {url}")


def get_run2(url, auth=None, timeout=None):
    if url.endswith("/builds/build-fail"):
        return FakeResponse(json_data=poll_queue2.pop(0))
    raise AssertionError(f"unscripted GET {url}")


install(post_fn=post_run2, get_fn=get_run2)
with with_env(BROWSERSTACK_USERNAME="u", BROWSERSTACK_ACCESS_KEY="k"):
    backend = bs.BrowserStackBackend(app_path=real_app, poll_seconds=0)
    artifacts2 = backend.run(flow_dir, SCRATCH / "run_out2",
                              env={}, timeout_seconds=60)
check("a failed build maps to exit_code 1", artifacts2.exit_code, 1)
restore()


# --------------------------------------------------------------------------- #
section("_poll() gives up after the timeout, rather than looping forever")

def get_always_running(url, auth=None, timeout=None):
    return FakeResponse(json_data={"status": "running"})


install(get_fn=get_always_running)
with with_env(BROWSERSTACK_USERNAME="u", BROWSERSTACK_ACCESS_KEY="k"):
    backend = bs.BrowserStackBackend(app_path=real_app, poll_seconds=0)
    try:
        backend._poll("build-stuck", timeout_seconds=0, started=__import__("time").monotonic())
        check_true("a build stuck 'running' forever raises BackendError, not an infinite loop",
                   False)
    except BackendError as exc:
        check_true("a build stuck 'running' forever raises BackendError, not an infinite loop",
                   True)
        check_true("the timeout error names the build id", "build-stuck" in str(exc))
restore()


# --------------------------------------------------------------------------- #
section("_post/_get surface HTTP error bodies rather than swallowing them")

install(post_fn=lambda *a, **k: FakeResponse(status_code=422, text="bad request"))
with with_env(BROWSERSTACK_USERNAME="u", BROWSERSTACK_ACCESS_KEY="k"):
    backend = bs.BrowserStackBackend(app_path=real_app)
    try:
        backend._post("android/build", json={})
        check_true("a 4xx POST response raises BackendError with the body", False)
    except BackendError as exc:
        check_true("a 4xx POST response raises BackendError with the body",
                    "422" in str(exc) and "bad request" in str(exc))
restore()

install(get_fn=lambda *a, **k: FakeResponse(status_code=500, text="server exploded"))
with with_env(BROWSERSTACK_USERNAME="u", BROWSERSTACK_ACCESS_KEY="k"):
    backend = bs.BrowserStackBackend(app_path=real_app)
    try:
        backend._get("builds/x")
        check_true("a 5xx GET response raises BackendError with the body", False)
    except BackendError as exc:
        check_true("a 5xx GET response raises BackendError with the body",
                    "500" in str(exc) and "server exploded" in str(exc))
restore()


# --------------------------------------------------------------------------- #
section("Connection-level failures are retried; HTTP responses are not")

# Real, seen on first live contact: a 150+MB app upload got its connection
# reset by the remote host mid-transfer - no HTTP response at all, just a
# transport failure. time.sleep is patched out for these so the suite stays
# fast; the retry count and backoff logic themselves don't depend on real time.
real_sleep = bs.time.sleep
bs.time.sleep = lambda seconds: None

attempts_seen = []


def post_flaky_then_ok(url, auth=None, timeout=None, **kwargs):
    attempts_seen.append(1)
    if len(attempts_seen) < 3:
        raise bs.requests.RequestException("connection reset mid-upload")
    return FakeResponse(json_data={"build_id": "recovered-build"})


install(post_fn=post_flaky_then_ok)
with with_env(BROWSERSTACK_USERNAME="u", BROWSERSTACK_ACCESS_KEY="k"):
    backend = bs.BrowserStackBackend(app_path=real_app)
    result = backend._post("android/build", json={})
check("a connection error on the first two attempts still succeeds on the third",
      result, {"build_id": "recovered-build"})
check("exactly three attempts were made (two failures, one success)",
      len(attempts_seen), 3)
restore()

rewound = []


class TrackedStream(io.BytesIO):
    def seek(self, *a, **k):
        rewound.append(True)
        return super().seek(*a, **k)


stream_attempts = []


def post_flaky_upload(url, auth=None, timeout=None, **kwargs):
    stream_attempts.append(kwargs["files"]["file"].read())
    if len(stream_attempts) < 2:
        raise bs.requests.RequestException("connection reset mid-upload")
    return FakeResponse(json_data={"app_url": "bs://recovered"})


install(post_fn=post_flaky_upload)
with with_env(BROWSERSTACK_USERNAME="u", BROWSERSTACK_ACCESS_KEY="k"):
    backend = bs.BrowserStackBackend(app_path=real_app)
    stream = TrackedStream(b"fake apk bytes")
    result = backend._post("app", files={"file": stream})
check("a retried upload resends the full stream, not an exhausted one",
      stream_attempts, [b"fake apk bytes", b"fake apk bytes"])
check_true("the stream was rewound (seek) before the retried attempt",
           len(rewound) >= 1)
restore()


def post_always_broken(url, auth=None, timeout=None, **kwargs):
    raise bs.requests.RequestException("connection reset mid-upload")


install(post_fn=post_always_broken)
with with_env(BROWSERSTACK_USERNAME="u", BROWSERSTACK_ACCESS_KEY="k"):
    backend = bs.BrowserStackBackend(app_path=real_app)
    try:
        backend._post("android/build", json={})
        check_true("a connection error on every attempt eventually raises BackendError", False)
    except BackendError as exc:
        check_true("a connection error on every attempt eventually raises BackendError", True)
        check_true("the error names how many attempts were made", "3 attempt" in str(exc))
restore()


def get_always_broken(url, auth=None, timeout=None):
    raise bs.requests.RequestException("connection reset")


install(get_fn=get_always_broken)
with with_env(BROWSERSTACK_USERNAME="u", BROWSERSTACK_ACCESS_KEY="k"):
    backend = bs.BrowserStackBackend(app_path=real_app)
    try:
        backend._get("builds/x")
        check_true("_get also retries and eventually raises BackendError, not a raw exception",
                   False)
    except BackendError:
        check_true("_get also retries and eventually raises BackendError, not a raw exception",
                   True)
restore()

bs.time.sleep = real_sleep


# --------------------------------------------------------------------------- #
print(f"\n{'=' * 60}")
if _failures:
    print(f"FAILED {len(_failures)}/{_checks}:")
    for name in _failures:
        print(f"  - {name}")
    sys.exit(1)
print(f"All {_checks} checks passed.")
