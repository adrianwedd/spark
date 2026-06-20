"""Tests for declarative TOOL_SCHEMAS in pxh.schemas."""


def test_every_allowed_tool_has_schema():
    from pxh.voice_loop import ALLOWED_TOOLS
    from pxh.schemas import TOOL_SCHEMAS
    missing = ALLOWED_TOOLS - set(TOOL_SCHEMAS)
    assert not missing, f"tools missing schemas: {sorted(missing)}"


def test_schema_shape():
    from pxh.schemas import TOOL_SCHEMAS
    for name, spec in TOOL_SCHEMAS.items():
        assert "description" in spec, f"{name} missing 'description'"
        assert "params" in spec and isinstance(spec["params"], dict), f"{name} missing 'params' dict"
