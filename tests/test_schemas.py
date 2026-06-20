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


def test_announce_max_matches_constant():
    from pxh.schemas import TOOL_SCHEMAS
    from pxh.spark_config import ANNOUNCE_MAX_CHARS
    assert TOOL_SCHEMAS["tool_announce"]["params"]["text"]["max"] == ANNOUNCE_MAX_CHARS
