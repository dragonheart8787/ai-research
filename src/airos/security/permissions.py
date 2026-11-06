"""Role-by-action authorization matrix, stored as DATA (permissions.json) and enforced by the repository.

Two parts:
  * "actions": action name -> list of actor kinds allowed. Includes one `event:<name>` row per state-machine
    event (kept equal to the transition table by a test) and one row per repository operation.
  * "artifact_read": artifact class -> {"always": [...], "inputs_or_own": [...]}; every kind not listed is denied.
    Quarantined artifacts are additionally readable only by human/system (and the ai_scientist that owns them).
"""
from __future__ import annotations

import json
from importlib import resources
from typing import Any

from airos.domain.states import ActorKind
from airos.errors import NotAuthorized


def _load() -> dict[str, Any]:
    text = resources.files("airos.security").joinpath("permissions.json").read_text(encoding="utf-8")
    data: dict[str, Any] = json.loads(text)
    return data


MATRIX: dict[str, Any] = _load()
ACTIONS: dict[str, frozenset[ActorKind]] = {
    name: frozenset(ActorKind(k) for k in kinds) for name, kinds in MATRIX["actions"].items()
}
ARTIFACT_READ: dict[str, dict[str, frozenset[ActorKind]]] = {
    cls: {mode: frozenset(ActorKind(k) for k in kinds) for mode, kinds in rule.items()}
    for cls, rule in MATRIX["artifact_read"].items()
}
ALWAYS_PRIVILEGED = frozenset({ActorKind.HUMAN, ActorKind.SYSTEM})


def is_allowed(kind: ActorKind, action: str) -> bool:
    """Unknown actions are denied (fail closed)."""
    return kind in ACTIONS.get(action, frozenset())


def authorize(kind: ActorKind, action: str) -> None:
    if not is_allowed(kind, action):
        raise NotAuthorized(f"{kind.value} is not permitted to perform {action!r}")


def may_read_artifact(kind: ActorKind, artifact_class: str, *, pinned_or_own: bool, quarantined: bool) -> bool:
    rule = ARTIFACT_READ.get(artifact_class)
    if rule is None:
        return False  # unknown class: deny
    if quarantined and kind not in ALWAYS_PRIVILEGED and kind is not ActorKind.AI_SCIENTIST:
        return False
    if kind in rule.get("always", frozenset()):
        return True
    return pinned_or_own and kind in rule.get("inputs_or_own", frozenset())
