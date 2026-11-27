"""日终种子日期对齐（P2）：把 dwd_shop 的测试数据整体平移到 T-1。

背景：种子数据日期固定在 2026-11-13 ~ 11-22，隔天会偏离「昨天」，导致巡检断流、
「昨天」类提问无数据。本脚本按 delta 天平移所有日期列，保持表间相对关系。

用法：
  python -m worker.align_seed_dates            # 默认 dry-run，只打印将要执行的 SQL
  python -m worker.align_seed_dates --apply    # 实际执行

注意：
- 平移后 `eval_v1_seed` 里的固定日期用例（C021–C050）会与数据错位，需要重新取基准值；
- 只动测试数仓 dwd_shop，不碰业务库 datapro；
- 重复执行是幂等的：最大分区已是昨天时 delta=0。
"""
import argparse
from datetime import date, timedelta

import pymysql

# 需要平移的日期列（表 -> 列）
DATE_COLUMNS = {
    "dwd_order_detail_di": ["dt"],
    "dwd_user_di": ["register_date", "login_date", "dt"],
    "dws_category_daily": ["dt"],
    "dws_channel_daily": ["dt"],
    "dws_goods_daily": ["dt"],
    "dwd_campaign_daily": ["dt"],
    "dwd_refund_detail_di": ["apply_date", "finish_date", "dt"],
    "dim_campaign": ["start_date", "end_date"],
}


def compute_delta(max_dt: date, today: date) -> int:
    """目标是把最大分区平移到昨天（T-1）。"""
    return (today - timedelta(days=1) - max_dt).days


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实际执行；不加则只 dry-run")
    args = ap.parse_args()

    conn = pymysql.connect(host="127.0.0.1", port=3306, user="root", password="123456",
                           database="dwd_shop", charset="utf8mb4", autocommit=False)
    cur = conn.cursor()
    cur.execute("SELECT MAX(dt) FROM dwd_order_detail_di")
    max_dt = cur.fetchone()[0]
    if max_dt is None:
        raise SystemExit("订单表没有数据，无法计算对齐偏移")
    delta = compute_delta(max_dt, date.today())
    print(f"当前最大分区 {max_dt}，目标 T-1，需平移 {delta} 天")
    if delta == 0:
        print("已对齐，无需处理")
        return

    statements = []
    for table, columns in DATE_COLUMNS.items():
        for col in columns:
            statements.append(
                (f"UPDATE {table} SET {col} = DATE_ADD({col}, INTERVAL {delta} DAY) "
                 f"WHERE {col} IS NOT NULL", table, col))
    for sql, table, col in statements:
        print(f"  {sql}")
    if not args.apply:
        print("dry-run：未修改数据；确认后加 --apply")
        return

    for sql, _, _ in statements:
        cur.execute(sql)
    conn.commit()
    print(f"已完成 {len(statements)} 个日期列平移（{delta} 天）")
    print("提醒：固定日期评测用例需要重新取基准值后再跑 eval。")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
