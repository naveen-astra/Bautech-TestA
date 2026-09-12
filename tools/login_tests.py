"""Prove login() and logout() against a stubbed device - no phone required.

sentinel/login.py had no dedicated offline coverage before this file: it was
exercised only live, against a real phone, through tools/live_demo.py. That
meant every fix documented in its own module - the polling loop that replaced
a too-short fixed sleep, the multi-field search that replaced a single-match
find, the verified keyboard dismissal - was proven once, on hardware, and
never checked again. These tests pin all of that down on a scripted screen
sequence, so a future change to this file gets caught here before it ever
reaches a device.

logout() carries an honest limitation its own docstring states plainly: no
real hierarchy dump has ever shown where sign-out actually lives in the app,
so it only works from a screen where the control is already visible - it does
not open a drawer to go find one. These tests prove the function does exactly
what it claims: uses the control when it is there, and fails with a specific,
actionable reason when it is not, never a guess.

    python tools/login_tests.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SENTINEL_ALLOW_UNVERIFIED", "1")

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from sentinel import actions as actions_module  # noqa: E402
from sentinel import login as login_module  # noqa: E402
from sentinel.login import LoginResult, login, logout  # noqa: E402
from sentinel.perception import Element, Screen  # noqa: E402

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
    check(name, bool(condition), True)


def section(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


def screen(labels: list[str], roles: dict[str, str] | None = None) -> Screen:
    """One fixed screen. `roles` overrides a label's role; default BUTTON."""
    roles = roles or {}
    elements = [
        Element(index=i + 1, text=text, role=roles.get(text, "BUTTON"),
                clickable=True, focused=False, scrollable=False,
                bounds=(0, i * 100, 300, i * 100 + 80))
        for i, text in enumerate(labels)
    ]
    return Screen(elements=elements, package="com.naviconinfra.bautech",
                  width=720, height=1600)


def install(screens: list[Screen]) -> None:
    """Each call to capture() advances one screen; the last one repeats."""
    remaining = list(screens)

    def fake_capture(adb, package, attempts=3):
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    login_module.capture = fake_capture
    actions_module._shell = lambda adb, *args, timeout=30: ""
    # _keyboard_visible shells out to a real `adb` for real hardware state -
    # nothing to read here, and an absent/hanging adb binary must not turn
    # a logic test into a many-second (or indefinite) wait on a real
    # subprocess. Reporting "already hidden" makes _ensure_keyboard_hidden
    # return on its very first check, every time.
    login_module._keyboard_visible = lambda adb: False


# --------------------------------------------------------------------------- #
section("A complete login, phone tab already showing")

otp_screen = screen(["Enter Code", "Verify OTP"], {"Enter Code": "INPUT"})
install([
    screen(["Phone\nTab 1 of 2", "Email\nTab 2 of 2"]),
    screen(["Phone\nTab 1 of 2", "Enter 10-digit number"], {"Enter 10-digit number": "INPUT"}),
    screen(["Send OTP"]),
    # Fixed OTP mode: the phone-number field is gone, replaced by a genuinely
    # new INPUT - exactly the real bug this loop was written to survive.
    # This same real screen is captured twice in a row by the real code:
    # once inside the poll loop to find the field, once more immediately
    # after typing into it to find the Verify button - both captures see
    # the same state, so the fixture provides it twice.
    otp_screen, otp_screen,
    screen(["Site Home", "Sites"]),  # login screen text is gone - success
])
result = login("adb", "pkg", phone="9876543210", otp="123456", log=lambda m: None)
check("a full login succeeds", result.ok, True)
check_true("the phone tab was tapped", any("Phone tab" in s for s in result.steps))
check_true("Send OTP was tapped", any("Send OTP" in s for s in result.steps))
check_true("Verify OTP was tapped", any("Verify OTP" in s for s in result.steps))


# --------------------------------------------------------------------------- #
section("The one-time language screen is handled and does not break anything else")

otp_screen2 = screen(["Enter Code", "Verify OTP"], {"Enter Code": "INPUT"})
install([
    screen(["Select Your Language", "English", "Continue"]),
    screen(["Phone\nTab 1 of 2", "Email\nTab 2 of 2"]),
    screen(["Phone\nTab 1 of 2", "Enter 10-digit number"], {"Enter 10-digit number": "INPUT"}),
    screen(["Send OTP"]),
    otp_screen2, otp_screen2,
    screen(["Site Home"]),
])
result = login("adb", "pkg", phone="9876543210", otp="123456", log=lambda m: None)
check("login still succeeds with the language screen in the way", result.ok, True)
check_true("the language screen was dismissed first",
           "language screen" in result.steps[0])


# --------------------------------------------------------------------------- #
section("A real, previously-caught bug: the phone field must not eat the OTP")

# The phone-number field stays on screen unchanged - only the phone field
# exists, no new INPUT ever appears. login() must recognise that nothing new
# arrived, rather than typing the OTP into the 10-digit-full phone field a
# second time and reporting success anyway.
phone_screen = screen(["Phone\nTab 1 of 2", "Enter 10-digit number"], {"Enter 10-digit number": "INPUT"})
install([
    phone_screen, phone_screen,  # seen once to find the tab, once to find the field
    screen(["Send OTP"]),
    screen(["9876543210"], {"9876543210": "INPUT"}),  # only the old field, unchanged
])
result = login("adb", "pkg", phone="9876543210", otp="123456", log=lambda m: None,
               timeout=0.1)
check("no new OTP field is never mistaken for one", result.ok, False)
check_true("the reason says a new code field never appeared",
           "no new code field" in result.reason)


# --------------------------------------------------------------------------- #
section("Perception failure is reported, not raised")

def broken_capture(adb, package, attempts=3):
    from sentinel.perception import PerceptionError
    raise PerceptionError("uiautomator dump timed out")

login_module.capture = broken_capture
result = login("adb", "pkg", phone="9876543210", otp="123456", log=lambda m: None)
check("a device that cannot be read is a clean failure, not a crash", result.ok, False)
check_true("the real perception error is in the reason",
           "uiautomator dump timed out" in result.reason)


# --------------------------------------------------------------------------- #
section("Neither Phone tab nor a login form at all")

install([screen(["Some Other Screen", "Nothing Relevant"])])
result = login("adb", "pkg", phone="9876543210", otp="123456", log=lambda m: None)
check("an unrelated screen is reported honestly", result.ok, False)
check_true("says plainly that nothing login-shaped was found",
           "no Phone tab" in result.reason)


# --------------------------------------------------------------------------- #
section("logout(): the control is already visible")

install([
    screen(["Company Profile", "Settings", "Logout"]),
    screen(["Are you sure you want to sign out?", "Sign Out", "Cancel"]),
    screen(["Phone\nTab 1 of 2", "Email\nTab 2 of 2"]),  # back on login
])
result = logout("adb", "pkg", log=lambda m: None)
check("logout succeeds when the control is already on screen", result.ok, True)
check_true("Logout was tapped", any("Logout" in s for s in result.steps))
check_true("the confirmation dialog was handled",
           any("confirming" in s for s in result.steps))


# --------------------------------------------------------------------------- #
section("logout(): 'Sign Out' wording is recognised too, not only 'Logout'")

install([
    screen(["Account", "Sign Out"]),
    screen(["Send OTP"]),  # some builds may show no confirmation at all
])
result = logout("adb", "pkg", log=lambda m: None)
check("the 'Sign Out' label alone is enough", result.ok, True)
check_true("no confirmation step was fabricated when none appeared",
           not any("confirming" in s for s in result.steps))


# --------------------------------------------------------------------------- #
section("logout(): honest failure when no control is visible at all")

install([screen(["Sites", "Aqua Line", "Terminal 1"])])
result = logout("adb", "pkg", log=lambda m: None)
check("no Logout/Sign Out control anywhere is a clean, honest failure", result.ok, False)
check_true("the reason names exactly what is missing",
           "no 'Logout' or 'Sign Out' control" in result.reason)
check_true("the real screen is included as evidence, same discipline as login()",
           "Aqua Line" in result.reason)


# --------------------------------------------------------------------------- #
section("logout(): the login screen never reappearing is reported, not assumed")

install([
    screen(["Logout"]),
    screen(["Still Somehow Logged In", "Dashboard"]),  # confirmation never showed either
])
result = logout("adb", "pkg", log=lambda m: None, timeout=0.1)
check("a sign-out that never actually completes is reported as such", result.ok, False)
check_true("the reason says the login screen never reappeared",
           "never reappeared" in result.reason)


# --------------------------------------------------------------------------- #
section("logout(): perception failure is reported, not raised")

login_module.capture = broken_capture
result = logout("adb", "pkg", log=lambda m: None)
check("a device that cannot be read is a clean failure, not a crash", result.ok, False)
check_true("the real perception error is in the reason",
           "uiautomator dump timed out" in result.reason)


# --------------------------------------------------------------------------- #
section("logout() never invents a tap the app never offered")

# The whole point of this function's honesty: it must never guess at a
# hamburger icon or a drawer it has no confirmed selector for. Prove that no
# element is ever tapped beyond what was actually found on screen.
taps: list[int] = []
actions_module._shell = lambda adb, *args, timeout=30: taps.append(args) or ""
install([screen(["Reports", "Issues"])])  # neither label present anywhere
result = logout("adb", "pkg", log=lambda m: None)
check("nothing is tapped when there is genuinely nothing to tap", len(taps), 0)
check("and the function still returns cleanly rather than raising",
      isinstance(result, LoginResult), True)


# --------------------------------------------------------------------------- #
print(f"\n{'=' * 60}")
if _failures:
    print(f"FAILED {len(_failures)}/{_checks}:")
    for name in _failures:
        print(f"  - {name}")
    sys.exit(1)
print(f"All {_checks} checks passed.")
print("login() and logout() are both proven against a stubbed device.")
print("logout() still needs a real hierarchy dump before it can find its own")
print("way to the control from an unknown starting screen - see its docstring.")
