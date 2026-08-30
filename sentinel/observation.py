"""Recover what the device saw.

The flows print observations as single JSON lines behind an `@@OBS` marker.
This module reads them back out of whatever log we can get hold of - Maestro's
`maestro.log` locally, BrowserStack's text logs in the cloud - and turns them
into `ObservedValue`s the verifier can judge.

The parser is deliberately tolerant of surrounding noise, because these lines
arrive embedded in timestamps, log levels and device chatter, and it is
deliberately intolerant of malformed content: a line we cannot parse is reported
rather than skipped. A silently dropped observation would resurface later as an
assertion with nothing to consume, which the verifier calls an automation
failure - correct, but far harder to diagnose than "line 412 was not JSON".
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from sentinel.renderer import FLOW_MARKER, OBS_MARKER
from sentinel.schema import ActionOutcome, ObservedValue

# The marker may be preceded by anything a log framework cares to prepend.
_OBS_LINE = re.compile(re.escape(OBS_MARKER) + r"\s*(\{.*\})\s*$")
_FLOW_LINE = re.compile(re.escape(FLOW_MARKER) + r"\s+(start|end)\s+(\S+)")


class ObservationError(Exception):
    """Observations could not be recovered from a run."""


def _coerce_outcome(value: object) -> ActionOutcome | None:
    if value in (None, ""):
        return None
    try:
        return ActionOutcome(str(value))
    except ValueError:
        return ActionOutcome.UNKNOWN


def _to_observation(payload: dict, captured_at: str) -> ObservedValue:
    key = str(payload.get("key", "")).strip()
    if not key:
        raise ValueError("observation has no key")

    raw = payload.get("raw")
    raw = "" if raw is None else str(raw)

    screen_texts = payload.get("screen_texts") or []
    if isinstance(screen_texts, str):
        screen_texts = [screen_texts]
    screen_texts = [str(t) for t in screen_texts]

    anchor = payload.get("anchor")
    if isinstance(anchor, str):
        anchor = anchor.lower() == "true"

    return ObservedValue(
        key=key,
        raw=raw,
        screen_texts=screen_texts,
        anchor_found=anchor if isinstance(anchor, bool) else None,
        outcome=_coerce_outcome(payload.get("outcome")),
        captured_at=captured_at,
        source=str(payload.get("source", "ui")),
    )


def parse_log(text: str) -> tuple[dict[str, ObservedValue], list[str]]:
    """Extract observations from raw log text.

    Returns the observations keyed by name, plus a list of complaints about
    lines that looked like observations but were not usable.
    """
    observations: dict[str, ObservedValue] = {}
    problems: list[str] = []
    captured_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for number, line in enumerate(text.splitlines(), start=1):
        match = _OBS_LINE.search(line)
        if match is None:
            continue
        try:
            payload = json.loads(match.group(1))
            observation = _to_observation(payload, captured_at)
        except (json.JSONDecodeError, ValueError) as exc:
            problems.append(f"line {number}: {exc}")
            continue

        if observation.key in observations:
            # A repeated key means a flow ran a probe twice. The later reading
            # is the one the plan intended to end on, but say so rather than
            # overwrite in silence.
            problems.append(f"line {number}: observation {observation.key!r} seen more than once")
        observations[observation.key] = observation

    return observations, problems


def flow_boundaries(text: str) -> dict[str, str]:
    """Which cases started, and which of those also finished.

    A case that started and never ended is a flow that died mid-way. That is an
    automation failure, and the distinction is invisible in the observations
    alone - a dead flow simply reports fewer of them.
    """
    state: dict[str, str] = {}
    for match in _FLOW_LINE.finditer(text):
        event, case_id = match.group(1), match.group(2)
        if event == "start":
            state[case_id] = "started"
        elif case_id in state:
            state[case_id] = "completed"
    return state


def collect(paths: list[str | Path]) -> tuple[dict[str, ObservedValue], list[str]]:
    """Parse every log we were given, merging the results."""
    observations: dict[str, ObservedValue] = {}
    problems: list[str] = []

    for path in paths:
        path = Path(path)
        if not path.exists():
            problems.append(f"missing log: {path}")
            continue
        found, issues = parse_log(path.read_text(encoding="utf-8", errors="replace"))
        observations.update(found)
        problems.extend(f"{path.name}: {issue}" for issue in issues)

    return observations, problems


def find_evidence(results_dir: str | Path, case_id: str) -> list[str]:
    """Screenshots and logs on disk belonging to one case.

    Evidence is matched by the case id appearing in the filename, which is why
    the renderer names screenshots after it. A verdict with no evidence cannot
    be constructed at all, so this running dry is a real failure and the caller
    is expected to notice.
    """
    results_dir = Path(results_dir)
    if not results_dir.exists():
        return []
    return sorted(
        str(p.relative_to(results_dir))
        for p in results_dir.rglob("*")
        if p.is_file() and case_id.lower() in p.name.lower()
    )
