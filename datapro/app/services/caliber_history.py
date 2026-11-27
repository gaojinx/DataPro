"""口径变更历史（P2）：每次写入前把旧值留档。

为什么单独成服务：口径是「先口径后取数」的凭据，改了必须有据可查；
但历史写入属于旁路，不能因为它失败而阻断导入或管理端保存。
"""
from __future__ import annotations

import logging

logger = logging.getLogger("datapro.caliber_history")

_FIELDS = ("metric_id", "version", "metric_name", "aliases", "definition", "logic",
           "owner", "status", "source_table")


def snapshot(db, caliber, change_type: str = "update", changed_by: str = "") -> None:
    """把口径当前值写入历史表；调用方负责在【修改前】调用（create 在 flush 后调用）。

    失败只记日志：历史缺失不应阻断业务写入。
    """
    try:
        from app.db.models import MetricCaliberHistory

        payload = {f: getattr(caliber, f, None) for f in _FIELDS}
        db.add(MetricCaliberHistory(**payload, change_type=change_type,
                                    changed_by=changed_by or ""))
    except Exception as exc:  # noqa: BLE001 —— 旁路，失败不影响主流程
        logger.warning("口径历史写入失败（%s）：%s", getattr(caliber, "metric_id", "?"), exc)
