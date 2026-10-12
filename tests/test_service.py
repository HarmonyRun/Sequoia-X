"""Tests for the fnOS scheduler helpers."""

from sequoia_x.service import Scheduler, _env_bool


def test_env_bool(monkeypatch) -> None:
    monkeypatch.setenv("FEATURE_SWITCH", "yes")
    assert _env_bool("FEATURE_SWITCH", False) is True
    monkeypatch.setenv("FEATURE_SWITCH", "off")
    assert _env_bool("FEATURE_SWITCH", True) is False


def test_parse_schedule(monkeypatch) -> None:
    monkeypatch.setenv("RUN_TIME", "18:45")
    monkeypatch.setenv("RUN_WEEKDAYS", "1,3,5")
    assert Scheduler._parse_schedule() == (18, 45, {1, 3, 5})


def test_parse_schedule_falls_back(monkeypatch) -> None:
    monkeypatch.setenv("RUN_TIME", "99:99")
    monkeypatch.setenv("RUN_WEEKDAYS", "invalid")
    assert Scheduler._parse_schedule() == (16, 0, {1, 2, 3, 4, 5})
