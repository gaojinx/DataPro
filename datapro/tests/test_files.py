"""会话文件存储单测（B18）：路径安全、类型与大小校验、增删查。"""
import pytest

from app.services import file_store as fs_mod


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(fs_mod, "ROOT", tmp_path)
    monkeypatch.setattr(fs_mod, "INDEX", tmp_path / "index.json")
    return fs_mod.FileStore()


def test_sanitize_strips_path():
    assert fs_mod.FileStore.sanitize("../../etc/passwd") == "passwd"
    assert fs_mod.FileStore.sanitize("订单 明细.csv") == "订单_明细.csv"


def test_blocked_extension(store):
    with pytest.raises(ValueError):
        store.add(session_id="s1", filename="virus.exe", content=b"x")


def test_empty_and_oversize(store):
    with pytest.raises(ValueError):
        store.add(session_id="s1", filename="a.csv", content=b"")
    with pytest.raises(ValueError):
        store.add(session_id="s1", filename="a.csv", content=b"x" * (fs_mod.MAX_SIZE + 1))


def test_illegal_session_falls_back_to_default(store):
    entry = store.add(session_id="../hack", filename="a.csv", content=b"a,b")
    assert entry["session_id"] == "default"


def test_add_list_delete(store):
    e1 = store.add(session_id="s1", filename="orders.csv", content=b"a,b\n1,2")
    e2 = store.add(session_id="s1", filename="report.csv", content=b"c", origin="generated", query_id="q1")
    assert e1["origin"] == "upload" and e2["origin"] == "generated"
    assert [e["file_id"] for e in store.list("s1")] == [e2["file_id"], e1["file_id"]]
    assert store.path_of(store.get(e1["file_id"])).exists()
    assert store.delete(e1["file_id"]) is True
    assert len(store.list("s1")) == 1
    assert store.delete("not-exist") is False
