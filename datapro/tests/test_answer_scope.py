"""未命中知识库时的兜底：可查询范围必须来自实时元数据，不能写死在提示词里。

背景：`SYSTEM_GENERAL` 曾写死「含订单明细日报表与用户行为日报表两张表」，
而实际库里有 11 张表 + 14 个口径。用户问「你们都有哪些数据」时，模型会照着
这段写死的描述理直气壮地回答「两张表」——答错且没有任何依据条目可供核对。
"""
import pytest

from app.services import smalltalk


def _fake_tables() -> list[dict]:
    return [
        {"schema": "dwd_shop", "table_name": "dwd_order_detail_di",
         "cn_name": "订单明细日报表", "description": "订单粒度"},
        {"schema": "dwd_shop", "table_name": "dim_goods",
         "cn_name": "商品维表", "description": "商品维度"},
    ]


def test_general_system_uses_live_catalog(monkeypatch):
    """目录来自 metadata_store：表增删后 system 自动跟着变。"""
    monkeypatch.setattr("app.services.metadata_store.metadata_store.tables",
                        _fake_tables, raising=False)
    monkeypatch.setattr("app.services.metadata_store.metadata_store.calibers",
                        lambda: [{"metric_name": "支付订单数"}], raising=False)

    system = smalltalk.general_system()
    assert "dwd_order_detail_di（订单明细日报表）" in system
    assert "dim_goods（商品维表）" in system
    assert "支付订单数" in system
    # 旧硬编码的两张表里，dwd_user_di 不在假目录中，绝不能残留
    assert "dwd_user_di" not in system


def test_general_system_survives_metadata_failure(monkeypatch):
    """元数据读不到时宁可不说范围，也不能让寒暄/通用问答 500。"""
    def boom():
        raise RuntimeError("metadata db down")

    monkeypatch.setattr("app.services.metadata_store.metadata_store.tables",
                        boom, raising=False)
    assert smalltalk.general_system() == smalltalk.SYSTEM_GENERAL


def test_general_system_without_tables(monkeypatch):
    """目录为空时不拼【可查询范围】块：空清单等于告诉模型「系统什么都没有」。"""
    monkeypatch.setattr("app.services.metadata_store.metadata_store.tables",
                        lambda: [], raising=False)
    assert "可查询表：" not in smalltalk.general_system()


def test_general_prompt_forbids_fabricating_internal_rules():
    """制度/流程类问题没有资料时不许凭常识作答——这是「无背书但不编」的唯一约束。"""
    system = smalltalk.SYSTEM_GENERAL
    assert "制度" in system and "严禁" in system
    assert "【可查询范围" in system     # 范围约束也必须写在固定段，不依赖目录是否存在
