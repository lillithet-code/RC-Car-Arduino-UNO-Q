import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import rpi4b_board_commands as commands


def test_normalize_gpiozero_pin_factory_preserves_non_lgpio():
    env = {'GPIOZERO_PIN_FACTORY': 'native'}
    assert commands.normalize_gpiozero_pin_factory(env) == 'native'
    assert env['GPIOZERO_PIN_FACTORY'] == 'native'


def test_normalize_gpiozero_pin_factory_falls_back_when_lgpio_missing(monkeypatch):
    env = {'GPIOZERO_PIN_FACTORY': 'lgpio'}
    monkeypatch.setattr(commands.importlib.util, 'find_spec', lambda name: None)

    assert commands.normalize_gpiozero_pin_factory(env) == 'native'
    assert env['GPIOZERO_PIN_FACTORY'] == 'native'