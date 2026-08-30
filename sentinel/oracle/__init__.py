"""The specification oracle: what Navicon says each role may do.

Loaded from `permissions.yaml`, which transcribes the permission matrix in the
Complete Guide. Two jobs:

*   **Explain a verdict.** When a Site Engineer turns out to be able to approve
    an eMB, the oracle is what lets the diagnosis say "the spec grants this to
    Owner, Admin, Manager and named approvers only" instead of guessing.

*   **Pick the control persona for a differential probe.** To claim that a
    control is genuinely hidden from one role we first confirm our selector
    finds it for a role that should see it. The oracle knows who that is.

The oracle never decides a verdict. If observed behaviour contradicts this file,
that contradiction is the finding.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

import yaml

_MATRIX_PATH = Path(__file__).with_name("permissions.yaml")

# Persona names as the test sheet writes them -> role keys in the matrix.
_PERSONA_TO_ROLE: dict[str, str] = {
    "owner": "own",
    "super admin": "own",
    "owner / super admin": "own",
    "admin": "adm",
    "manager": "mgr",
    "accountant": "acc",
    "site engineer": "eng",
    "engineer": "eng",
    "site member": "mem",
    "munshi": "mem",
    "client": "cli",
    "external approver": "ext",
}

# A grant of `none` means the affordance should not exist. Everything else does.
_DENIED = "none"

# Grants that permit the action but only over a narrower slice than "anything".
_SCOPED = frozenset({"site", "own", "own_project", "raise", "log", "stock", "fin", "view", "del", "if_set"})


class OracleError(Exception):
    """The matrix was asked about something it does not describe."""


class _GrantLoader(yaml.SafeLoader):
    """SafeLoader that keeps `yes` a string.

    The grant vocabulary includes `yes`, which YAML 1.1 would otherwise resolve
    to the boolean True and silently break every lookup against it.
    """


_GrantLoader.add_constructor(
    "tag:yaml.org,2002:bool", lambda loader, node: loader.construct_scalar(node)
)


@cache
def _matrix() -> dict:
    with _MATRIX_PATH.open(encoding="utf-8") as handle:
        return yaml.load(handle, Loader=_GrantLoader)


def role_key(persona: str) -> str:
    """'Site Engineer' -> 'eng'. Raises if the persona is unknown."""
    key = _PERSONA_TO_ROLE.get(persona.strip().lower())
    if key is None:
        raise OracleError(f"unknown persona {persona!r}")
    return key


def grant(persona: str, module: str, action: str) -> str:
    """The spec grant for one role x action, e.g. 'full', 'site', 'none'."""
    modules = _matrix()["modules"]
    if module not in modules:
        raise OracleError(f"no module {module!r} in the matrix")
    if action not in modules[module]:
        raise OracleError(f"no action {action!r} in module {module!r}")
    return str(modules[module][action][role_key(persona)])


def is_allowed(persona: str, module: str, action: str) -> bool:
    """Should this role be able to do this at all?"""
    return grant(persona, module, action) != _DENIED


def is_scoped(persona: str, module: str, action: str) -> bool:
    """Allowed, but only within a narrower scope (their site, their own entries)."""
    return grant(persona, module, action) in _SCOPED


def control_persona(persona: str, module: str, action: str) -> str | None:
    """Who should legitimately see the control that `persona` must not see?

    Returns the *lowest*-authority tested role that is still permitted, which
    keeps the differential probe as close to the case under test as possible. A
    None result means nobody we can log in as should see it either - the probe
    cannot distinguish enforcement from a broken selector, and the caller must
    fall back to attempting the action outright.
    """
    from sentinel.schema import PERSONA_AUTHORITY

    candidates = [
        name
        for name in PERSONA_AUTHORITY
        if name.lower() != persona.strip().lower() and is_allowed(name, module, action)
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda name: PERSONA_AUTHORITY[name])


def actions() -> list[tuple[str, str]]:
    """Every (module, action) pair the matrix describes."""
    return [(m, a) for m, acts in _matrix()["modules"].items() for a in acts]


def high_risk_controls() -> list[dict]:
    """The three controls the guide itself flags as carrying the risk."""
    return list(_matrix().get("high_risk_controls", []))


def describe(module: str, action: str) -> str:
    """One line naming who the spec permits - for diagnosis prose."""
    modules = _matrix()["modules"]
    if module not in modules or action not in modules[module]:
        raise OracleError(f"no such action: {module}.{action}")
    names = _matrix()["roles"]
    permitted = [
        names[role] for role, value in modules[module][action].items() if str(value) != _DENIED
    ]
    if not permitted:
        return f"{module}.{action} is granted to no role."
    return f"{module}.{action} is granted to: {', '.join(permitted)}."
