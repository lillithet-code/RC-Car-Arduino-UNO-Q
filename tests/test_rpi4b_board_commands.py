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


def test_parse_desired_state_accepts_valid_payload():
    now_ms = 1_000_000
    state, error = commands.parse_desired_state(
        {
            'sequence': 10,
            'expires_in_ms': 500,
            'issued_at_ms': now_ms,
            'throttle': 0.7,
            'steering': -0.25,
            'lights': True,
        },
        last_sequence=9,
        now_ms=now_ms,
    )

    assert error is None
    assert state is not None
    assert state['sequence'] == 10
    assert state['throttle'] == 0.7
    assert state['steering'] == -0.25
    assert state['lights'] is True
    assert state['stop'] is False


def test_parse_desired_state_rejects_expired_payload():
    now_ms = 2_000_000
    state, error = commands.parse_desired_state(
        {
            'sequence': 42,
            'expires_in_ms': 300,
            'issued_at_ms': now_ms - 1000,
            'throttle': 0.8,
            'steering': 0.1,
        },
        last_sequence=41,
        now_ms=now_ms,
    )
    assert state is None
    assert error == 'expired'


def test_parse_desired_state_rejects_out_of_order_sequence():
    now_ms = 3_000_000
    state, error = commands.parse_desired_state(
        {
            'sequence': 5,
            'expires_in_ms': 500,
            'issued_at_ms': now_ms,
            'throttle': 0.4,
            'steering': 0.0,
        },
        last_sequence=6,
        now_ms=now_ms,
    )
    assert state is None
    assert error == 'out_of_order'