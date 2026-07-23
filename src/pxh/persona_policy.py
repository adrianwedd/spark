"""Runtime policy separating child-facing SPARK from adult personas."""
from __future__ import annotations

import os

ADULT_PERSONAS = frozenset({"gremlin", "vixen"})
CHILD_PERSONAS = frozenset({"spark"})
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def adult_personas_enabled() -> bool:
    """Return whether this process was explicitly deployed for adult personas."""
    return os.environ.get("PX_ENABLE_ADULT_PERSONAS", "").lower().strip() in _TRUE_VALUES


def runtime_persona(value: object, *, fallback: str = "spark") -> str:
    """Normalize a stored persona, replacing disabled adult modes safely."""
    persona = str(value or "").lower().strip()
    if persona in ADULT_PERSONAS and not adult_personas_enabled():
        return fallback
    return persona


def allowed_session_personas() -> frozenset[str]:
    """Personas the authenticated session API may activate in this deployment."""
    if adult_personas_enabled():
        return CHILD_PERSONAS | ADULT_PERSONAS
    return CHILD_PERSONAS
