"""会话活跃登记单测（S07）。"""
import json
from datetime import datetime, timedelta

from app.services.session_registry import SessionRegistry


def test_touch_and_all(tmp_path):
    reg = SessionRegistry(tmp_path / "sessions.json")
    reg.touch("s1")
    assert "s1" in reg.all()
    reg.touch("")  # 空会话不登记
    assert "" not in reg.all()


def test_expired_by_ttl(tmp_path):
    path = tmp_path / "sessions.json"
    old = (datetime.now() - timedelta(hours=25)).isoformat(timespec="seconds")
    fresh = datetime.now().isoformat(timespec="seconds")
    path.write_text(json.dumps({"old": old, "fresh": fresh}), encoding="utf-8")

    reg = SessionRegistry(path)
    assert reg.expired(24) == ["old"]
    assert sorted(reg.expired(1)) == ["old"] or reg.expired(1) == ["old"]


def test_remove_persists(tmp_path):
    path = tmp_path / "sessions.json"
    reg = SessionRegistry(path)
    reg.touch("s1")
    reg.remove("s1")
    assert reg.all() == {}
    assert json.loads(path.read_text(encoding="utf-8")) == {}
