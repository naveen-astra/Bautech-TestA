"""Log in for real, deterministically - so the agent's thinking budget goes
toward the test case, not toward re-deriving a solved problem.

This is a deliberate design choice, not a shortcut. A human tester does not
"reason" their way through typing a phone number and an OTP - they just do
it, the same way every time, and save their judgment for what the screen
shows afterwards. Login is exactly that kind of step: it has been proven
correct on real hardware, repeatedly, elsewhere in this project, and asking a
language model to re-derive it on every case would spend real tokens re-
solving something that is not actually in question. It would also mean
sending OTP credentials through a third-party API on every single run, for no
benefit - here, they never leave this machine.

What makes this safe to keep deterministic rather than agentic: it operates
on the same package-filtered, index-addressed element list the agent itself
uses (`sentinel.perception`), so it inherits the same immunity to the status-
bar mis-tap class of bug that plagued the earlier regex-selector approach.
Matching is done against `Element.text` directly - a real field on a real
captured element - not a constructed pattern, so there is no selector to get
wrong.

Handles the one real environment quirk documented elsewhere in this project:
a genuinely fresh app state shows a one-time "Select Your Language" screen
before login. Optional and skipped cleanly when it is not there.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass

from sentinel.actions import ActionError, HideKeyboard, Tap, TypeText
from sentinel.perception import PerceptionError, Screen, capture


class LoginError(Exception):
    """Login could not be completed, with the real reason why."""


@dataclass
class LoginResult:
    ok: bool
    reason: str = ""
    steps: list[str] = None

    def __post_init__(self):
        if self.steps is None:
            self.steps = []


def _find_all(screen: Screen, *, starts_with: str = "", equals: str = "", role: str = ""):
    for element in screen.elements:
        if equals and element.text != equals:
            continue
        if starts_with and not element.text.startswith(starts_with):
            continue
        if role and element.role != role:
            continue
        yield element


def _find(screen: Screen, *, starts_with: str = "", equals: str = "", role: str = ""):
    """The first match. For a field search where more than one might exist
    on screen at once - like the phone number field still being present
    alongside a newly-arrived OTP field - use _find_all and pick correctly,
    not this: a real bug, caught live, was this function stopping at the
    first INPUT-role element and never considering a second one.
    """
    return next(_find_all(screen, starts_with=starts_with, equals=equals, role=role), None)


def _settle(seconds: float = 1.5) -> None:
    time.sleep(seconds)


def _keyboard_visible(adb: str) -> bool | None:
    """Ground truth from the OS itself, not a guess from timing.

    `HideKeyboard`'s ESC keyevent does not reliably dismiss every keyboard -
    confirmed live: the button beneath it stayed covered and the tap that
    should have hit Verify OTP landed on the keyboard instead, with no error
    from either side. `dumpsys input_method`'s own `mInputShown` flag is the
    real answer, not an assumption from how long a dismiss usually takes.
    Returns None if the state cannot be read at all, so a caller can choose
    to proceed rather than block forever on a signal it cannot get.
    """
    try:
        # Real, Windows-specific bug caught live: text=True decodes with the
        # OS default codepage (cp1252 here), and a real device's input_method
        # dump contains at least one byte that is not valid cp1252 - the
        # decode crashes inside subprocess's own reader thread, and .stdout
        # comes back None rather than raising somewhere this function could
        # catch it. Capturing raw bytes and decoding as UTF-8 with a
        # replacement fallback sidesteps the whole class of problem.
        raw = subprocess.run(
            [adb, "shell", "dumpsys", "input_method"],
            capture_output=True, timeout=10,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return None
    if raw is None:
        return None
    out = raw.decode("utf-8", errors="replace")
    for line in out.splitlines():
        if "mInputShown=" in line:
            return "mInputShown=true" in line
    return None


def _ensure_keyboard_hidden(adb: str, screen: Screen, log, attempts: int = 4) -> None:
    """Hide the keyboard and confirm it actually went away before moving on."""
    for attempt in range(attempts):
        if _keyboard_visible(adb) is False:
            return
        HideKeyboard().execute(adb, screen)
        _settle(0.6 + 0.3 * attempt)
        if _keyboard_visible(adb) is False:
            return
        # ESC alone was not enough - BACK is the more universally reliable
        # dismiss for a soft keyboard that is confirmed still open, and it is
        # safe here specifically because the keyboard being open is already
        # verified: BACK closes an open IME before it can touch the
        # underlying screen.
        subprocess.run([adb, "shell", "input", "keyevent", "4"],
                       capture_output=True, timeout=10)
        _settle(0.6 + 0.3 * attempt)
    still_shown = _keyboard_visible(adb)
    if still_shown:
        log("  login: keyboard still reported open after repeated attempts to hide it")


def login(
    adb: str,
    package: str,
    phone: str,
    otp: str,
    log=print,
    timeout: float = 20.0,
) -> LoginResult:
    """Phone tab -> number -> Send OTP -> code -> Verify. Real device, no LLM.

    Every step re-reads the real screen rather than assuming what comes next,
    because the alternative - a fixed tap sequence with no observation in
    between - is exactly the brittleness this whole project moved away from.
    The steps are fixed; whether each one's target is actually there, right
    now, on this device, is checked every time.
    """
    steps: list[str] = []

    def note(msg: str) -> None:
        steps.append(msg)
        log(f"  login: {msg}")

    try:
        screen = capture(adb, package)
    except PerceptionError as exc:
        return LoginResult(False, f"could not read the screen at all: {exc}", steps)

    # A genuinely fresh install shows a one-time language screen. Optional,
    # skipped cleanly if it is not there - see the module docstring.
    continue_btn = _find(screen, equals="Continue")
    if continue_btn and _find(screen, starts_with="Select Your Language"):
        note("language screen present - dismissing with Continue")
        Tap(index=continue_btn.index).execute(adb, screen)
        _settle()
        screen = capture(adb, package)

    phone_tab = _find(screen, starts_with="Phone")
    if phone_tab:
        note(f"tapping the Phone tab (element {phone_tab.index})")
        Tap(index=phone_tab.index).execute(adb, screen)
        _settle()
        screen = capture(adb, package)
    elif not _find(screen, starts_with="Mobile Number") and not _find(screen, role="INPUT"):
        return LoginResult(
            False,
            f"no Phone tab and nothing that looks like a login form - "
            f"real screen was:\n{screen.render()}",
            steps,
        )

    number_field = _find(screen, role="INPUT")
    if not number_field:
        return LoginResult(False, "found the Phone tab but no input field after it", steps)
    note(f"typing the phone number into element {number_field.index}")
    TypeText(text=phone, index=number_field.index).execute(adb, screen)
    _ensure_keyboard_hidden(adb, screen, note)
    _settle(0.5)

    screen = capture(adb, package)
    send_otp = _find(screen, equals="Send OTP")
    if not send_otp:
        return LoginResult(
            False,
            f"typed the number but no 'Send OTP' button is on screen - real "
            f"screen was:\n{screen.render()}",
            steps,
        )
    note("tapping Send OTP")
    Tap(index=send_otp.index).execute(adb, screen)

    # A fixed sleep here was a real, live-caught bug: on a slower transition
    # the screen had not actually changed yet, so this grabbed the still-
    # present phone-number field, and typing the OTP into it was silently
    # dropped by that field's own 10-digit limit - no error, just nothing
    # happening. Poll for the number field to genuinely be gone, the same
    # discipline already used below for Verify, rather than assume a fixed
    # wait was enough.
    otp_field = None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _settle(1.5)
        try:
            screen = capture(adb, package)
        except PerceptionError:
            continue
        # The phone field is still on screen at this point, so this must
        # check every INPUT present, not just the first - real, live-caught
        # bug: the single-match _find() kept re-finding the phone field
        # (correctly rejected for holding the phone number, but with no
        # fallback to the genuine OTP field sitting right next to it) and
        # the whole wait timed out despite the real field being there the
        # entire time.
        candidate = next(
            (e for e in _find_all(screen, role="INPUT") if e.text != phone), None
        )
        if candidate:
            otp_field = candidate
            break
    if not otp_field:
        return LoginResult(
            False,
            f"sent the OTP request but no new code field appeared within "
            f"{timeout:.0f}s (still saw the phone number, unchanged) - real "
            f"screen was:\n{screen.render()}",
            steps,
        )
    note(f"typing the OTP into element {otp_field.index}")
    TypeText(text=otp, index=otp_field.index).execute(adb, screen)
    # This exact spot is where the real, live failure happened: the keyboard
    # was still open, mInputShown=true confirmed it directly, and a tap aimed
    # at Verify OTP's real accessibility-tree coordinates landed on the
    # keyboard instead - no error from either side, just a login that never
    # went anywhere. Verified hide, not a blind one, before proceeding.
    _ensure_keyboard_hidden(adb, screen, note)
    _settle(0.5)

    screen = capture(adb, package)
    verify = _find(screen, starts_with="Verify")
    if not verify:
        return LoginResult(
            False,
            f"entered the code but no Verify button is on screen - real "
            f"screen was:\n{screen.render()}",
            steps,
        )
    note(f"tapping {verify.text!r}")
    Tap(index=verify.index).execute(adb, screen)

    # Verification is a real network round trip; poll rather than guess a
    # fixed wait, and stop the moment the login screen's own text is gone.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _settle(1.5)
        try:
            screen = capture(adb, package)
        except PerceptionError:
            continue
        if not _find(screen, starts_with="Welcome Back") and not _find(screen, equals="Send OTP"):
            note("no longer on the login screen - verification went through")
            return LoginResult(True, "", steps)

    return LoginResult(
        False,
        f"still on the login screen {timeout:.0f}s after tapping Verify - "
        f"the app did not complete sign-in (a known, reported issue for "
        f"fresh sessions; not something this login sequence can fix)",
        steps,
    )
