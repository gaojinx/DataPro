"""日终种子日期对齐脚本：偏移计算与覆盖范围。"""
from datetime import date

from worker.align_seed_dates import DATE_COLUMNS, compute_delta


def test_compute_delta():
    today = date(2026, 11, 24)
    assert compute_delta(date(2026, 11, 22), today) == 1   # 最大分区是前天 → 平移 1 天
    assert compute_delta(date(2026, 11, 23), today) == 0   # 已是昨天 → 不动
    assert compute_delta(date(2026, 11, 20), today) == 3


def test_date_columns_cover_derived_tables():
    # 汇总/派生表必须一起平移，否则与订单表对不上
    for table in ("dwd_order_detail_di", "dws_category_daily", "dws_channel_daily",
                  "dws_goods_daily", "dwd_campaign_daily", "dwd_refund_detail_di"):
        assert table in DATE_COLUMNS
    assert "register_date" in DATE_COLUMNS["dwd_user_di"]
    assert "start_date" in DATE_COLUMNS["dim_campaign"]
