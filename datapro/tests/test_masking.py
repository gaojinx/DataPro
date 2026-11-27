"""返回层脱敏（S04）单测：格式规则、列名规则、误伤与开关。"""
from app.services.masking import mask_rows, mask_value


def test_value_rules_mask_pii():
    phone, hits = mask_value("联系 13812345678 下单")
    assert phone == "联系 138****5678 下单" and hits["phone"] == 1
    idc, hits = mask_value("身份证 11010119900307123X")
    assert idc == "身份证 110101********123X" and hits["id_card"] == 1
    bank, hits = mask_value("卡号 6222021234567890123")
    assert bank.endswith("0123") and "*" in bank and hits["bank_card"] == 1
    mail, hits = mask_value("邮箱 zhangsan@example.com")
    assert mail == "邮箱 z***@example.com" and hits["email"] == 1


def test_numbers_and_business_text_untouched():
    rows = [{"city": "北京市", "channel": "抖音", "amount": 72470.55, "cnt": 43}]
    out, stats = mask_rows(rows, ["city", "channel", "amount", "cnt"], enabled=True)
    assert out == rows and stats == {}


def test_column_name_rule_masks_whole_value():
    # 列名命中 PII 关键词 → 整列打码，即使值不符合固定格式
    rows = [{"user_phone": "abc123", "user_name": "张三", "channel_name": "抖音"}]
    out, stats = mask_rows(rows, ["user_phone", "user_name", "channel_name"], enabled=True)
    assert out[0]["user_phone"] == "a****3"
    assert out[0]["user_name"] != "张三" and "*" in out[0]["user_name"]
    assert out[0]["channel_name"] == "抖音"      # 业务列不能被误伤
    assert stats["column"] == 2


def test_disabled_bypasses_masking():
    rows = [{"phone": "13812345678"}]
    out, stats = mask_rows(rows, ["phone"], enabled=False)
    assert out == rows and stats == {}


def test_extra_keywords_configurable():
    rows = [{"staff_no": "A123456"}]
    out, stats = mask_rows(rows, ["staff_no"], extra_keywords=("staff_no",), enabled=True)
    assert out[0]["staff_no"] != "A123456" and stats["column"] == 1
