import sys
import types

from pxh.hardware import make_picarx


def test_secondary_picarx_skips_mcu_reset_and_restores_function(monkeypatch):
    reset_calls = []
    utils = types.ModuleType("robot_hat.utils")
    utils.reset_mcu = lambda: reset_calls.append("reset")
    robot_hat = types.ModuleType("robot_hat")
    robot_hat.utils = utils

    class FakePicarx:
        def __init__(self):
            utils.reset_mcu()

    picarx = types.ModuleType("picarx")
    picarx.Picarx = FakePicarx
    monkeypatch.setitem(sys.modules, "robot_hat", robot_hat)
    monkeypatch.setitem(sys.modules, "robot_hat.utils", utils)
    monkeypatch.setitem(sys.modules, "picarx", picarx)
    original_reset = utils.reset_mcu

    assert isinstance(make_picarx(), FakePicarx)
    assert reset_calls == []
    assert utils.reset_mcu is original_reset
