"""The seam between deciding what to run and where it runs.

Local emulator and BrowserStack differ in almost every mechanical detail, and in
one that shapes the architecture: BrowserStack takes an uploaded batch of flows
and hands back logs when it is done, so nothing can steer a cloud device
mid-flight. Both backends are therefore reduced to the same narrow contract -
here are some flows and some secrets, give me back logs and artefacts - and
everything above this line is identical whichever one runs.

That is what makes "switch to BrowserStack" a config change rather than a
rewrite, and it is why the flows carry their observations out through stdout:
it is the one channel both environments preserve.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass
class RunArtifacts:
    """Everything a run left behind.

    `logs` is the load-bearing one: the observation parser reads the `@@OBS`
    lines out of it. Empty logs mean we learned nothing, which the orchestrator
    must treat as an automation failure rather than an absence of findings.
    """

    logs: list[Path] = field(default_factory=list)
    screenshots: list[Path] = field(default_factory=list)
    junit: Path | None = None
    session_urls: list[str] = field(default_factory=list)
    exit_code: int = 0
    duration_seconds: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def produced_evidence(self) -> bool:
        return bool(self.logs or self.screenshots)


class BackendError(Exception):
    """The backend could not run the flows at all."""


@runtime_checkable
class ExecutionBackend(Protocol):
    """Run Maestro flows somewhere and bring back what happened."""

    name: str

    def check(self) -> list[str]:
        """Problems that would stop a run, found before spending an hour on it.

        Returns human-readable complaints; empty means ready. Called before the
        suite starts so a missing binary or an unset credential fails in the
        first seconds rather than at case forty.
        """
        ...

    def run(
        self,
        flow_dir: Path,
        out_dir: Path,
        env: dict[str, str],
        timeout_seconds: int = 1800,
    ) -> RunArtifacts:
        """Execute every flow in `flow_dir`, writing artefacts under `out_dir`."""
        ...
