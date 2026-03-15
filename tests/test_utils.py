"""Tests for pxh.utils."""

from pxh.utils import clamp


def test_clamp_within_range():
    assert clamp(5, 0, 10) == 5


def test_clamp_below():
    assert clamp(-1, 0, 10) == 0


def test_clamp_above():
    assert clamp(15, 0, 10) == 10


def test_clamp_at_boundaries():
    assert clamp(0, 0, 10) == 0
    assert clamp(10, 0, 10) == 10
