"""自动学习（A11）单测：问题归一化、SQL 占位化、规范去重、记忆写入与敏感拦截、LLM 判断。"""
from app.services import auto_learn as mod
from app.services.learned_skills import LearnedSkills, normalize_question, placeholderize
from app.services.memory_store import MemoryStore


def test_normalize_question_ignores_time():
    a = normalize_question("查昨天的支付订单数")
    b = normalize_question("查上周的支付订单数")
    c = normalize_question("2026-11-20 支付订单数")
    assert a == b == c


def test_placeholderize_dates():
    sql = ("SELECT COUNT(*) FROM dwd_order_detail_di "
           "WHERE dt BETWEEN '2026-11-01' AND '2026-11-02'")
    out = placeholderize(sql)
    assert "BETWEEN '{date_start}' AND '{date_end}'" in out
    assert "2026-11-01" not in out
    # 单日条件里的字面量日期同样能被替换
    assert "{date_start}" in placeholderize("SELECT * FROM t WHERE dt = '2026-11-01'")


def test_learned_skills_dedupe_and_match(tmp_path):
    svc = LearnedSkills(tmp_path / "learned.json")
    first = svc.upsert("alice", {
        "name": "支付订单数", "question": "查昨天的支付订单数",
        "sql_template": "SELECT COUNT(*) FROM t WHERE dt BETWEEN '2026-11-01' AND '2026-11-02'",
        "caliber": "M003", "confidence": 0.8})
    assert first and first["hit"] == 1
    again = svc.upsert("alice", {"name": "支付订单数", "question": "查上周的支付订单数",
                                 "confidence": 0.9})
    assert again["id"] == first["id"] and again["hit"] == 2
    assert svc.match("alice", "查上月的支付订单数")["id"] == first["id"]
    assert svc.match("bob", "查昨天的支付订单数") is None       # 按用户隔离
    assert svc.list("alice")[0]["sql_template"].startswith("SELECT COUNT")


def test_learned_skills_limit(tmp_path, monkeypatch):
    from app.services import learned_skills as ls_mod
    monkeypatch.setattr(ls_mod, "MAX_PER_USER", 3)
    svc = LearnedSkills(tmp_path / "learned.json")
    for metric in ("支付订单数", "销售额", "销量", "退款金额", "客单价"):
        svc.upsert("alice", {"name": f"规范{metric}", "question": f"查昨天的{metric}",
                             "confidence": 0.7})
    assert len(svc.list("alice")) == 3


def test_memory_store_isolated_and_sensitive(tmp_path):
    svc = MemoryStore(tmp_path / "mem.json")
    assert svc.add("alice", "preference", "默认查看最近 7 天")["username"] == "alice"
    # 重复提到 → 同一条，命中次数累加
    dup = svc.add("alice", "preference", "默认查看最近 7 天。", confidence=0.9)
    assert dup["hit"] == 1 and dup["confidence"] == 0.9
    assert len(svc.for_user("alice")) == 1
    assert svc.for_user("bob") == []
    # 敏感信息拒绝写入
    assert svc.add("alice", "rule", "我的手机号是 13812345678") is None
    # 停用后不参与注入，但仍可管理
    svc.update(dup["id"], {"enabled": False}, username="alice")
    assert svc.for_user("alice") == []
    assert len(svc.list("alice")) == 1
    # 越权删除无效
    assert svc.delete(dup["id"], username="bob") is False
    assert svc.delete(dup["id"], username="alice") is True


async def test_generator_injects_memory_and_learned(tmp_path, monkeypatch):
    """生成 prompt 必须带上用户记忆与匹配到的历史规范。"""
    from app.core.statemachine import PipelineContext
    from app.services import generator as gen_mod
    from app.services import learned_skills as ls_mod

    captured = {}

    class FakeClient:
        def available(self):
            return True

        async def structured_call(self, prompt, **kwargs):
            captured["prompt"] = prompt
            return {"sql": "SELECT 1 AS ok", "explanation": "x"}

    monkeypatch.setattr(gen_mod, "llm_client", FakeClient())
    monkeypatch.setattr(ls_mod.learned_skills, "_path", tmp_path / "learned.json")
    monkeypatch.setattr(ls_mod.learned_skills, "_cache", None)
    ls_mod.learned_skills.upsert("t", {"name": "支付订单数", "question": "查昨天的支付订单数",
                                       "sql_template": "SELECT COUNT(*) FROM t", "confidence": 0.9})

    ctx = PipelineContext(question="查昨天的支付订单数", user_id="t",
                          memories=[{"type": "rule", "content": "只看华东区域"}])
    gen = await gen_mod.GeneratorService().generate(ctx)
    assert gen is not None
    assert "只看华东区域" in captured["prompt"]
    assert "支付订单数" in captured["prompt"] and "历史成功 SQL" in captured["prompt"]


async def test_auto_learn_query_and_memory(tmp_path, monkeypatch):
    from app.services import learned_skills as ls_mod
    from app.services import memory_store as ms_mod

    monkeypatch.setattr(ls_mod.learned_skills, "_path", tmp_path / "learned.json")
    monkeypatch.setattr(ls_mod.learned_skills, "_cache", None)
    monkeypatch.setattr(ms_mod.memory_store, "_path", tmp_path / "mem.json")
    monkeypatch.setattr(ms_mod.memory_store, "_cache", None)
    monkeypatch.setattr(mod.llm_client, "available", lambda: True)

    async def fake_call(prompt, **kwargs):
        if "技能规范" in prompt:
            return {"worth": True, "name": "昨日支付订单数",
                    "sql_template": ("SELECT COUNT(*) FROM dwd_order_detail_di "
                                     "WHERE dt BETWEEN '2026-11-01' AND '2026-11-02'"),
                    "caliber": "支付订单数", "notes": "按天", "confidence": 0.9}
        return {"remember": True, "type": "preference",
                "content": "默认查看最近 7 天且只看华东区域", "confidence": 0.9}

    monkeypatch.setattr(mod.llm_client, "structured_call", fake_call)

    learned = await mod.auto_learn.observe_query(
        username="alice", question="查昨天的支付订单数",
        sql="SELECT COUNT(*) FROM dwd_order_detail_di WHERE dt BETWEEN '{date_start}' AND '{date_end}'")
    assert learned and learned["name"] == "昨日支付订单数"
    assert learned["sql_template"].count("{date_start}") == 1

    memory = await mod.auto_learn.observe_conversation(
        username="alice", question="以后默认看最近7天，只看华东", answer="好的")
    assert memory and memory["type"] == "preference"
    assert len(ms_mod.memory_store.for_user("alice")) == 1


async def test_auto_learn_low_confidence_skipped(tmp_path, monkeypatch):
    from app.services import learned_skills as ls_mod

    monkeypatch.setattr(ls_mod.learned_skills, "_path", tmp_path / "learned.json")
    monkeypatch.setattr(ls_mod.learned_skills, "_cache", None)
    monkeypatch.setattr(mod.llm_client, "available", lambda: True)

    async def fake_call(prompt, **kwargs):
        return {"worth": True, "name": "低置信", "confidence": 0.2}

    monkeypatch.setattr(mod.llm_client, "structured_call", fake_call)
    out = await mod.auto_learn.observe_query(
        username="alice", question="查昨天的支付订单数", sql="SELECT 1")
    assert out is None


async def test_auto_learn_failure_is_silent(monkeypatch):
    monkeypatch.setattr(mod.llm_client, "available", lambda: True)

    async def boom(*args, **kwargs):
        raise RuntimeError("model down")

    monkeypatch.setattr(mod.llm_client, "structured_call", boom)
    assert await mod.auto_learn.observe_query(
        username="alice", question="查昨天的支付订单数", sql="SELECT 1") is None
    assert await mod.auto_learn.observe_conversation(
        username="alice", question="以后默认看最近7天", answer="好的") is None
