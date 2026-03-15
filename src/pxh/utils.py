"""Shared utility functions for the pxh library."""

from __future__ import annotations


def clamp(value: float, lo: float, hi: float) -> float:
    """Clamp value to [lo, hi] range."""
    return max(lo, min(hi, value))
