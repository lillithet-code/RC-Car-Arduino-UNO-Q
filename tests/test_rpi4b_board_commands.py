import os
import sys
from unittest.mock import Mock

import pytest

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


@pytest.fixture
def driver(monkeypatch):
    monkeypatch.setattr(commands, 'DigitalOutputDevice', Mock())
    monkeypatch.setattr(commands, 'PWMOutputDevice', Mock(side_effect=lambda *args, **kwargs: Mock()))
    monkeypatch.setattr(commands, 'SoftwareServoPWM', Mock())
    return commands.CarGPIODriver()


def assert_motor_output(driver, forward, reverse):
    assert driver._devices['drive_in1'].value == forward
    assert driver._devices['drive_in2'].value == reverse


def test_throttle_changes_apply_immediately_without_scaling(driver, monkeypatch):
    # No elapsed time is needed, even when reversing or reducing throttle.
    monkeypatch.setattr(commands.time, 'monotonic', lambda: 100.0)
    for throttle in (1.0, 0.3, -1.0, -0.4, 0.0, 1.0):
        assert driver.apply_state(throttle=throttle)
        assert_motor_output(driver, max(throttle, 0.0), max(-throttle, 0.0))


@pytest.mark.parametrize('throttle,forward,reverse', [
    (2.0, 1.0, 0.0),
    (-2.0, 0.0, 1.0),
])
def test_throttle_stays_within_physical_pwm_range(driver, throttle, forward, reverse):
    assert driver.apply_state(throttle=throttle)
    assert_motor_output(driver, forward, reverse)


@pytest.mark.parametrize('action,forward,reverse', [
    ('forward', 1.0, 0.0),
    ('back', 0.0, 1.0),
    ('stop', 0.0, 0.0),
])
def test_discrete_actions_use_full_motor_output(driver, action, forward, reverse):
    assert driver.apply_action(action)
    assert_motor_output(driver, forward, reverse)


def test_stop_and_emergency_stop_remain_immediate(driver):
    driver.apply_state(throttle=1.0)
    driver.apply_state(throttle=1.0, stop=True)
    assert_motor_output(driver, 0.0, 0.0)
    driver.apply_state(throttle=-1.0)
    driver.emergency_stop()
    assert_motor_output(driver, 0.0, 0.0)


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