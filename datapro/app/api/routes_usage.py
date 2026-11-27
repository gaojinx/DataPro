"""S-F05 最小用量页：基于 query_record 的只读统计，不引入计量计费概念。

普通用户只能看本人；管理员显式传 all=1 时看全量。B14 的限流计数不在此处使用。
"""
import asyncio
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends

from app.services.auth import AuthUser, require_permission

router = APIRouter(prefix="/api/v1/usage", tags=["usage"])


@router.get("/summary")
async def usage_summary(days: int = 30, all: int = 0,
                        user: AuthUser = Depends(require_permission("chat.use"))):
    """近 N 天问答量、今日量、结果分布与每日趋势。"""
    days = min(max(days, 1), 365)
    admin_all = bool(all and user.role == "admin")

    def _load() -> dict:
        from sqlalchemy import func

        from app.db.models import QueryRecord
        from app.db.session import SessionLocal

        since = datetime.now() - timedelta(days=days)
        filters = [QueryRecord.created_at >= since]
        if not admin_all:
            filters.append(QueryRecord.username == user.username)

        with SessionLocal() as db:
            total = db.query(QueryRecord).filter(*filters).count()
            by_outcome = dict(db.query(QueryRecord.outcome, func.count())
                              .filter(*filters).group_by(QueryRecord.outcome).all())
            daily_rows = (db.query(func.date(QueryRecord.created_at), func.count())
                          .filter(*filters)
                          .group_by(func.date(QueryRecord.created_at))
                          .order_by(func.date(QueryRecord.created_at)).all())
        daily = [{"date": str(d)[:10], "count": int(c)} for d, c in daily_rows]
        today = datetime.now().date().isoformat()
        return {
            "scope": "all" if admin_all else "mine",
            "days": days,
            "total": int(total),
            "today": next((c["count"] for c in daily if c["date"] == today), 0),
            "by_outcome": {str(k or "unknown"): int(v) for k, v in by_outcome.items()},
            "daily": daily,
        }

    return await asyncio.to_thread(_load)
