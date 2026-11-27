"""巡检 worker（APScheduler 日级任务，HLD §3 巡检调度）。

规则可配：空值率 / 断流 / 行数波动；失败写结果文件并按需走 IM webhook。
运行：python -m worker.inspection_worker
"""
import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import settings
from app.services.inspection import inspection_service
from app.services.session_janitor import sweep as sweep_sessions

logger = logging.getLogger("datapro.inspection")


async def run_inspection() -> list[dict]:
    if not settings.feature_inspection_enabled:
        return []
    result = await asyncio.to_thread(inspection_service.run_all, "scheduler")
    return result.get("items", [])


async def run_janitor() -> dict:
    """S07：每小时回收过期会话账号与临时表。"""
    return await asyncio.to_thread(sweep_sessions)


async def run_retention() -> dict:
    """生命周期清理：按保留期分批删除审计与取数记录（每日一次）。"""
    from app.services.persist import purge_expired

    stats = await purge_expired(settings.audit_retention_days,
                                settings.query_retention_days,
                                settings.message_retention_days)
    if stats:
        logger.info("过期数据清理完成：%s", stats)
    return stats


async def run_partition_clock() -> None:
    """U-B01：定期刷新真实最新分区，保证相对时间窗不落在空分区。"""
    from app.services.partition_clock import partition_clock

    await asyncio.to_thread(partition_clock.refresh)


def main() -> None:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(run_inspection, "cron", hour=6, minute=0)   # 每日 06:00
    scheduler.add_job(run_retention, "cron", hour=6, minute=30)   # 每日 06:30 清理过期记录
    scheduler.add_job(run_janitor, "interval", hours=1)           # 每小时会话回收
    scheduler.add_job(run_partition_clock, "interval", minutes=10)  # 分区锚点（TTL 10min）
    scheduler.start()
    logger.info("DataPro 巡检 worker 已启动")
    asyncio.get_event_loop().run_forever()


if __name__ == "__main__":
    main()
