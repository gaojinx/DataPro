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


def test_owner_recorded_and_access_rules(store):
    from app.api.routes_files import _can_access
    from app.services.auth import AuthUser

    entry = store.add(session_id="s1", filename="a.csv", content=b"a,b", owner="alice")
    assert entry["owner"] == "alice"

    alice = AuthUser(id=1, username="alice", display_name="A", role="user")
    bob = AuthUser(id=2, username="bob", display_name="B", role="user")
    admin = AuthUser(id=3, username="admin", display_name="管理员", role="admin")
    assert _can_access(entry, alice) is True
    assert _can_access(entry, bob) is False
    assert _can_access(entry, admin) is True

    # 历史文件无 owner 视为公开，避免升级后旧文件不可用
    legacy = store.add(session_id="s1", filename="old.csv", content=b"x")
    assert _can_access(legacy, bob) is True


def test_version_chain_and_restore_source(store):
    """F49：编辑保存归到同一 root_id，版本号递增；回滚读取历史内容。"""
    v1 = store.add(session_id="s1", filename="order.csv", content=b"a,b\n1,2")
    v2 = store.add(session_id="s1", filename="order_edited.csv", content=b"a,b\n1,3",
                   origin="edited", root_id=v1["file_id"])
    v3 = store.add(session_id="s1", filename="order_edited.csv", content=b"a,b\n1,4",
                   origin="edited", root_id=v2["file_id"])
    assert v1["version"] == 1 and v2["version"] == 2 and v3["version"] == 3
    assert v2["root_id"] == v1["file_id"] == v3["root_id"]
    versions = store.versions(v3["file_id"])
    assert [v["version"] for v in versions] == [1, 2, 3]
    assert store.content_of(store.get(v1["file_id"])) == b"a,b\n1,2"

    # 跨会话的 root_id 不串联，另起一条链
    other = store.add(session_id="s2", filename="order.csv", content=b"x",
                      origin="edited", root_id=v1["file_id"])
    assert other["root_id"] == other["file_id"] and other["version"] == 1
