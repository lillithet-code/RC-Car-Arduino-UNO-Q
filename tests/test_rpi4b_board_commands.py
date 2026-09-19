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
        assert driver.apply_state(control_active=True, throttle=throttle)
        assert_motor_output(driver, max(throttle, 0.0), max(-throttle, 0.0))


def test_saved_trim_offsets_center_clamps_endpoints_and_does_not_wake_idle_servo(driver):
    driver.apply_state(control_active=True, throttle=1, steering_trim=0.1)
    assert driver._servo.angle == pytest.approx(3.0)
    driver.apply_state(control_active=True, throttle=1, steering=1, steering_trim=0.3)
    assert driver._servo.angle == commands.SERVO_RIGHT_ANGLE
    driver.apply_state(control_active=True, stop=True, steering_trim=0.1)
    assert driver._servo.angle is None
    driver.apply_state(control_active=False, throttle=1, steering_trim=0.1)
    assert driver._servo.angle is None


@pytest.mark.parametrize('throttle,forward,reverse', [
    (2.0, 1.0, 0.0),
    (-2.0, 0.0, 1.0),
])
def test_throttle_stays_within_physical_pwm_range(driver, throttle, forward, reverse):
    assert driver.apply_state(control_active=True, throttle=throttle)
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
    driver.apply_state(control_active=True, throttle=1.0)
    driver.apply_state(control_active=True, throttle=1.0, stop=True)
    assert_motor_output(driver, 0.0, 0.0)
    driver.apply_state(control_active=True, throttle=-1.0)
    driver.emergency_stop()
    assert_motor_output(driver, 0.0, 0.0)
    assert driver._servo.angle is None


def test_idle_disables_steering_and_active_commands_restore_it(driver):
    driver.apply_state(control_active=True, throttle=1.0, steering=0.0)
    assert driver._servo.angle == commands.SERVO_CENTER_ANGLE
    driver.apply_state(control_active=True, throttle=0.0, steering=0.0, stop=True)
    assert driver._servo.angle is None
    # Repeated server stop messages must not wake the servo.
    driver.apply_state(control_active=True, throttle=0.0, steering=0.0, stop=True)
    assert driver._servo.angle is None
    driver.apply_state(control_active=True, throttle=0.0, steering=1.0, stop=True)
    assert driver._servo.angle == commands.SERVO_RIGHT_ANGLE
    driver.apply_action('stop')
    assert driver._servo.angle is None
    driver.apply_action('left')
    assert driver._servo.angle == commands.SERVO_LEFT_ANGLE
    for action in ('forward', 'back'):
        driver.apply_action('stop')
        driver.apply_action(action)
        assert driver._servo.angle == commands.SERVO_CENTER_ANGLE


def test_software_servo_emits_no_pulses_until_enabled_and_after_disable(monkeypatch):
    device = Mock()
    monkeypatch.setattr(commands, 'DigitalOutputDevice', Mock(return_value=device))
    monkeypatch.setattr(commands.threading, 'Thread', Mock())
    monkeypatch.setattr(commands.time, 'sleep', Mock())
    servo = commands.SoftwareServoPWM(6, -90, 90, 0.0005, 0.0025, 0.02)

    def run_one_frame():
        servo._stop_event = Mock()
        servo._stop_event.is_set.side_effect = [False, True]
        device.reset_mock()
        servo._run()

    run_one_frame()
    device.on.assert_not_called()
    servo.angle = 30
    run_one_frame()
    device.on.assert_called_once()
    device.off.assert_called_once()
    servo.angle = None
    run_one_frame()
    device.on.assert_not_called()
    servo.angle = 0
    run_one_frame()
    device.on.assert_called_once()


def test_parse_desired_state_accepts_valid_payload():
    now_ms = 1_000_000
    state, error = commands.parse_desired_state(
        {
            'sequence': 10,
            'control_active': True,
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
    assert state['control_active'] is True


@pytest.mark.parametrize('active', [None, False, 'true', 1])
def test_unconfirmed_control_cannot_enable_motor_or_steering(driver, active):
    payload = {'sequence': 1, 'throttle': 1.0, 'steering': 1.0}
    if active is not None:
        payload['control_active'] = active
    state, error = commands.parse_desired_state(payload, 0, 1000)
    assert error is None
    assert state['control_active'] is False
    assert state['stop'] is True
    assert state['throttle'] == state['steering'] == 0.0
    driver.apply_state(control_active=True, throttle=1.0, steering=1.0)
    driver.apply_state(throttle=1.0, steering=1.0, control_active=active)
    assert_motor_output(driver, 0.0, 0.0)
    assert driver._servo.angle is None
    driver.apply_state(control_active=True, throttle=1.0, steering=1.0)
    assert driver._servo.angle == commands.SERVO_RIGHT_ANGLE


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
