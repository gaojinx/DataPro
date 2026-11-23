"""巡检 worker（APScheduler 日级任务，HLD §3 巡检调度）。

规则可配：空值率 / 断流 / 行数波动；告警走 IM webhook（骨架打印日志）。
运行：python -m worker.inspection_worker
"""
import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import settings
from app.services.session_janitor import sweep as sweep_sessions

logger = logging.getLogger("datapro.inspection")

# TODO(G4): 巡检规则配置化；告警接企业 IM webhook
INSPECTION_RULES = [
    {"table": "dwd_order_detail_di", "check": "rowcount_drop", "threshold": 0.5},   # 行数较昨日下降超 50%
    {"table": "dwd_order_detail_di", "check": "null_ratio", "column": "amount", "threshold": 0.05},
]


async def run_inspection() -> list[dict]:
    if not settings.feature_inspection_enabled:
        return []
    # TODO(G4): 连接数仓执行规则，产出巡检结果并写入 /inspections 数据源
    logger.info("巡检任务触发，规则数=%s（骨架模式，仅记录）", len(INSPECTION_RULES))
    return []


async def run_janitor() -> dict:
    """S07：每小时回收过期会话账号与临时表。"""
    return await asyncio.to_thread(sweep_sessions)


def main() -> None:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(run_inspection, "cron", hour=6, minute=0)  # 每日 06:00
    scheduler.add_job(run_janitor, "interval", hours=1)          # 每小时会话回收
    scheduler.start()
    logger.info("DataPro 巡检 worker 已启动")
    asyncio.get_event_loop().run_forever()


if __name__ == "__main__":
    main()
