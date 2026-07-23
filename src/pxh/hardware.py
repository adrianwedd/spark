"""Shared PiCar-X construction rules for secondary hardware clients."""
from __future__ import annotations


def make_picarx(*, force_reset: bool = False):
    """Create a Picarx without reclaiming the MCU reset GPIO by default."""
    from picarx import Picarx

    if force_reset:
        return Picarx()

    try:
        import robot_hat.utils
    except ImportError:
        return Picarx()

    original_reset = robot_hat.utils.reset_mcu
    robot_hat.utils.reset_mcu = lambda: None
    try:
        return Picarx()
    finally:
        robot_hat.utils.reset_mcu = original_reset
