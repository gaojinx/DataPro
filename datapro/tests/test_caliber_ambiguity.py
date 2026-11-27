"""口径歧义检测单测（P1）：同词多口径先澄清，选择解析不误伤。"""
from app.services.caliber_ambiguity import (ambiguous_groups, clarify_text, match_choice,
                                            option_view)
from app.services.recall import SEED_CALIBERS


def _options():
    return [option_view(c) for c in SEED_CALIBERS if c["metric_id"] in ("M004", "M005")]


def test_same_term_two_calibers_is_ambiguous():
    groups = ambiguous_groups(SEED_CALIBERS, "查一下昨天的新增用户")
    assert len(groups) == 1
    assert {o["metric_id"] for o in groups[0]["options"]} == {"M004", "M005"}
    assert "新增用户" in clarify_text(groups[0])


def test_single_caliber_not_ambiguous():
    assert ambiguous_groups(SEED_CALIBERS, "昨天的支付订单数") == []
    assert ambiguous_groups(SEED_CALIBERS, "查一下销售额") == []


def test_choice_matching():
    options = _options()
    assert match_choice("A", options)["metric_id"] == "M004"
    assert match_choice("第二个", options)["metric_id"] == "M005"
    assert match_choice("按支付口径", options)["metric_id"] == "M005"
    assert match_choice("注册的", options)["metric_id"] == "M004"
    assert match_choice("M005", options)["metric_id"] == "M005"
    assert match_choice("随便看看", options) is None
    assert match_choice("A", []) is None