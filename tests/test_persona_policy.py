from pxh.persona_policy import (
    adult_personas_enabled,
    allowed_session_personas,
    runtime_persona,
)


def test_child_deployment_disables_adult_personas(monkeypatch):
    monkeypatch.delenv("PX_ENABLE_ADULT_PERSONAS", raising=False)

    assert adult_personas_enabled() is False
    assert allowed_session_personas() == frozenset({"spark"})
    assert runtime_persona("vixen") == "spark"
    assert runtime_persona("gremlin") == "spark"


def test_adult_deployment_requires_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("PX_ENABLE_ADULT_PERSONAS", "true")

    assert adult_personas_enabled() is True
    assert allowed_session_personas() == frozenset({"spark", "vixen", "gremlin"})
    assert runtime_persona("vixen") == "vixen"
