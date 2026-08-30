"""Load and validate the screen map.

The screen map is the boundary between intent and mechanics. Everything above
it (the sheet, the compiler, the plan) talks about *what* a test wants; only
this file knows what that looks like on a device.

Two guarantees matter here:

*   **Referential integrity.** Every control, value and entity must point at a
    screen that exists. A typo becomes a load-time error rather than a flow that
    taps into empty space at minute forty of a two-hour run.

*   **No silent guessing.** Entries carry `verified: false` until someone has
    confirmed the selector against a real build. Rendering an unverified target
    raises, unless SENTINEL_ALLOW_UNVERIFIED=1 is set for early development. A
    run therefore cannot quietly produce verdicts that rest on a guess.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "screen_map.yaml"

_ALLOW_UNVERIFIED_ENV = "SENTINEL_ALLOW_UNVERIFIED"


class ScreenMapError(Exception):
    """The screen map is malformed, or was asked for something it lacks."""


class UnverifiedTarget(ScreenMapError):
    """A target exists but has not been confirmed against a real build."""


class ScreenMap:
    """Logical target names -> Maestro selectors."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data
        self.screens: dict[str, Any] = data.get("screens") or {}
        self.controls: dict[str, Any] = data.get("controls") or {}
        self.values: dict[str, Any] = data.get("values") or {}
        self.entities: dict[str, Any] = data.get("entities") or {}
        self.navigation: dict[str, Any] = data.get("navigation") or {}
        self.meta: dict[str, Any] = data.get("meta") or {}
        self._validate()

    # -- construction ------------------------------------------------------ #

    @classmethod
    def load(cls, path: str | Path | None = None) -> ScreenMap:
        path = Path(path) if path else DEFAULT_PATH
        if not path.exists():
            raise ScreenMapError(f"no screen map at {path}")
        with path.open(encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
        if not isinstance(data, dict):
            raise ScreenMapError(f"{path} is not a mapping")
        return cls(data)

    def _validate(self) -> None:
        problems: list[str] = []

        if not self.screens:
            problems.append("no screens defined")
        for name, screen in self.screens.items():
            if not (screen or {}).get("anchor"):
                problems.append(
                    f"screen {name!r} has no anchor; without one we cannot prove we arrived"
                )

        for kind, table in (
            ("control", self.controls),
            ("value", self.values),
            ("entity", self.entities),
        ):
            for name, entry in table.items():
                screen = (entry or {}).get("screen")
                if screen is None:
                    problems.append(f"{kind} {name!r} names no screen")
                elif screen not in self.screens:
                    problems.append(f"{kind} {name!r} points at unknown screen {screen!r}")

        for name, route in (self.navigation.get("routes") or {}).items():
            if name not in self.screens:
                problems.append(f"route {name!r} does not correspond to a screen")
            origin = (route or {}).get("from")
            if origin and origin not in self.screens:
                problems.append(f"route {name!r} starts from unknown screen {origin!r}")

        if problems:
            raise ScreenMapError("screen map is inconsistent:\n  - " + "\n  - ".join(problems))

    # -- lookups ----------------------------------------------------------- #

    @staticmethod
    def _allow_unverified() -> bool:
        return os.environ.get(_ALLOW_UNVERIFIED_ENV, "") not in ("", "0", "false", "False")

    def _fetch(self, table: dict[str, Any], kind: str, name: str) -> dict[str, Any]:
        entry = table.get(name)
        if entry is None:
            known = ", ".join(sorted(table)) or "(none defined)"
            raise ScreenMapError(f"unknown {kind} {name!r}; known {kind}s: {known}")
        return entry

    def _check_verified(self, kind: str, name: str, entry: dict[str, Any]) -> None:
        if entry.get("verified") is True or self._allow_unverified():
            return
        raise UnverifiedTarget(
            f"{kind} {name!r} is not verified against a real build "
            f"(source: {entry.get('source', 'unknown')}). Confirm it with "
            f"`maestro hierarchy` and set verified: true, or set "
            f"{_ALLOW_UNVERIFIED_ENV}=1 to render anyway during development."
        )

    def screen(self, name: str) -> dict[str, Any]:
        entry = self._fetch(self.screens, "screen", name)
        self._check_verified("screen", name, entry)
        return entry

    def anchor(self, name: str) -> dict[str, Any]:
        """The selector that proves we are on this screen."""
        return self.screen(name)["anchor"]

    def control(self, name: str) -> dict[str, Any]:
        entry = self._fetch(self.controls, "control", name)
        self._check_verified("control", name, entry)
        return entry

    def value(self, name: str) -> dict[str, Any]:
        entry = self._fetch(self.values, "value", name)
        self._check_verified("value", name, entry)
        return entry

    def entity(self, name: str) -> dict[str, Any]:
        entry = self._fetch(self.entities, "entity", name)
        self._check_verified("entity", name, entry)
        return entry

    def route(self, screen: str) -> dict[str, Any] | None:
        return (self.navigation.get("routes") or {}).get(screen)

    def screen_of(self, target: str) -> str | None:
        """Which screen a control, value or entity lives on.

        Returns the target itself when it already names a screen, so callers can
        pass either without caring which they have.
        """
        for table in (self.controls, self.values, self.entities):
            entry = table.get(target)
            if entry:
                return entry.get("screen")
        return target if target in self.screens else None

    def spec_for_control(self, name: str) -> tuple[str, str] | None:
        """The (module, action) this control implements, for oracle lookups."""
        spec = self._fetch(self.controls, "control", name).get("spec") or {}
        module, action = spec.get("module"), spec.get("action")
        return (module, action) if module and action else None

    @property
    def refusal_markers(self) -> list[str]:
        return [str(m).lower() for m in (self._data.get("refusal_markers") or [])]

    # -- introspection, used to build the compiler prompt ------------------- #

    def vocabulary(self) -> dict[str, list[str]]:
        """The logical target names a plan is allowed to use.

        Handed to the compiler so it selects from what exists instead of
        inventing a target the renderer cannot resolve.
        """
        return {
            "screens": sorted(self.screens),
            "controls": sorted(self.controls),
            "values": sorted(self.values),
            "entities": sorted(self.entities),
        }

    def lint(self) -> list[str]:
        """Weaknesses that load fine but would produce untrustworthy evidence.

        The important one is an anchor that matches the control used to reach
        the screen. If tapping "Material" is followed by checking that
        "Material" is visible, the check passes whether or not the screen ever
        opened - the nav item is still on screen behind it. Reachability then
        proves nothing, and every absence judged on that screen is worthless.
        """
        warnings: list[str] = []

        for name, route in (self.navigation.get("routes") or {}).items():
            screen = self.screens.get(name)
            if not screen:
                continue
            anchor_text = str((screen.get("anchor") or {}).get("text", "")).strip().lower()
            if not anchor_text:
                continue
            taps = [str(t).strip().lower() for t in (route or {}).get("taps", [])]
            if anchor_text in taps:
                warnings.append(
                    f"screen {name!r}: anchor text {anchor_text!r} is also the tap that "
                    f"navigates here, so it cannot prove arrival - pick something only "
                    f"this screen shows"
                )

        seen: dict[str, str] = {}
        for name, screen in self.screens.items():
            anchor = (screen or {}).get("anchor") or {}
            key = str(anchor.get("text") or anchor.get("id") or "").strip().lower()
            if not key:
                continue
            if key in seen:
                warnings.append(
                    f"screens {seen[key]!r} and {name!r} share the anchor {key!r}; "
                    f"arriving on one would look like arriving on the other"
                )
            else:
                seen[key] = name

        return warnings

    def unverified(self) -> list[str]:
        """Everything still resting on a guess - the Phase 1 worklist."""
        out: list[str] = []
        for kind, table in (
            ("screen", self.screens),
            ("control", self.controls),
            ("value", self.values),
            ("entity", self.entities),
        ):
            out.extend(
                f"{kind}:{name}"
                for name, entry in table.items()
                if not (entry or {}).get("verified")
            )
        return sorted(out)

    def coverage(self) -> str:
        """One line for the run banner, so the honesty is unmissable."""
        total = len(self.screens) + len(self.controls) + len(self.values) + len(self.entities)
        pending = len(self.unverified())
        return f"screen map: {total - pending}/{total} targets verified"
