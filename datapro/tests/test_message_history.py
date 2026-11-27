"""消息历史持久化（0018）：附件引用随消息落库 + 会话历史读取 + 读取权限回退。

持久化用例走真实业务库（与 test_message_persist 同一约定），用唯一 session_id 并清理。
"""
import json

import pytest

from app.services import persist


SESSION = "_hist_smoke_sess"


def _cleanup():
    import pymysql

    c = pymysql.connect(host="127.0.0.1", port=3306, user="datapro", password="pw",
                        database="datapro", connect_timeout=3, autocommit=True)
    cur = c.cursor()
    cur.execute("DELETE FROM message WHERE session_id = %s", (SESSION,))
    c.close()


@pytest.fixture(autouse=True)
def _clean():
    _cleanup()
    yield
    _cleanup()


async def test_save_message_with_attachments_roundtrip():
    atts = [{"file_id": "abc123", "name": "订单.csv", "ext": ".csv",
             "size": 128, "origin": "upload"}]
    mid = await persist.save_message(SESSION, "tester", "user", "帮我分析这份文件",
                                     kind="query", trace_id="t1", attachments=atts)
    assert mid

    rows = await persist.list_messages(SESSION)
    assert len(rows) == 1
    row = rows[0]
    assert row["role"] == "user"
    assert row["content"] == "帮我分析这份文件"
    assert row["attachments"] == atts          # 解析回列表（不返回 JSON 文本）


async def test_save_message_without_attachments_returns_empty_list():
    await persist.save_message(SESSION, "tester", "assistant", "这是回答", kind="query")
    rows = await persist.list_messages(SESSION)
    assert rows[0]["attachments"] == []
    assert rows[0]["username"] == "tester"


async def test_list_messages_ascending_and_limit():
    for i in range(5):
        await persist.save_message(SESSION, "tester", "user", f"m{i}")
    rows = await persist.list_messages(SESSION, limit=3)
    assert [r["content"] for r in rows] == ["m2", "m3", "m4"]      # 最新 3 条、时间升序


async def test_save_message_with_payload_roundtrip():
    """终态载荷（0019）：result 事件随助手消息落库，历史读取原样还原。"""
    payload = {"type": "result", "summary": "共 123 单",
               "columns": ["c1"], "rows": [{"c1": 123}], "row_count": 1,
               "sql": "SELECT COUNT(*)", "query_id": 7, "trace_code": "DP-0007"}
    await persist.save_message(SESSION, "tester", "user", "最近7天支付订单数", kind="query")
    await persist.save_message(SESSION, "tester", "assistant", "共 123 单",
                               kind="query", trace_id="t9", payload=payload)
    rows = await persist.list_messages(SESSION)
    assert len(rows) == 2
    assert rows[1]["payload"]["type"] == "result"
    assert rows[1]["payload"]["query_id"] == 7
    assert rows[1]["payload"]["rows"] == [{"c1": 123}]
    assert rows[0]["payload"] is None                       # 用户消息无载荷


def test_step_entry_rules():
    """F54：节点事件 → 步骤轨迹；START/DONE 不进时间轴，retry 取整数。"""
    from app.api.routes_chat import step_entry

    assert step_entry({"type": "node", "node": "INTENT"}) == {"node": "INTENT", "retry": 0}
    assert step_entry({"type": "node", "node": "GENERATE", "retry": 1}) == \
        {"node": "GENERATE", "retry": 1}
    assert step_entry({"type": "node", "node": "START"}) is None
    assert step_entry({"type": "node", "node": "DONE"}) is None
    assert step_entry({"type": "result"}) is None
    assert step_entry({"type": "node", "node": "VALIDATE", "retry": "x"}) == \
        {"node": "VALIDATE", "retry": 0}                     # 非法 retry 容错


async def test_payload_with_steps_roundtrip():
    """终态载荷含步骤轨迹与耗时，历史读取原样还原。"""
    payload = {"type": "result", "summary": "ok", "steps": [
        {"node": "INTENT", "retry": 0},
        {"node": "RECALL", "retry": 0},
        {"node": "GENERATE", "retry": 1},
        {"node": "PRESENT", "retry": 0},
    ], "elapsed_ms": 3210}
    await persist.save_message(SESSION, "tester", "assistant", "ok",
                               kind="query", payload=payload)
    rows = await persist.list_messages(SESSION)
    assert rows[0]["payload"]["steps"] == payload["steps"]
    assert rows[0]["payload"]["elapsed_ms"] == 3210


async def test_list_sessions_for_user_archive():
    """服务端会话归档：标题取本人首条提问，只含本人参与的会话。"""
    await persist.save_message(SESSION, "tester", "user", "最近7天支付订单数是多少")
    await persist.save_message(SESSION, "tester", "assistant", "共 123 单")
    sessions = await persist.list_sessions_for_user("tester")
    hit = next((s for s in sessions if s["session_id"] == SESSION), None)
    assert hit is not None
    assert hit["title"].startswith("最近7天支付订单数")
    assert hit["message_count"] == 2
    assert hit["last_at"]
    assert not any(s["session_id"] == SESSION for s in await persist.list_sessions_for_user("nobody"))


async def test_session_has_user():
    assert await persist.session_has_user(SESSION, "tester") is False
    await persist.save_message(SESSION, "tester", "user", "参与过")
    assert await persist.session_has_user(SESSION, "tester") is True
    assert await persist.session_has_user(SESSION, "other") is False


async def test_sanitize_attachments_whitelists_and_drops_foreign():
    from app.api.routes_chat import AuthUser, _sanitize_attachments
    from app.services import file_store as fs_mod

    entries = {
        "mine": {"file_id": "mine", "session_id": SESSION, "owner": "tester",
                 "name": "a.csv", "ext": ".csv", "size": 1, "origin": "upload",
                 "stored_name": "x"},
        "other_sess": {"file_id": "other_sess", "session_id": "sess-B", "owner": "tester",
                       "name": "b.csv", "ext": ".csv", "size": 1, "origin": "upload"},
        "foreign": {"file_id": "foreign", "session_id": SESSION, "owner": "someone_else",
                    "name": "c.csv", "ext": ".csv", "size": 1, "origin": "upload"},
    }
    monkeypatch_get = lambda fid: entries.get(fid)  # noqa: E731
    original = fs_mod.file_store.get
    fs_mod.file_store.get = staticmethod(monkeypatch_get)
    try:
        user = AuthUser(id=1, username="tester", display_name="t", role="user",
                        permissions=["chat.use"])
        raw = [{"file_id": "mine", "evil": "x"}, {"file_id": "other_sess"},
               {"file_id": "foreign"}, {"file_id": "ghost"}]
        out = _sanitize_attachments(raw, SESSION, user)
        assert len(out) == 1                          # 只有本会话本人的文件保留
        assert out[0]["file_id"] == "mine"
        assert "evil" not in out[0]                   # 白名单字段，客户端塞的键被剔除
        assert "stored_name" not in out[0]            # 磁盘文件名不进消息快照
        assert _sanitize_attachments(None, SESSION, user) is None
        assert _sanitize_attachments([{"file_id": "ghost"}], SESSION, user) is None

        admin = AuthUser(id=2, username="admin", display_name="a", role="admin", permissions=[])
        out_admin = _sanitize_attachments([{"file_id": "foreign"}], SESSION, admin)
        assert out_admin and out_admin[0]["file_id"] == "foreign"   # 管理员可引用
    finally:
        fs_mod.file_store.get = original
