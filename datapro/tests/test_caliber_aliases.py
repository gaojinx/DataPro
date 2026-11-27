"""口径别名：口语问法要能命中，且大小写不能成为障碍。

`GMV`/`ROAS`/`DAU`/`广告ROI` 这类带大写的别名曾经永远匹配不上——问题在进入匹配前
已被统一转小写，而比较时用的是原样别名。
"""
import pytest

from app.services.recall import SEED_CALIBERS, _caliber_match


def _match(question: str) -> set[str]:
    q = question.lower()
    return {c["metric_id"] for c in SEED_CALIBERS if _caliber_match(c, q)}


@pytest.mark.parametrize("question,expected", [
    ("最近7天广告ROI", "M014"),
    ("最近7天广告roi", "M014"),
    ("最近7天ROAS", "M014"),
    ("投放投产比是多少", "M014"),
    ("最近7天GMV", "M006"),
    ("最近7天gmv", "M006"),
    ("上周营业额", "M006"),
    ("最近流水怎么样", "M006"),
    ("昨天DAU", "M001"),
    ("昨天日活跃", "M001"),
    ("昨天客单是多少", "M008"),
    ("退款比例高不高", "M012"),
    ("广告费花了多少", "M013"),
    ("退货单数", "M015"),
])
def test_colloquial_phrasings_hit_the_intended_caliber(question, expected):
    assert expected in _match(question), f"{question} 应命中 {expected}，实际 {_match(question)}"


def test_uppercase_alias_matches_lowercased_question():
    roi = next(c for c in SEED_CALIBERS if c["metric_id"] == "M014")
    assert _caliber_match(roi, "最近7天gmv和roas对比".lower())
    # 小写别名同样可用
    assert _caliber_match(roi, "最近7天广告roi")


def test_every_seed_caliber_has_aliases():
    """别名是召回的主要入口：没有别名的指标只能被规范名命中，口语问法会直接漏。"""
    missing = [c["metric_id"] for c in SEED_CALIBERS if not c.get("aliases")]
    assert not missing, f"以下口径缺别名：{missing}"


def test_new_user_aliases_keep_both_calibers_ambiguous():
    """「新客」同时属于注册口径与下单口径：靠别名制造歧义是设计，不是 bug。"""
    assert {"M004", "M005"} <= _match("昨天新客有多少")
