"""Unit tests for pxh.wander (extracted from bin/px-wander heredoc)."""
import json
from pxh import wander


def test_wander_module_importable_without_picarx():
    assert callable(wander.main)


def test_best_direction_ignores_none():
    assert wander.best_direction({0: None, 25: 40.0, -25: 10.0}) == (25, 40.0)
