"""Prove the OTP relay works without a real mailbox.

Two things are tested for real, and one thing is stubbed:

*   The HTTP layer (`OtpRelayServer`) runs for real, on an OS-assigned port,
    and is hit with real HTTP requests. What sits behind it - `OtpSource` - is
    a fake with a scripted response, since the behaviour under test is "does
    the server hold the connection and respond correctly", not "can we reach a
    real IMAP server from this machine".
*   Code extraction (`_extract_code`, `_all_text`) runs against real
    `email.message.Message` objects built in-process, so the regex and
    multipart-walking logic is exercised exactly as it will be against a real
    message - just without needing one to actually arrive.

    python tools/otp_relay_tests.py
"""

from __future__ import annotations

import email.message
import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sentinel.otp_relay import OtpRelayServer, _all_text, _extract_code  # noqa: E402

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


def section(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


def plain_message(to: str, subject: str, body: str) -> email.message.Message:
    msg = email.message.EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    return msg


# --------------------------------------------------------------------------- #
section("Code extraction")

check(
    "finds a plain 6-digit code",
    _extract_code(plain_message("eng@test.co", "Your Bautech code", "Your code is 482913.")),
    "482913",
)
check(
    "ignores a 6-digit run embedded in a longer number",
    _extract_code(plain_message("x", "x", "Invoice #48291356 is overdue.")),
    None,
)
check(
    "ignores a 5-digit number",
    _extract_code(plain_message("x", "x", "Your PIN is 4829.")),
    None,
)
check(
    "picks the code even with surrounding punctuation",
    _extract_code(plain_message("x", "x", "Code: [123456] - expires in 10 minutes")),
    "123456",
)
check(
    "no code present",
    _extract_code(plain_message("x", "x", "Welcome to Bautech!")),
    None,
)

multipart = email.message.EmailMessage()
multipart["To"] = "owner@test.co"
multipart["Subject"] = "OTP"
multipart.set_content("Your one-time code is 998877.")
multipart.add_alternative("<p>Your one-time code is 998877.</p>", subtype="html")
check("finds the code in a multipart message's plain-text part",
      _extract_code(multipart), "998877")

check(
    "identifier search covers To, subject and body",
    "eng-test@naviconinfra.com" in _all_text(
        plain_message("eng-test@naviconinfra.com", "x", "hello")
    ),
    True,
)


# --------------------------------------------------------------------------- #
section("HTTP layer (real server, fake OTP source)")


class ScriptedSource:
    """Returns whatever this test queued, once, then remembers what it was asked."""

    def __init__(self) -> None:
        self.queued: str | None = None
        self.calls: list[tuple[str, float]] = []

    def wait_for_code(self, identifier: str, timeout_seconds: float) -> str | None:
        self.calls.append((identifier, timeout_seconds))
        return self.queued


def get(url: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


source = ScriptedSource()
server = OtpRelayServer(source, host="127.0.0.1", port=0)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
base = f"http://{server.address[0]}:{server.address[1]}"

try:
    source.queued = "555444"
    status, body = get(f"{base}/otp?identifier=eng%40test.co")
    check("a code available -> 200 with the code", (status, body.get("otp")), (200, "555444"))
    check("the identifier reached the source",
          source.calls[-1][0], "eng@test.co")

    source.queued = None
    status, body = get(f"{base}/otp?identifier=eng%40test.co&timeout_ms=50")
    check("no code in time -> 504, not a silent empty success", status, 504)
    check("timeout_ms is honoured and converted to seconds",
          source.calls[-1][1], 0.05)

    status, body = get(f"{base}/otp")
    check("no identifier -> 400, never guesses whose code to fetch", status, 400)

    status, body = get(f"{base}/nonsense")
    check("unknown path -> 404", status, 404)

    source.queued = "111222"
    status, body = get(f"{base}/otp?identifier=admin%40test.co")
    check("a second, different persona in the same run gets its own identifier",
          source.calls[-1][0], "admin@test.co")
finally:
    server.shutdown()
    thread.join(timeout=5)


# --------------------------------------------------------------------------- #
section("Renderer integration")

import os  # noqa: E402
os.environ.setdefault("SENTINEL_ALLOW_UNVERIFIED", "1")
import tempfile  # noqa: E402
import yaml  # noqa: E402
from sentinel.renderer import FETCH_OTP_JS, FlowRenderer  # noqa: E402
from sentinel.schema import (  # noqa: E402
    Assertion, AssertionKind, Capability, Segment, Step, TestPlan,
)
from sentinel.screen_map import ScreenMap  # noqa: E402

screen_map = ScreenMap.load()
relay_personas = {
    "defaults": {"otp_mode": "fixed", "relay_url_env": "SENTINEL_OTP_RELAY_URL",
                 "otp_timeout_ms": 30000},
    "personas": {
        # login_method: phone here on purpose, even though relay mode is
        # realistically paired with email in production - the phone field
        # selector is confirmed against a real device, the email one is not
        # yet (render_login raises rather than guess at it), and this test's
        # only concern is the relay/fetch_otp.js wiring, not which tab is
        # tapped to get there.
        "Site Engineer": {
            "identifier_env": "BAUTECH_ENGINEER_EMAIL", "otp_mode": "relay",
            "login_method": "phone",
        },
    },
}

renderer = FlowRenderer(screen_map, relay_personas, app_id="com.naviconinfra.bautech",
                        run_id="T1")
plan = TestPlan(
    case_id="TC-000", source_hash="h", title="relay smoke", primary_persona="Site Engineer",
    segments=[Segment(persona="Site Engineer", intent="i", steps=[
        Step(capability=Capability.READ_VALUE, target="stock_level",
             observation_key="v")])],
    assertions=[Assertion(id="a", kind=AssertionKind.TEXT_CONTAINS, expected_text="x",
                          consumes=["v"], expected_value="x")],
)

with tempfile.TemporaryDirectory() as tmp:
    written = renderer.render_plan(plan, tmp)
    fetch_script = Path(tmp) / "fetch_otp.js"
    check("fetch_otp.js is written alongside a relay-mode flow",
          fetch_script.exists(), True)
    check("its content matches the renderer's own script",
          fetch_script.read_text(encoding="utf-8"), FETCH_OTP_JS)

    flow_text = written[0].read_text(encoding="utf-8")
    check("the flow references fetch_otp.js", "fetch_otp.js" in flow_text, True)
    check("no fixed-mode OTP env leaks into a relay-mode flow",
          "BAUTECH_ENGINEER_OTP" not in flow_text, True)

# A plan using only fixed-mode personas should not get a script it never calls.
fixed_personas = {
    "defaults": {"otp_mode": "fixed"},
    "personas": {"Owner": {"identifier_env": "BAUTECH_OWNER_PHONE",
                           "otp_env": "BAUTECH_OWNER_OTP", "otp_mode": "fixed"}},
}
renderer2 = FlowRenderer(screen_map, fixed_personas, app_id="com.naviconinfra.bautech",
                         run_id="T2")
plan2 = plan.model_copy(update={
    "case_id": "TC-001", "primary_persona": "Owner",
    "segments": [Segment(persona="Owner", intent="i", steps=[
        Step(capability=Capability.SCREENSHOT, target="home")])],
})
with tempfile.TemporaryDirectory() as tmp2:
    renderer2.render_plan(plan2, tmp2)
    check("fixed-only plan does not get fetch_otp.js",
          (Path(tmp2) / "fetch_otp.js").exists(), False)


# --------------------------------------------------------------------------- #
print(f"\n{'=' * 60}")
if _failures:
    print(f"FAILED {len(_failures)}/{_checks}:")
    for name in _failures:
        print(f"  - {name}")
    sys.exit(1)
print(f"All {_checks} checks passed.")
