"""审计模块（HLD §3 / 权限设计 §3）：旁路异步写，不阻塞主流程。"""
import json
import logging

from app.core.trace import current_trace_id

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
        # 骨架阶段写日志；接入 app_database_url 后异步落 audit_log 表（保留 ≥180 天）
        logger.info("AUDIT %s", json.dumps(entry, ensure_ascii=False))
        # TODO(G2): asyncio.create_task 写 MySQL audit_log，失败仅记日志不重试


audit_service = AuditService()
