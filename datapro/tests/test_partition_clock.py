"""数据新鲜度锚点（U-B01）：相对时间窗用真实分区，显式日期不漂移。"""
from datetime import date, timedelta

import pytest

from app.config import settings
from app.core import time_window as tw
from app.services.partition_clock import partition_clock


@pytest.fixture(autouse=True)
def _reset_clock(monkeypatch):
    monkeypatch.setattr(settings, "eval_fixed_anchor", "")
    monkeypatch.setattr(settings, "partition_read_enabled", True)
    partition_clock.reset()
    yield
    partition_clock.reset()


def test_anchor_falls_back_to_calendar_when_never_probed():
    today = date(2026, 11, 26)
    assert partition_clock.confirmed() is None
    assert partition_clock.anchor(today) == today - timedelta(days=1)


def test_default_window_uses_confirmed_anchor():
    today = date(2026, 11, 26)
    # 模拟探测到真实分区为 11-22（比日历 T-1 落后 3 天）
    partition_clock._anchor = date(2026, 11, 22)
    w = tw.default_window(today)
    assert w.end == date(2026, 11, 22)
    assert w.start == date(2026, 11, 16)
    assert w.explicit is False


def test_explicit_relative_day_keeps_calendar():
    """「昨天」是用户明确的日历表达，不能被锚点挪走。"""
    today = date(2026, 11, 26)
    partition_clock._anchor = date(2026, 11, 22)
    w = tw.resolve("昨天", today)
    assert w.start == w.end == date(2026, 11, 25)


def test_recent_days_uses_anchor():
    today = date(2026, 11, 26)
    partition_clock._anchor = date(2026, 11, 22)
    w = tw.resolve("最近7天", today)
    assert w.end == date(2026, 11, 22)
    assert w.start == date(2026, 11, 16)


def test_disabled_flag_restores_old_behaviour(monkeypatch):
    monkeypatch.setattr(settings, "partition_read_enabled", False)
    today = date(2026, 11, 26)
    partition_clock._anchor = date(2026, 11, 22)
    assert partition_clock.anchor(today) == date(2026, 11, 25)
    assert partition_clock.confirmed() is None
    assert tw.default_window(today).end == date(2026, 11, 25)


def test_fixed_anchor_wins_for_eval(monkeypatch):
    monkeypatch.setattr(settings, "eval_fixed_anchor", "2026-11-20")
    today = date(2026, 11, 26)
    assert partition_clock.anchor(today) == date(2026, 11, 20)
    assert partition_clock.confirmed() == date(2026, 11, 20)
    assert tw.resolve("最近7天", today).end == date(2026, 11, 20)


def test_status_does_not_deadlock():
    """status() 曾因非重入锁 + is_fresh() 二次加锁死锁，回归覆盖。"""
    status = partition_clock.status()
    assert status["enabled"] is True
    assert status["anchor"] is None
    assert status["fresh"] is False
