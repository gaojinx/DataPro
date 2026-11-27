"""A11 学习数据存储兼容：包装格式 {"memories"/"skills": [...]} 与裸数组都必须能写。

背景：线上两个文件曾被包装成字典，读路径兼容、写路径不兼容，
导致新增记忆/技能抛 `'str' object has no attribute 'get'`（删除用户时可见告警）。
"""
import json

from app.services.learned_skills import LearnedSkills
from app.services.memory_store import MemoryStore


def _wrapped(tmp_path, filename: str, key: str, items: list) -> "object":
    path = tmp_path / filename
    path.write_text(json.dumps({key: items}, ensure_ascii=False), encoding="utf-8")
    return path


def test_memory_add_and_clear_on_wrapped_file(tmp_path):
    path = _wrapped(tmp_path, "memories.json", "memories", [])
    store = MemoryStore(path=path)

    item = store.add("alice", "preference", "默认看华东区域")
    assert item and item["username"] == "alice"

    doc = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(doc, dict)                     # 保持文件原有形态
    assert doc["memories"][0]["content"] == "默认看华东区域"

    assert store.list("alice")[0]["content"] == "默认看华东区域"
    assert store.clear_user("alice") == 1
    assert json.loads(path.read_text(encoding="utf-8"))["memories"] == []


def test_memory_add_on_bare_list_file(tmp_path):
    path = tmp_path / "memories.json"
    path.write_text("[]", encoding="utf-8")
    store = MemoryStore(path=path)

    store.add("bob", "rule", "退款按支付时间统计")
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(doc, list) and doc[0]["username"] == "bob"
    assert store.clear_user("bob") == 1
    assert json.loads(path.read_text(encoding="utf-8")) == []


def test_learned_skills_upsert_and_clear_on_wrapped_file(tmp_path):
    path = _wrapped(tmp_path, "learned_skills.json", "skills", [])
    svc = LearnedSkills(path=path)

    item = svc.upsert("alice", {"name": "近7天订单", "question": "最近7天支付订单数是多少",
                                "sql_template": "SELECT 1", "confidence": 0.8})
    assert item and item["username"] == "alice"
    assert svc.match("alice", "最近7天支付订单数是多少") is not None

    doc = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(doc, dict) and doc["skills"][0]["name"] == "近7天订单"

    assert svc.clear_user("alice") == 1
    assert json.loads(path.read_text(encoding="utf-8"))["skills"] == []
