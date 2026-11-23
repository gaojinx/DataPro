"""审计模块（HLD §3 / 权限设计 §3）：旁路异步写，不阻塞主流程。"""
import json
import logging

from app.core.trace import current_trace_id
from app.services import persist

logger = logging.getLogger("datapro.audit")


class AuditService:
    async def record(self, node: str, ctx, payload: dict | None = None):
        entry = {
            "trace_id": current_trace_id(),
            "user_id": ctx.user_id,
            "node": node,
            "question_digest": ctx.question[:200],
            "sql_text": getattr(ctx, "generated_sql", None),
            "payload": json.dumps(payload or {}, ensure_ascii=False, default=str)[:2000],
        }
        # 审计落库（S03）：后台任务写 audit_log，失败仅记日志不重试
        logger.info("AUDIT %s", json.dumps(entry, ensure_ascii=False))
        persist.spawn(persist.save_audit(entry))


audit_service = AuditService()
