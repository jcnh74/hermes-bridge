"""Tests for CLI helpers — PID file resolution (the multi-instance fix)."""

from pathlib import Path

from hermes_bridge import cli


def test_pid_file_is_port_specific():
    p8765 = cli.find_pid_file(8765)
    p9000 = cli.find_pid_file(9000)
    assert p8765 != p9000
    assert "8765" in p8765.name
    assert "9000" in p9000.name


def test_pid_file_default_port():
    p = cli.find_pid_file()
    assert p.name == "bridge-8765.pid"


def test_pid_file_under_hermes_home():
    p = cli.find_pid_file(8765)
    assert p.parent == Path.home() / ".hermes"


def test_log_file_path():
    log = cli.find_log_file()
    assert log.name == "bridge.log"
    assert ".hermes" in str(log)


def test_two_ports_dont_collide():
    """Regression: the original bug was a single shared bridge.pid for all ports."""
    ports = [8765, 8766, 9000, 12345]
    names = {cli.find_pid_file(p).name for p in ports}
    assert len(names) == len(ports)  # all unique
