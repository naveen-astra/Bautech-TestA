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
from sentinel.screen_map import ScreenMap

OBS_MARKER = "@@OBS"

# Anything the flow reports about itself, rather than about the app.
FLOW_MARKER = "@@FLOW"

class RenderError(Exception):
    """A plan could not be turned into a runnable flow."""


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _selector(spec: dict[str, Any]) -> Any:
    """Screen-map selector -> Maestro selector.

    A single `text` key becomes a bare string, which is what Maestro flows
    normally use and what a human will expect to read.
    """
    if set(spec) == {"text"}:
        return spec["text"]
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
        anchor = _selector(self.screen_map.anchor(screen))
        return [
            {"evalScript": "${output.anchor = false}"},
            {
                "runFlow": {
                    "when": {"visible": anchor},
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
                commands.append({"tapOn": label})
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
                        "element": {"text": resolved},
                        "direction": "DOWN",
                        "timeout": 4000,
                    },
                    "optional": True,
                },
                {
                    "runFlow": {
                        "when": {"visible": resolved},
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
                commands = [{"tapOn": self._substitute(step.args.get("name", step.target))}]
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

        if step.label:
            commands = [{**c, "label": step.label} if len(c) == 1 else c for c in commands[:1]] + \
                commands[1:]
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
            # Confirmed against a real device: this tab's actual accessibility
            # text is "Phone\nTab 1 of 2" - Flutter merges the tab-position
            # hint into the label for this segmented-control widget. Maestro's
            # text selector requires a full match, so the bare word alone
            # matches nothing; ".*" either side absorbs the merged text.
            {"tapOn": {"text": ".*Phone.*" if by_phone else ".*Email.*"}},
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
        commands.append({"tapOn": "Verify"})
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
            {"launchApp": {"appId": self.app_id, "clearState": False}},
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

        return written
