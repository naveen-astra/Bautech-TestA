"""What the agent can actually see: one real screen, as a short numbered list.

This is the agent's eyes. Everything it decides is decided from what this
module returns, so two properties matter more than anything else here:

*   **Only the app under test.** Every node is filtered to the package being
    tested. This is not tidiness - it is a fix for a real, expensive class of
    bug. A text selector searching the whole device hierarchy matched a
    Samsung status-bar notification ("Galaxy Themes notification: Phone
    personalization") and then, after that was fixed, the signal-strength
    icon ("Phone signal full."), each time tapping system UI instead of the
    app and derailing the run. An agent that cannot see the status bar
    cannot tap it.

*   **Elements are addressed by index, not by text.** The agent says "tap 14"
    and we already hold element 14's exact bounds. No regex is constructed,
    so no regex can be ambiguous, over-match, or need anchoring. The whole
    fuzzy-matching problem - merged Flutter accessibility strings, escaped
    newlines, start-anchoring - simply does not arise at this layer. Text is
    still carried, because the recorded trace needs it to replay as a
    Maestro flow later, but it is never how an action is aimed.

The rendered form is deliberately terse. It goes into an LLM prompt on every
single step of every case, so the difference between raw XML (~10KB for a
simple screen) and this (~600 bytes) is the difference between an affordable
run and an unaffordable one.
"""

from __future__ import annotations

import re
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")

# Classes that carry no information on their own. A node of one of these with
# no text, no description and no interactivity is pure layout scaffolding.
_STRUCTURAL = {
    "android.widget.FrameLayout",
    "android.widget.LinearLayout",
    "android.widget.RelativeLayout",
    "android.view.ViewGroup",
    "androidx.compose.ui.platform.ComposeView",
}


class PerceptionError(Exception):
    """The screen could not be read at all."""


@dataclass
class Element:
    """One thing on screen the agent may look at or act on."""

    index: int
    text: str
    role: str
    clickable: bool
    focused: bool
    scrollable: bool
    bounds: tuple[int, int, int, int]
    klass: str = ""

    @property
    def center(self) -> tuple[int, int]:
        left, top, right, bottom = self.bounds
        return ((left + right) // 2, (top + bottom) // 2)

    def render(self) -> str:
        marks = []
        if self.clickable:
            marks.append("tappable")
        if self.focused:
            marks.append("focused")
        if self.scrollable:
            marks.append("scrollable")
        suffix = f"  [{', '.join(marks)}]" if marks else ""
        label = self.text if self.text else "(no label)"
        return f"{self.index:>3}. {self.role:<6} {label!r}{suffix}"


@dataclass
class Screen:
    """Everything the agent can see right now, and nothing it cannot."""

    elements: list[Element] = field(default_factory=list)
    package: str = ""
    width: int = 0
    height: int = 0

    def render(self) -> str:
        if not self.elements:
            return "(the app is showing nothing readable - it may still be loading)"
        return "\n".join(element.render() for element in self.elements)

    def get(self, index: int) -> Element:
        for element in self.elements:
            if element.index == index:
                return element
        raise PerceptionError(
            f"no element {index} on screen; visible indexes are "
            f"{[e.index for e in self.elements]}"
        )

    @property
    def texts(self) -> list[str]:
        return [e.text for e in self.elements if e.text]

    def contains(self, needle: str) -> bool:
        """Case-insensitive substring search across everything visible.

        The agent uses this for absence questions, and so does the
        adjudicator - "is this token anywhere on this screen" is a different
        question from "is this one widget present", and conflating them is
        how a prohibition check turns into a false pass.
        """
        lowered = needle.lower()
        return any(lowered in text.lower() for text in self.texts)


def _role_of(klass: str, clickable: bool, text: str) -> str:
    if "EditText" in klass:
        return "INPUT"
    if "Button" in klass or clickable:
        return "BUTTON"
    if "Image" in klass:
        return "IMAGE"
    if text:
        return "TEXT"
    return "VIEW"


def _parse(xml_text: str, package: str) -> Screen:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise PerceptionError(f"hierarchy was not valid XML: {exc}") from exc

    screen = Screen(package=package)
    index = 1

    for node in root.iter("node"):
        attrs = node.attrib

        # The package filter: the single most important line in this file.
        if package and attrs.get("package") != package:
            continue

        text = (attrs.get("text") or "").strip()
        desc = (attrs.get("content-desc") or "").strip()
        hint = (attrs.get("hintText") or "").strip()
        label = text or desc or hint

        clickable = attrs.get("clickable") == "true"
        scrollable = attrs.get("scrollable") == "true"
        focused = attrs.get("focused") == "true"
        klass = attrs.get("class", "")

        # Layout scaffolding with nothing to say and nothing to do.
        if not label and not clickable and not scrollable and klass in _STRUCTURAL:
            continue
        # Anything else with no label and no interactivity is noise too.
        if not label and not clickable and not scrollable:
            continue

        match = _BOUNDS.match(attrs.get("bounds", ""))
        if not match:
            continue
        left, top, right, bottom = (int(g) for g in match.groups())
        if right <= left or bottom <= top:
            continue  # zero-area, cannot be seen or tapped

        screen.elements.append(
            Element(
                index=index,
                text=label,
                role=_role_of(klass, clickable, label),
                clickable=clickable,
                focused=focused,
                scrollable=scrollable,
                bounds=(left, top, right, bottom),
                klass=klass,
            )
        )
        index += 1

    if screen.elements:
        screen.width = max(e.bounds[2] for e in screen.elements)
        screen.height = max(e.bounds[3] for e in screen.elements)
    return screen


def capture(adb: str, package: str, attempts: int = 3) -> Screen:
    """Read the live screen.

    Retries because `uiautomator dump` genuinely fails while the screen is
    animating - it reports "could not get idle state" rather than returning
    a partial tree, so a retry is the correct response rather than a
    workaround.
    """
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            dump = subprocess.run(
                [adb, "exec-out", "uiautomator", "dump", "/dev/tty"],
                capture_output=True, timeout=30,
            )
            xml_text = dump.stdout.decode("utf-8", errors="replace")
            start = xml_text.find("<?xml")
            if start >= 0:
                xml_text = xml_text[start:]
            end = xml_text.rfind("</hierarchy>")
            if end >= 0:
                xml_text = xml_text[: end + len("</hierarchy>")]
            if "<hierarchy" not in xml_text:
                raise PerceptionError(f"no hierarchy in dump output: {xml_text[:200]!r}")
            return _parse(xml_text, package)
        except (PerceptionError, subprocess.SubprocessError, OSError) as exc:
            last = exc
            time.sleep(1.0 + attempt)
    raise PerceptionError(f"could not read the screen after {attempts} attempts: {last}")


def capture_from_file(path: str | Path, package: str) -> Screen:
    """Parse a saved dump - used by the tests, which need no device."""
    return _parse(Path(path).read_text(encoding="utf-8"), package)
