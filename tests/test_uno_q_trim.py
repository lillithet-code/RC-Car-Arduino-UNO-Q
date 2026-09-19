import os
import sys
from unittest.mock import Mock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import uno_q_board_commands as commands


@pytest.mark.parametrize('transport,method', [('serial', 'write'), ('tcp', 'sendall'), ('unix', 'sendall'), ('udp', 'sendto')])
def test_trim_transport_sends_degrees(monkeypatch, transport, method):
    monkeypatch.setattr(commands, 'TARGET_CONFIG', {'transport': transport, 'host': 'localhost', 'port': 1234})
    connection = Mock()
    assert commands.send_steering_trim_on_connection(-0.1, connection)
    args = getattr(connection, method).call_args.args
    assert args[0] == b'T-3\n'


def test_trim_rpc_clamps_to_steering_range(monkeypatch):
    monkeypatch.setattr(commands, 'TARGET_CONFIG', {'transport': 'rpc'})
    packer = Mock()
    monkeypatch.setattr(commands, 'msgpack', packer)
    commands.send_steering_trim_on_connection(2, Mock())
    packer.packb.assert_called_once_with([2, 'car_set_trim', [9]])
