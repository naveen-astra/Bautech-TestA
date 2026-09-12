"""Turn a TestPlan into Maestro flows.

One segment becomes one flow file. The renderer is the only component that
knows Maestro syntax, and the only one that reads the screen map, so a change to
either stops here.

THE OBSERVATION PROTOCOL

A flow has to send facts back to a process that may be on another continent.
BrowserStack runs Maestro as a batch and hands back logs, so the channel that
works everywhere is stdout: every observation is one line of JSON behind a
marker.

    @@OBS {"key":"stock_before","raw":"500","anchor":true}

`observation.py` parses those lines out of `maestro.log` locally, or out of the
BrowserStack text logs in the cloud, with no other transport to arrange.

WHY THE FLOWS LOOK DEFENSIVE

A test that must prove a *negative* cannot use a bare `assertVisible`, because a
failed assertion aborts the flow and an aborted flow reports nothing. We need to
observe the absence, not die of it. So every check is written as a conditional
`runFlow` that records what it saw and keeps going. The flow reaches the end and
reports; the judging happens later, in Python, where it can be reasoned about.

This is also why reachability is recorded rather than asserted: a screen we
failed to open has to arrive at the adjudicator as a fact, so it can be called
an automation failure instead of being silently scored as a pass.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

from sentinel.schema import Capability, Segment, Step, TestPlan
from sentinel.scheduler import Wave
from sentinel.screen_map import ScreenMap

OBS_MARKER = "@@OBS"

# Anything the flow reports about itself, rather than about the app.
FLOW_MARKER = "@@FLOW"

# Companion script for otp_mode: relay logins (see render_login below).
# Maestro's JS sandbox has no sleep/timer, so this makes exactly one HTTP call
# and trusts the relay server to hold the connection open until the code
# arrives or its own timeout elapses (sentinel/otp_relay.py long-polls IMAP
# internally for this reason). Keeping the polling entirely server-side is
# what makes this reliable rather than a retry loop guessing at backoff.
FETCH_OTP_JS = """\
// Waits for a one-time code via sentinel/otp_relay.py. The relay itself
// blocks until the code arrives or TIMEOUT_MS elapses, so this makes exactly
// one request rather than polling from here.
const url = RELAY_URL + '/otp?identifier=' + encodeURIComponent(IDENTIFIER)
    + '&timeout_ms=' + TIMEOUT_MS;

const response = http.get(url, { timeout: Number(TIMEOUT_MS) + 5000 });

if (!response.ok) {
    throw new Error('OTP relay returned ' + response.status + ': ' + response.body);
}

const data = json(response.body);
if (!data.otp) {
    throw new Error('OTP relay responded without a code: ' + response.body);
}

output.otp = data.otp;
console.log('fetch_otp: received a code for ' + IDENTIFIER);
"""


class RenderError(Exception):
    """A plan could not be turned into a runnable flow."""


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _selector(spec: dict[str, Any]) -> Any:
    """Screen-map selector -> Maestro selector.

    A single `text` key is fuzzy-matched by default (see `_fuzzy_text`) rather
    than passed through as an exact-match bare string. Confirmed against a
    real device three separate times - a login tab, the bottom nav, a site
    list card - that this app's custom widgets merge several pieces of text
    into one accessibility node, which an exact match cannot see into. A
    fuzzy match costs nothing on the many labels that turn out to be clean
    ("Reports.*" still fully matches a node whose text is only "Reports"),
    so this is the safe default everywhere, not a targeted patch. Every call
    site must go through this function or `_fuzzy_text` directly, never
    reimplement the pattern inline - one line in `render_login` did exactly
    that and silently missed `_fuzzy_text`'s later start-anchoring fix as a
    result (see `_fuzzy_text`'s own docstring).
    """
    if set(spec) == {"text"}:
        return _fuzzy_text(spec["text"])
    return dict(spec)


def _emit_obs(**fields: Any) -> dict[str, Any]:
    """A command that prints one observation line.

    Built as a JS expression so the values are resolved on the device at the
    moment of capture, not baked in at render time.
    """
    parts = ", ".join(f"{json.dumps(k)}: {v}" for k, v in fields.items())
    return {"evalScript": "${console.log('" + OBS_MARKER + " ' + JSON.stringify({" + parts + "}))}"}


def _js(value: str) -> str:
    """A Python string used as a JS string literal inside an evalScript."""
    return json.dumps(value)


def _fuzzy_text(text: str) -> dict[str, str]:
    """A selector for TAPPING one specific widget whose own label may be
    merged into a longer accessibility string.

    Confirmed against a real device three separate times: a segmented-control
    tab whose real label was "Phone\\nTab 1 of 2", a bottom-nav item
    ("Sites\\nTab 1 of 4"), and a site-list card whose real label was the
    whole card's content run together ("Aqua Line\\nMumbai\\n100%\\n...Total
    expense: ₹74,113"). Flutter merges adjacent Semantics nodes into one
    accessibility string for custom widgets like these, and Maestro's text
    selector requires a full match - so a bare literal, correct as it looks,
    matches nothing.

    Anchored at the start (`text.*`), not `.*text.*` either side. In every
    confirmed case above, the widget's own label is the first thing in its
    merged string - Flutter concatenates a widget's own Semantics children in
    reading order, so this is not a coincidence. First live cloud contact
    (2026-09-03) proved why the unanchored version was actually dangerous: a
    real Samsung Galaxy S22 device had a stray OEM status-bar notification
    reading "Galaxy Themes notification: Phone personalization", and
    `.*Phone.*` matched it - Maestro tapped that instead of the app's own
    Phone tab, and the whole login flow derailed before it ever reached the
    real screen. Anchoring at the start would have excluded it (the word
    "Phone" sits mid-string there, not first) while still matching all three
    confirmed real cases above. This must never be used for "does this token
    appear anywhere on screen" - that is `_contains_text`, kept unanchored on
    purpose, for a genuinely different question.
    """
    return {"text": _escape_multiline(text) + ".*"}


def _escape_multiline(text: str) -> str:
    """`re.escape`, but safe for text containing an embedded newline.

    `re.escape` on a string with a real newline byte in it inserts a literal
    backslash immediately before that raw byte - not the two-character `\\n`
    regex metasequence a regex engine actually recognises as "newline". Real,
    live-only finding (2026-09-03): Maestro's own log printed the resulting
    selector split across two lines, which is exactly this - a backslash
    followed by a raw newline, not an escaped newline. Whether the underlying
    (Java) regex engine happens to treat that as a literal newline anyway was
    not something worth trusting; escaping each line separately and joining
    with the unambiguous `\\n` sequence removes the question entirely.
    """
    return "\\n".join(re.escape(line) for line in text.split("\n"))


def _contains_text(text: str) -> dict[str, str]:
    """A selector for SWEEPING a screen: does `text` appear anywhere at all.

    Deliberately unanchored (`.*text.*`) - unlike `_fuzzy_text`, this is not
    matching one specific widget's own label, it is asking whether a token a
    prohibition cares about leaked anywhere on screen, which could legitimately
    be embedded mid-string inside unrelated content. Anchoring this the same
    way as `_fuzzy_text` would risk real false negatives on exactly the kind
    of leak this sweep exists to catch.
    """
    return {"text": ".*" + _escape_multiline(text) + ".*"}


def _unverified_email_field() -> dict[str, Any]:
    raise RenderError(
        "login_method: email has no confirmed field selector yet - only the phone "
        "field has been verified against a real device (see docs/phase1_discovery.md). "
        "Run maestro hierarchy on the Email tab and update render_login before using it."
    )


class FlowRenderer:
    """Renders plans into Maestro flow files."""

    def __init__(
        self,
        screen_map: ScreenMap,
        personas: dict[str, Any],
        app_id: str | None = None,
        run_id: str = "LOCAL",
    ) -> None:
        self.screen_map = screen_map
        self.personas = personas
        self.run_id = run_id
        # The screen we last proved we were on, or None if anything since then
        # could have moved us. Reset at the start of every segment.
        self._anchored: str | None = None
        self.app_id = app_id or (screen_map.meta.get("app_id") or "")
        if not self.app_id:
            raise RenderError(
                "no app id. Set it in config/screen_map.yaml under meta.app_id, or pass "
                "--app-id. Find it with: adb shell pm list packages | grep -i bau"
            )

    # -- helpers ----------------------------------------------------------- #

    def _substitute(self, value: Any) -> Any:
        """Expand ${RUN_ID} so entities created by a run never collide."""
        if isinstance(value, str):
            return value.replace("${RUN_ID}", self.run_id)
        return value

    def _anchor_check(self, screen: str) -> list[dict[str, Any]]:
        """Record whether we actually arrived, without aborting if we did not.

        Skipped when we already confirmed this screen and have done nothing
        since that could have navigated away. Two reads off one screen should
        cost one anchor check, not two - across 85 cases that difference is
        minutes of device time against a two-hour budget.
        """
        if self._anchored == screen:
            return []
        self._anchored = screen
        # A fresh dict per use, not one object referenced three times below.
        # Real, live-confirmed bug: PyYAML detects repeated identical object
        # references and automatically collapses them into a YAML
        # anchor/alias (&id001 / *id001) to avoid duplicating the content -
        # confirmed in the actually-rendered flow file. The element was
        # proven present via a real captured hierarchy dump at the exact
        # moment the check ran, and still evaluated false, which points at
        # Maestro's own alias handling rather than timing or text matching -
        # both already ruled out with real evidence. Every other selector in
        # this file builds a fresh dict per use and has no such problem.
        anchor_selector = self.screen_map.anchor(screen)
        return [
            # Confirmed against a real device, twice: a screen resuming from
            # the background is not necessarily redrawn yet by the time the
            # very next command runs. The plain visibility check below is a
            # single instant look with no retry, so it can report false on a
            # screen that is genuinely there a moment later - proven both
            # times by a later step finding the identical text this check
            # just missed. A first attempt at this fix used 6000ms and still
            # missed on the very next real run, so this is deliberately more
            # generous than that measurement suggested - an engineering
            # judgment call under time pressure, not a re-measured number,
            # and worth tightening once real device time allows timing this
            # properly rather than padding it. Optional, so a genuinely
            # missing anchor still falls through to be recorded as false
            # rather than aborting the flow.
            {"extendedWaitUntil": {"visible": _selector(anchor_selector),
                                   "timeout": 15000, "optional": True}},
            # A plain visibility check only sees what is already on the
            # visible portion of the screen - confirmed live (2026-09-04):
            # the "home" anchor sat below the fold on a real populated
            # company (a scrollable sites list above it), so this check
            # reported false while a later, scrolling step found the exact
            # same text moments later. scrollUntilVisible is a safe no-op
            # cost when the anchor is already on screen, so this is the same
            # kind of default-safe generosity as the wait above, not a
            # per-screen special case.
            {"scrollUntilVisible": {"element": _selector(anchor_selector), "direction": "DOWN",
                                    "timeout": 4000, "optional": True}},
            {"evalScript": "${output.anchor = false}"},
            {
                "runFlow": {
                    "when": {"visible": _selector(anchor_selector)},
                    "commands": [{"evalScript": "${output.anchor = true}"}],
                }
            },
        ]

    def _screen_of(self, target: str) -> str | None:
        for table in (self.screen_map.controls, self.screen_map.values, self.screen_map.entities):
            if target in table:
                return table[target]["screen"]
        return target if target in self.screen_map.screens else None

    # -- capabilities ------------------------------------------------------ #

    def _render_navigate(self, step: Step) -> list[dict[str, Any]]:
        screen = step.target
        route = self.screen_map.route(screen)
        commands: list[dict[str, Any]] = []
        if route:
            if route.get("via_drawer"):
                drawer = self.screen_map.screens.get("drawer", {})
                opener = drawer.get("opened_by")
                if opener:
                    commands.append({"tapOn": _selector(opener)})
            for label in route.get("taps", []):
                commands.append({"tapOn": _fuzzy_text(label)})
        else:
            commands.append({"tapOn": _selector(self.screen_map.anchor(screen))})
        # We just moved, so whatever we last confirmed no longer holds.
        self._anchored = None
        commands.extend(self._anchor_check(screen))
        return commands

    def _render_read_value(self, step: Step) -> list[dict[str, Any]]:
        entry = self.screen_map.value(step.target)
        commands = self._anchor_check(entry["screen"])
        commands += [
            {"evalScript": "${output.raw = ''}"},
            {
                "runFlow": {
                    "when": {"visible": _selector(entry["selector"])},
                    "commands": [
                        {"copyTextFrom": _selector(entry["selector"])},
                        {"evalScript": "${output.raw = maestro.copiedText}"},
                    ],
                }
            },
            _emit_obs(
                key=_js(step.observation_key),
                raw="output.raw",
                anchor="output.anchor",
            ),
        ]
        return commands

    def _render_probe_control(self, step: Step) -> list[dict[str, Any]]:
        entry = self.screen_map.control(step.target)
        commands = self._anchor_check(entry["screen"])
        commands += [
            {"evalScript": "${output.ctl = 'absent'}"},
            {
                "runFlow": {
                    "when": {"visible": _selector(entry["selector"])},
                    "commands": [{"evalScript": "${output.ctl = 'present'}"}],
                }
            },
            _emit_obs(
                key=_js(step.observation_key),
                raw="output.ctl",
                anchor="output.anchor",
            ),
        ]
        return commands

    def _render_capture_screen_text(self, step: Step) -> list[dict[str, Any]]:
        """Sweep a screen for the tokens a prohibition cares about.

        Maestro cannot dump a whole screen, so we search for the specific tokens
        the assertion names - scrolling first, because a token below the fold is
        not the same as a token that is not there.
        """
        screen = self._screen_of(step.target) or step.target
        tokens = [str(t) for t in (step.args.get("tokens") or "").split("|") if t]
        commands = self._anchor_check(screen)
        commands.append({"evalScript": "${output.seen = []}"})

        for token in tokens:
            resolved = self._substitute(token)
            commands += [
                {
                    "scrollUntilVisible": {
                        "element": _contains_text(resolved),
                        "direction": "DOWN",
                        "timeout": 4000,
                        # A token this sweep is looking for is often
                        # genuinely absent - that is the whole point of a
                        # prohibition check - so failing to scroll to it
                        # must not abort the flow. `optional` has to sit
                        # inside the command's own block, not beside it, or
                        # Maestro rejects the flow as malformed before a
                        # single step runs.
                        "optional": True,
                    },
                },
                {
                    "runFlow": {
                        "when": {"visible": _contains_text(resolved)},
                        "commands": [
                            {"evalScript": "${output.seen.push(" + _js(resolved) + ")}"}
                        ],
                    }
                },
            ]

        commands.append(
            _emit_obs(
                key=_js(step.observation_key),
                raw=_js(""),
                screen_texts="output.seen",
                anchor="output.anchor",
            )
        )
        return commands

    def _render_attempt_action(self, step: Step) -> list[dict[str, Any]]:
        """Deliberately try something the case says must be refused.

        The outcome has to distinguish four things: it worked, it was refused,
        nothing happened, or we never managed to try. Guessing between them is
        exactly the mistake this whole system exists to avoid, so each is
        detected separately and reported as observed.
        """
        entry = self.screen_map.control(step.target)
        selector = _selector(entry["selector"])
        markers = self.screen_map.refusal_markers
        commands = self._anchor_check(entry["screen"])
        commands += [
            {"evalScript": "${output.outcome = 'stalled'}"},
            {"evalScript": "${output.seen = []}"},
            {
                "runFlow": {
                    "when": {"visible": selector},
                    "commands": [
                        {"tapOn": selector},
                        # Reaching here means the tap landed; what follows tells
                        # us whether the app then stopped us.
                        {"evalScript": "${output.outcome = 'succeeded'}"},
                    ],
                }
            },
        ]

        for marker in markers:
            commands.append(
                {
                    "runFlow": {
                        "when": {"visible": {"text": f"(?i).*{re.escape(marker)}.*"}},
                        "commands": [
                            {"evalScript": "${output.outcome = 'refused'}"},
                            {"evalScript": "${output.seen.push(" + _js(marker) + ")}"},
                        ],
                    }
                }
            )

        commands += [
            {"takeScreenshot": f"{step.observation_key}-attempt"},
            _emit_obs(
                key=_js(step.observation_key),
                raw=_js(""),
                outcome="output.outcome",
                screen_texts="output.seen",
                anchor="output.anchor",
            ),
        ]
        return commands

    def _render_create_entity(self, step: Step) -> list[dict[str, Any]]:
        entity = self.screen_map.entity(step.target)
        commands: list[dict[str, Any]] = [{"tapOn": _selector(entity["open_create"])}]
        for field, value in step.args.items():
            spec = (entity.get("fields") or {}).get(field)
            if spec is None:
                raise RenderError(
                    f"entity {step.target!r} has no field {field!r}; "
                    f"known fields: {', '.join(entity.get('fields') or {})}"
                )
            commands += [
                {"tapOn": _selector(spec["selector"])},
                {"inputText": str(self._substitute(value))},
            ]
        commands.append({"tapOn": _selector(entity["submit"])})
        return commands

    def _render_simple_tap(self, step: Step, label: str) -> list[dict[str, Any]]:
        entry = self.screen_map.controls.get(step.target)
        selector = (
            _selector(entry["selector"])
            if entry
            else _selector({"text": self._substitute(step.target) or label})
        )
        return [{"tapOn": selector}]

    # -- step dispatch ----------------------------------------------------- #

    # Capabilities that act on the app and so may leave the current screen.
    # After one of these, any earlier anchor confirmation is stale.
    _NAVIGATING = frozenset(
        {
            Capability.CREATE_ENTITY,
            Capability.EDIT_ENTITY,
            Capability.DELETE_ENTITY,
            Capability.OPEN_ENTITY,
            Capability.SUBMIT,
            Capability.APPROVE,
            Capability.REJECT,
            Capability.ATTEMPT_ACTION,
            Capability.LOGIN,
            Capability.LOGOUT,
        }
    )

    def render_step(self, step: Step) -> list[dict[str, Any]]:
        if step.capability in self._NAVIGATING:
            self._anchored = None

        match step.capability:
            case Capability.NAVIGATE:
                commands = self._render_navigate(step)
            case Capability.READ_VALUE:
                commands = self._render_read_value(step)
            case Capability.PROBE_CONTROL:
                commands = self._render_probe_control(step)
            case Capability.CAPTURE_SCREEN_TEXT:
                commands = self._render_capture_screen_text(step)
            case Capability.ATTEMPT_ACTION:
                commands = self._render_attempt_action(step)
            case Capability.CREATE_ENTITY:
                commands = self._render_create_entity(step)
            case Capability.INPUT_VALUE:
                commands = [
                    {"tapOn": _selector({"text": step.target})},
                    {"inputText": str(self._substitute(step.args.get("value", "")))},
                ]
            case Capability.SEARCH:
                commands = [{"inputText": str(self._substitute(step.args.get("value", "")))}]
            case Capability.SCREENSHOT:
                commands = [{"takeScreenshot": _slug(step.label or step.target or "shot")}]
            case Capability.SUBMIT:
                commands = self._render_simple_tap(step, "Save")
            case Capability.APPROVE:
                commands = self._render_simple_tap(step, "Approve")
            case Capability.REJECT:
                commands = self._render_simple_tap(step, "Reject")
            case Capability.OPEN_ENTITY:
                # Not directly confirmed whether a list card's name sits in
                # its own accessibility node or is merged into the whole
                # card's text (list cards showed this merging pattern
                # elsewhere in this app) - fuzzy by default rather than an
                # unverified exact-match guess either way.
                name = self._substitute(step.args.get("name", step.target))
                commands = [{"tapOn": _fuzzy_text(str(name))}]
            case Capability.EDIT_ENTITY | Capability.DELETE_ENTITY:
                commands = self._render_simple_tap(step, "Edit")
            case Capability.LOGIN:
                commands = self.render_login(str(step.args.get("persona", "")))
            case Capability.LOGOUT:
                commands = [{"tapOn": "Log out"}]
            case _:  # pragma: no cover
                raise RenderError(f"no renderer for capability {step.capability}")

        if step.capability in self._NAVIGATING:
            # Also stale *after* the action: the tap may have opened a form or
            # bounced us back to a list.
            self._anchored = None

        if step.label and commands:
            # label belongs INSIDE the command's own value block, not beside
            # the command name - the exact same class of mistake this file
            # already documents for `optional` (see _render_capture_screen_text).
            # Real, live consequence this time: {"tapOn": {...}, "label": "..."}
            # is not valid Maestro syntax and aborted the whole flow before a
            # single step ran, confirmed by a real "Invalid Command Format"
            # error on real hardware. Only merged when the command's own value
            # is already a dict (true for every real caller today - NAVIGATE's
            # first command is always a selector dict); left alone otherwise
            # rather than guess an unverified shorthand-to-dict conversion for
            # commands (evalScript, hideKeyboard, ...) that may not even
            # support a label the same way.
            first = commands[0]
            if len(first) == 1:
                (command_name, value), = first.items()
                if isinstance(value, dict):
                    commands = [{command_name: {**value, "label": step.label}}] + commands[1:]
        return commands

    # -- login ------------------------------------------------------------- #

    def render_login(self, persona: str) -> list[dict[str, Any]]:
        """Sign in as one persona, obtaining the OTP by the configured route."""
        defaults = self.personas.get("defaults", {})
        config = (self.personas.get("personas") or {}).get(persona)
        if config is None:
            raise RenderError(f"no login configured for persona {persona!r}")

        mode = config.get("otp_mode", defaults.get("otp_mode", "fixed"))
        identifier = "${" + config["identifier_env"] + "}"
        by_phone = config.get("login_method", "phone") == "phone"

        commands: list[dict[str, Any]] = [
            # A real BrowserStack cloud device (2026-09-03) proved every
            # attempt before this one was failing for a reason that had
            # nothing to do with the Phone tab selector at all: a genuinely
            # fresh install shows a one-time "Select Your Language" screen
            # (English/Hindi/Gujarati, "Continue") before the login screen
            # ever appears. This was never seen on the local physical phone,
            # because that install had already completed this step once and
            # it stuck - so render_login() never accounted for it. Confirmed
            # by fetching the real screenshot Maestro captured at the exact
            # failure point, not guessed. English is already the pre-checked
            # default, so tapping Continue needs no prior selection.
            #
            # A bare `runFlow: when: visible:` is a single instant look, no
            # retry - exactly the bug _anchor_check documents for a screen
            # resuming from the background: it can report false on a screen
            # that is genuinely there a moment later. Real consequence, live
            # cloud contact: this exact check fired before the cold-started
            # Flutter engine had finished rendering the language screen,
            # evaluated false, skipped the Continue tap - and by the time
            # the *next* command ran a few seconds later, the language screen
            # had finished rendering after all, so that command searched for
            # the Phone tab on a screen that was still "Select Your
            # Language". Fixed the same way _anchor_check already proved
            # works: an `optional` extendedWaitUntil polls for the text
            # first (never fails the flow either way), then the `runFlow`
            # check - now checking a screen that has had time to settle -
            # decides whether to tap Continue.
            #
            # 8000ms was not generous enough either - a later BrowserStack
            # device allocation proved it directly: a real captured video
            # frame at +13.7s into the app's cold start showed a *blank white
            # screen*, still mid Flutter-engine-initialisation, well past
            # this wait's original ceiling. Device pool variance on shared
            # cloud hardware is evidently wide enough that a number tuned
            # against one allocation can fail on the next. Raised to match
            # the same order of magnitude already used for the equally
            # real-evidence-driven anchor waits elsewhere in this file
            # (_anchor_check's 15000ms, the post-login home-anchor's 20000ms)
            # rather than re-guess a tighter number from one more sample.
            {"extendedWaitUntil": {"visible": {"text": "Select Your Language"},
                                   "timeout": 20000, "optional": True}},
            {
                "runFlow": {
                    "when": {"visible": {"text": "Select Your Language"}},
                    "commands": [{"tapOn": "Continue"}],
                },
            },
            # Confirmed against a real device: this tab's actual accessibility
            # text is "Phone\nTab 1 of 2" - Flutter merges the tab-position
            # hint into the label for this segmented-control widget. Maestro's
            # text selector requires a full match, so the bare word alone
            # matches nothing.
            #
            # Routed through _fuzzy_text() rather than a hand-written regex -
            # this exact line used to read {"text": ".*Phone.*" ...}, written
            # before _fuzzy_text existed as a shared helper, and it silently
            # never picked up _fuzzy_text's later start-anchoring fix because
            # it duplicated the pattern instead of calling the function. Real
            # consequence, seen on live cloud hardware before this was caught:
            # a Samsung Galaxy S22's stray OEM status-bar notification
            # ("Galaxy Themes notification: Phone personalization") matched
            # the unanchored regex and got tapped instead of this tab.
            #
            # Anchoring at the start alone was not enough, either - the very
            # next live run proved a *second* status-bar element collides:
            # the signal-strength icon's own accessibility text is literally
            # "Phone signal full.", which also starts with "Phone". Android's
            # status bar is evidently not excluded from Maestro's search at
            # all, so any selector built from "Phone" alone is fragile on
            # real hardware regardless of anchoring. Matching the confirmed
            # real merged string all the way through the newline
            # ("Phone\nTab 1 of 2") is what actually rules both out - no
            # status-bar element plausibly contains "Phone" immediately
            # followed by a literal newline and "Tab".
            {"tapOn": _fuzzy_text("Phone\nTab" if by_phone else "Email\nTab")},
            # Selecting the Phone/Email tab does not focus the input field
            # beneath it - confirmed against a real device, where inputText
            # sent with nothing focused typed into empty air and the flow
            # silently stayed on this screen. The field is a bare Flutter
            # EditText with no resource-id, so its hint text (visible only
            # while empty, which it always is at this point) is what Maestro
            # can actually tap on. Phone's hint is confirmed against a real
            # device; email's is not - login_method: email is untested, and
            # reusing the "Email" tab label here would hit this exact bug
            # again rather than fix it, so this raises instead of guessing.
            {"tapOn": "Enter 10-digit number"} if by_phone else _unverified_email_field(),
            {"inputText": identifier},
            # Confirmed against a real device: entering text opens the soft
            # keyboard, which covers "Send OTP" at the bottom of the screen.
            # tapOn resolves through the accessibility tree, not pixels, so it
            # reported success while the physical touch actually landed on
            # the keyboard underneath - the flow silently never left this
            # screen. Dismissing the keyboard first is what actually reveals
            # the button to tap.
            "hideKeyboard",
            {"tapOn": "Send OTP"},
        ]

        if mode == "fixed":
            # A Firebase test number: the code is constant and no SMS is sent.
            commands.append({"inputText": "${" + config["otp_env"] + "}"})
        elif mode == "relay":
            relay = "${" + defaults.get("relay_url_env", "SENTINEL_OTP_RELAY_URL") + "}"
            timeout = int(defaults.get("otp_timeout_ms", 30000))
            commands += [
                {
                    "runScript": {
                        "file": "fetch_otp.js",
                        "env": {"RELAY_URL": relay, "IDENTIFIER": identifier,
                                "TIMEOUT_MS": str(timeout)},
                    }
                },
                {"inputText": "${output.otp}"},
            ]
        else:
            raise RenderError(f"unknown otp_mode {mode!r} for persona {persona!r}")

        commands.append("hideKeyboard")
        # The real label is "Verify OTP", not "Verify" - confirmed directly
        # on the physical phone (2026-09-04), not guessed. BrowserStack live
        # contact had this tap searching its full timeout and finding
        # nothing, every time, across every fix tried for the steps before
        # it - a diagnostic screenshot proved unreliable (BrowserStack's
        # maestroScreenshot artifact returned the same stale first-launch
        # frame regardless of when it was requested, confirmed by comparing
        # two screenshots taken at genuinely different points in the same
        # run), so the real answer came from manually replaying the exact
        # same phone number and OTP on the connected physical device: typing
        # the OTP and tapping the real "Verify OTP" button worked
        # end-to-end, landing on a real post-login screen. "Verify" was
        # simply never going to match - Maestro requires a full match, and
        # this is the one label in the whole flow that turned out to need a
        # second word, not a merge or a selector strategy problem.
        commands.append({"tapOn": "Verify OTP"})
        commands.append(
            {"extendedWaitUntil": {"visible": _selector(self.screen_map.anchor("home")),
                                   "timeout": 20000}}
        )
        return commands

    # -- flows ------------------------------------------------------------- #

    def render_segment(self, plan: TestPlan, segment: Segment, index: int) -> str:
        """One segment -> one Maestro flow document."""
        # Each flow starts from a cold app, so nothing is confirmed yet.
        self._anchored = None

        header = {
            "appId": self.app_id,
            "name": f"{plan.case_id} [{index}] {segment.persona}",
            "properties": {
                "testCaseId": plan.case_id,
                "junitId": f"{plan.case_id}-{index}",
                "junitClassname": f"sentinel.{_slug(segment.persona)}",
                "persona": segment.persona,
                "runId": self.run_id,
            },
        }

        commands: list[dict[str, Any]] = [
            {"evalScript": "${console.log('" + FLOW_MARKER + " start " + plan.case_id + "')}"},
            # clearState: true, not false. Real, live finding: a device that
            # had been logged into a *different* account earlier the same
            # day (manual testing, a different persona, even the phone
            # owner's own account) resumes that session on launch when state
            # is preserved - the whole login flow then runs against a screen
            # that was never the login screen at all, and every subsequent
            # tap fails "correctly", for a reason that has nothing to do with
            # selectors. This is exactly the class of thing the project's own
            # test-isolation principle exists to prevent: a repeatable run
            # cannot depend on what state a device happened to be left in.
            # A fresh install shows the one-time language screen either way,
            # and render_login already waits for that - clearing state does
            # not add a step the flow was not already handling.
            {"launchApp": {"appId": self.app_id, "clearState": True}},
        ]

        explicit_login = any(s.capability is Capability.LOGIN for s in segment.steps)
        if not explicit_login:
            commands += self.render_login(segment.persona)

        for step in segment.steps:
            commands += self.render_step(step)

        commands.append({"takeScreenshot": f"{plan.case_id}-{index}-final"})
        commands.append(
            {"evalScript": "${console.log('" + FLOW_MARKER + " end " + plan.case_id + "')}"}
        )

        return (
            yaml.safe_dump(header, sort_keys=False, default_flow_style=False)
            + "---\n"
            + yaml.safe_dump(commands, sort_keys=False, default_flow_style=False)
        )

    def render_plan(self, plan: TestPlan, out_dir: str | Path) -> list[Path]:
        """Write every segment of a plan. Returns the files written."""
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        for index, segment in enumerate(plan.segments):
            path = out_dir / f"{plan.case_id}-{index}-{_slug(segment.persona)}.yaml"
            path.write_text(self.render_segment(plan, segment, index), encoding="utf-8")
            written.append(path)

        # runScript resolves its file relative to the flow, so any relay login
        # needs its own copy alongside the flows that reference it. Cheap and
        # idempotent - writing it whether or not this plan actually uses relay
        # mode is simpler than tracking usage, and a stray copy costs nothing.
        default_mode = self.personas.get("defaults", {}).get("otp_mode", "fixed")
        if any(
            self.personas.get("personas", {}).get(p, {}).get("otp_mode", default_mode)
            == "relay"
            for p in plan.personas
        ):
            (out_dir / "fetch_otp.js").write_text(FETCH_OTP_JS, encoding="utf-8")

        return written

    def render_scheduled(
        self, waves: list[Wave], out_dir: str | Path
    ) -> tuple[list[Path], list[tuple[str, str, str]]]:
        """Render segments in the order `scheduler.schedule()` computed, not sheet order.

        `render_plan` renders every segment of one plan together, which is
        the wrong unit here: the scheduler batches segments from *different*
        plans into a wave, and a single cross-persona plan (TC-032's Engineer
        segment, then its later Admin segment) can legitimately have its own
        segments land in two different waves. So this works per segment, via
        the same `render_segment` every other path already uses - nothing
        about Maestro syntax changes, only which order files are written in.

        That order matters because neither backend takes an explicit run
        order: `LocalBackend.run` hands Maestro the whole directory and lets
        it walk it, and `BrowserstackBackend` calls
        `sorted(Path(flow_dir).rglob("*.yaml"))` before zipping. Both
        therefore execute in directory-sort order - so a wave-numbered
        filename prefix (`w000-...`, `w001-...`) is what actually makes the
        scheduler's persona-batching reach either backend. Nothing
        downstream needs to know a schedule exists.

        WHAT THIS DOES NOT DO. Each segment is still its own flow file with
        its own `launchApp(clearState: true)` and its own login at the top,
        exactly as `render_segment` always has - this does not merge a
        wave's segments behind one shared login, which is the change that
        would actually collapse login count. That is a change to
        `render_segment`'s own flow model (today it assumes a fresh app and
        resets `self._anchored` at the top of every call), and it is
        deliberately not made here without device time to confirm it does
        not disturb the anchor-tracking the rest of this file depends on.
        What this buys on its own: flows run in persona-batched order rather
        than sheet order, which is real and cost nothing to get wrong.

        Returns the files written, and `(case_id, persona, error)` for any
        segment that failed to render - the caller reports those as
        `BLOCKED` per plan, same as `render_plan`'s failure path.
        """
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        failures: list[tuple[str, str, str]] = []
        relay_needed = False
        default_mode = self.personas.get("defaults", {}).get("otp_mode", "fixed")

        for wave in waves:
            for item in wave.items:
                persona = item.segment.persona
                try:
                    text = self.render_segment(item.plan, item.segment, item.segment_index)
                except Exception as exc:
                    # Broad on purpose, matching render_plan's own caller in
                    # run.py: an unverified screen-map target raises
                    # screen_map.UnverifiedTarget, not RenderError, and one
                    # bad plan must become a BLOCKED entry for that plan
                    # alone, never an unhandled crash of the whole run.
                    failures.append((item.plan.case_id, persona, str(exc)))
                    continue

                name = (
                    f"w{item.wave_index:03d}-{_slug(persona)}-"
                    f"{item.plan.case_id}-{item.segment_index}.yaml"
                )
                path = out_dir / name
                path.write_text(text, encoding="utf-8")
                written.append(path)

                mode = self.personas.get("personas", {}).get(persona, {}).get(
                    "otp_mode", default_mode
                )
                if mode == "relay":
                    relay_needed = True

        if relay_needed:
            (out_dir / "fetch_otp.js").write_text(FETCH_OTP_JS, encoding="utf-8")

        return written, failures
