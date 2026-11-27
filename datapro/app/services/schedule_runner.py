"""定时任务执行器：question 走取数状态机，ingest 走采集器；调度交给 APScheduler。"""
import asyncio
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.cron import CronTrigger

from app.core.statemachine import Pipeline, PipelineContext
from app.core.trace import new_trace_id
from app.services.executor import executor_service
from app.services.generator import generator_service
from app.services.intent import answer_service, intent_service
from app.services.recall import recall_service
from app.services.schedule_registry import schedule_registry
from app.services.validator import validator_service

logger = logging.getLogger("datapro.schedule")

TZ = ZoneInfo("Asia/Shanghai")
JOB_PREFIX = "sched:"
SYNC_SECONDS = 30
_SIGNATURES: dict[str, str] = {}     # job_id → 任务签名（判断是否需要重建作业）


async def run_task(task: dict) -> dict:
    """执行单个任务并把结果写入运行历史；type=ingest 分派到采集器。"""
    if str(task.get("type") or "question").strip().lower() == "ingest":
        return await _run_ingest(task)
    pipeline = Pipeline(intent_service, recall_service, generator_service,
                        validator_service, executor_service, answer_service)
    ctx = PipelineContext(question=task["question"], user_id=task.get("owner") or "system",
                          trace_id=new_trace_id(),
                          history=[{"role": "user", "content": task["question"]}],
                          session_id=f"sched-{task['id']}")
    result, reject, err = None, None, None
    try:
        async for ev in pipeline.run(ctx):
            etype = ev.get("type")
            if etype == "result":
                result = ev
            elif etype in ("reject", "escalate", "error"):
                reject = ev
    except Exception as exc:  # noqa: BLE001 —— 单次任务失败不影响调度
        err = str(exc)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if result:
        run = {"ts": ts, "status": "ok", "row_count": result.get("row_count"),
               "summary": result.get("summary"), "sql": result.get("sql")}
    elif reject:
        run = {"ts": ts, "status": "rejected",
               "error": f"[{reject.get('reason_code')}] {reject.get('message', '')}"}
    else:
        run = {"ts": ts, "status": "failed", "error": err or "未返回结果"}
    schedule_registry.record_run(task["id"], run)
    logger.info("定时任务 %s(%s) 执行完成：%s", task.get("name"), task["id"], run["status"])
    return run


async def _run_ingest(task: dict) -> dict:
    """采集型任务：同步采集器放到线程里跑，避免阻塞调度事件循环。"""
    from worker.xianzhi_collector import XianzhiCollector
    from worker.xianzhi_driver import CollectorConfig

    code = str(task.get("ingest_code") or "xianzhi").strip().lower()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if code != "xianzhi":
        run = {"ts": ts, "status": "failed", "error": "UNSUPPORTED_INGEST"}
        schedule_registry.record_run(task["id"], run)
        return run

    config_path = (Path(__file__).resolve().parent.parent.parent / "data" / "ingest" /
                   f"{code}.json")
    try:
        config = CollectorConfig.from_file(config_path)
        collector = XianzhiCollector(config=config, triggered_by=f"scheduler:{task['id']}")
        result = await asyncio.to_thread(collector.run, config=config)
        status = "ok" if result.get("status") in ("success", "skipped") else "failed"
        run = {
            "ts": ts,
            "status": status,
            "row_count": result.get("rows_loaded", 0),
            "summary": (f"window={result.get('window_start')}~{result.get('window_end')}; "
                        f"rows_loaded={result.get('rows_loaded', 0)}; "
                        f"rows_rejected={result.get('rows_rejected', 0)}"),
            "data_as_of": result.get("data_as_of", ""),
            "batch_no": result.get("batch_no", ""),
        }
        if result.get("error_code"):
            run["error"] = result["error_code"]     # 只有原因码，不含明文/HTML
    except Exception as exc:  # noqa: BLE001 —— 只记类型名，绝不回显异常原文
        run = {"ts": ts, "status": "failed", "error": f"INGEST:{type(exc).__name__}"}
    schedule_registry.record_run(task["id"], run)
    logger.info("采集定时任务 %s(%s) 执行完成：%s", task.get("name"), task["id"], run["status"])
    return run


async def _safe_run(task: dict):
    try:
        await run_task(task)
    except Exception as exc:  # noqa: BLE001
        logger.error("定时任务 %s 执行异常：%s", task.get("id"), type(exc).__name__)


def _cron(task: dict) -> BaseTrigger | None:
    """每日/每周固定时间 → cron 触发器（weekday 1=周一 对应 APScheduler 0=周一）。"""
    try:
        hour, minute = (int(x) for x in str(task.get("time") or "09:00").split(":", 1))
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    if task.get("freq") == "weekly":
        return CronTrigger(hour=hour, minute=minute,
                           day_of_week=(int(task.get("weekday") or 1) - 1) % 7, timezone=TZ)
    return CronTrigger(hour=hour, minute=minute, timezone=TZ)


def _signature(task: dict) -> str:
    return "|".join(str(task.get(k)) for k in
                    ("freq", "time", "weekday", "question", "enabled", "type", "ingest_code"))


def sync_jobs(scheduler: AsyncIOScheduler) -> None:
    """把 JSON 里的任务对齐成调度作业：新增/修改/删除都在下一轮同步生效。"""
    wanted = {}
    for task in schedule_registry.list():
        if not task.get("enabled"):
            continue
        trigger = _cron(task)
        if trigger is not None:
            job_id = f"{JOB_PREFIX}{task['id']}"
            wanted[job_id] = (task, trigger, _signature(task))

    for job in scheduler.get_jobs():
        if job.id.startswith(JOB_PREFIX) and job.id not in wanted:
            scheduler.remove_job(job.id)
            _SIGNATURES.pop(job.id, None)

    for job_id, (task, trigger, signature) in wanted.items():
        if scheduler.get_job(job_id) is not None and _SIGNATURES.get(job_id) == signature:
            continue
        scheduler.add_job(_safe_run, trigger, args=[task], id=job_id, replace_existing=True,
                          misfire_grace_time=3600, coalesce=True, max_instances=1)
        _SIGNATURES[job_id] = signature


def start_scheduler() -> AsyncIOScheduler:
    """随 FastAPI lifespan 启停；每 30 秒把 JSON 配置同步成 cron 作业。"""
    scheduler = AsyncIOScheduler(timezone=TZ)
    scheduler.add_job(sync_jobs, "interval", seconds=SYNC_SECONDS, args=[scheduler],
                      id="sched-sync", max_instances=1, coalesce=True)
    sync_jobs(scheduler)
    scheduler.start()
    logger.info("定时任务调度已启动（APScheduler，%s 秒同步一次）", SYNC_SECONDS)
    return scheduler
