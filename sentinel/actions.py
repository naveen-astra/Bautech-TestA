"""Everything the agent is allowed to do, and how each one reaches the device.

Two rules shape this module.

**Actions aim at an element index, never at a text pattern.** The index comes
from the numbered screen `perception.capture()` just produced, so the target's
exact bounds are already known and no selector is ever constructed. This is
what makes the agent immune to the selector-ambiguity failures that dominated
earlier work here.

**The agent records observations; it does not do arithmetic on them.** The
`note` action stores a raw value under a key, and the existing deterministic
verifier compares before against after. Keeping the model out of the
comparison is deliberate: a test that says "stock increases by 100" must be
settled by subtraction, not by an opinion about whether the screen looked
right.

The tool schema handed to the model is generated from these same classes, so
the description the model reads and the code that runs cannot drift apart.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

from sentinel.perception import Element, PerceptionError, Screen


class ActionError(Exception):
    """The action could not be carried out on the device."""


def _shell(adb: str, *args: str, timeout: int = 30) -> str:
    result = subprocess.run(
        [adb, "shell", *args], capture_output=True, text=True, timeout=timeout
    )
    if result.returncode != 0:
        raise ActionError(f"adb shell {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _escape(text: str) -> str:
    """`adb shell input text` needs spaces as %s and shell metacharacters gone."""
    out = []
    for char in text:
        if char == " ":
            out.append("%s")
        elif char in "()<>|;&*\\~\"'`$":
            out.append("\\" + char)
        else:
            out.append(char)
    return "".join(out)


# --------------------------------------------------------------------------- #
# The action vocabulary.


@dataclass
class Action:
    """One decision the agent made."""

    name: str = ""
    terminal: bool = False

    def describe(self) -> str:  # pragma: no cover - overridden
        return self.name

    def execute(self, adb: str, screen: Screen) -> str:
        raise NotImplementedError


@dataclass
class Tap(Action):
    index: int = 0
    name: str = "tap"

    def describe(self) -> str:
        return f"tap element {self.index}"

    def execute(self, adb: str, screen: Screen) -> str:
        element = screen.get(self.index)
        x, y = element.center
        _shell(adb, "input", "tap", str(x), str(y))
        time.sleep(1.2)  # let the UI react before the next observation
        return f"tapped {element.text or 'unlabelled element'} at ({x},{y})"


@dataclass
class TypeText(Action):
    text: str = ""
    index: int | None = None
    name: str = "type_text"

    def describe(self) -> str:
        where = f" into element {self.index}" if self.index is not None else ""
        return f"type {self.text!r}{where}"

    def execute(self, adb: str, screen: Screen) -> str:
        note = ""
        if self.index is not None:
            element = screen.get(self.index)
            x, y = element.center
            _shell(adb, "input", "tap", str(x), str(y))
            time.sleep(0.8)
            note = f" (focused {element.text or 'field'} first)"
        _shell(adb, "input", "text", _escape(self.text))
        time.sleep(0.6)
        return f"typed {self.text!r}{note}"


@dataclass
class Scroll(Action):
    direction: str = "down"
    name: str = "scroll"

    def describe(self) -> str:
        return f"scroll {self.direction}"

    def execute(self, adb: str, screen: Screen) -> str:
        width = screen.width or 1080
        height = screen.height or 1920
        mid = width // 2
        if self.direction == "down":
            start, end = int(height * 0.72), int(height * 0.28)
        elif self.direction == "up":
            start, end = int(height * 0.28), int(height * 0.72)
        else:
            raise ActionError(f"unknown scroll direction {self.direction!r}")
        _shell(adb, "input", "swipe", str(mid), str(start), str(mid), str(end), "400")
        time.sleep(1.0)
        return f"scrolled {self.direction}"


@dataclass
class Back(Action):
    name: str = "back"

    def describe(self) -> str:
        return "press back"

    def execute(self, adb: str, screen: Screen) -> str:
        _shell(adb, "input", "keyevent", "4")
        time.sleep(1.2)
        return "pressed back"


@dataclass
class HideKeyboard(Action):
    name: str = "hide_keyboard"

    def describe(self) -> str:
        return "hide the keyboard"

    def execute(self, adb: str, screen: Screen) -> str:
        # Confirmed the hard way on real hardware: the soft keyboard covers
        # buttons at the bottom of the screen, and a tap resolved through the
        # accessibility tree reports success while the real touch lands on a
        # key instead.
        _shell(adb, "input", "keyevent", "111")  # ESC closes the IME
        time.sleep(0.6)
        return "hid the keyboard"


@dataclass
class Note(Action):
    key: str = ""
    value: str = ""
    name: str = "note"

    def describe(self) -> str:
        return f"note {self.key}={self.value!r}"

    def execute(self, adb: str, screen: Screen) -> str:
        return f"recorded {self.key} = {self.value!r}"


@dataclass
class Finish(Action):
    summary: str = ""
    reached_target_screen: bool = False
    name: str = "finish"
    terminal: bool = True

    def describe(self) -> str:
        return f"finish: {self.summary}"

    def execute(self, adb: str, screen: Screen) -> str:
        return self.summary


@dataclass
class GiveUp(Action):
    reason: str = ""
    name: str = "give_up"
    terminal: bool = True

    def describe(self) -> str:
        return f"give up: {self.reason}"

    def execute(self, adb: str, screen: Screen) -> str:
        return self.reason


_REGISTRY: dict[str, type[Action]] = {
    "tap": Tap,
    "type_text": TypeText,
    "scroll": Scroll,
    "back": Back,
    "hide_keyboard": HideKeyboard,
    "note": Note,
    "finish": Finish,
    "give_up": GiveUp,
}


def build(name: str, arguments: dict[str, Any]) -> Action:
    """Turn the model's tool call into a real action, or refuse clearly."""
    cls = _REGISTRY.get(name)
    if cls is None:
        raise ActionError(f"unknown action {name!r}; valid actions: {sorted(_REGISTRY)}")
    allowed = {f for f in cls.__dataclass_fields__ if f not in ("name", "terminal")}
    unexpected = set(arguments) - allowed
    if unexpected:
        raise ActionError(f"{name} got unexpected argument(s): {sorted(unexpected)}")
    return cls(**arguments)


TOOL_SCHEMA: list[dict[str, Any]] = [
    {
        "name": "tap",
        "description": (
            "Tap one element, addressed by the index shown in the screen listing. "
            "Use this for buttons, tabs, list rows and links."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "index": {"type": "integer", "description": "index from the screen listing"},
            },
            "required": ["index"],
        },
    },
    {
        "name": "type_text",
        "description": (
            "Type text. Pass `index` to focus that input field first, which is "
            "almost always what you want - typing with nothing focused goes nowhere."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "index": {"type": "integer", "description": "input field to focus first"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "scroll",
        "description": (
            "Scroll the screen. Use this before concluding something is absent - "
            "a control below the fold is not the same as a control that is missing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"direction": {"type": "string", "enum": ["down", "up"]}},
            "required": ["direction"],
        },
    },
    {
        "name": "back",
        "description": "Press the Android back button.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "hide_keyboard",
        "description": (
            "Dismiss the soft keyboard. Do this after typing and before tapping a "
            "button near the bottom of the screen, which the keyboard covers."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "note",
        "description": (
            "Record a value you read off the screen, so it can be checked later. "
            "Record the raw text exactly as shown - do not do arithmetic on it and "
            "do not decide whether it is correct. For a before/after test, note the "
            "value before you act and again after, using keys like "
            "'stock_before' and 'stock_after'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "value": {"type": "string", "description": "raw text, exactly as displayed"},
            },
            "required": ["key", "value"],
        },
    },
    {
        "name": "finish",
        "description": (
            "The steps of the test case are complete. Say what you did and what you "
            "saw. Set reached_target_screen to true only if you actually confirmed "
            "you were on the screen the case is about - if you never got there, say "
            "false, because a conclusion drawn from the wrong screen is worthless."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "reached_target_screen": {"type": "boolean"},
            },
            "required": ["summary", "reached_target_screen"],
        },
    },
    {
        "name": "give_up",
        "description": (
            "You are stuck and cannot carry out the case - lost, blocked by "
            "something unrelated, or the app is not responding. Say precisely what "
            "stopped you. This is honest and useful; guessing is not."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
    },
]
