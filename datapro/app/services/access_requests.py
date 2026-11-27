"""数据表权限自助申请（U-B05）：申请 → 审批 → 带有效期写入 `db_access`。

设计要点：
- 只做「补授权」旁路，不改状态机：拦截仍由校验阶段按 A02 表级 E003 完成；
- 申请前先查是否已有权限，避免制造空申请；
- 同一张表只允许一条 pending，防止刷单；
- 审批通过写 `db_access.expires_at`，到期后 `tables_for()` 自动不再放行，
  无需额外的撤销任务；永久授权只能由管理员在用户管理页整体设置。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

logger = logging.getLogger("datapro.access")

DEFAULT_DURATION_DAYS = 30
MAX_DURATION_DAYS = 365
STATUSES = ("pending", "approved", "rejected", "cancelled")


def _fmt(dt) -> str | None:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if isinstance(dt, datetime) else dt


def _to_dict(row) -> dict:
    return {
        "id": row.id, "requester": row.requester, "db_name": row.db_name,
        "table_name": row.table_name, "reason": row.reason,
        "duration_days": row.duration_days, "status": row.status,
        "reviewed_by": row.reviewed_by, "reviewed_at": _fmt(row.reviewed_at),
        "review_note": row.review_note, "expires_at": _fmt(row.expires_at),
        "created_at": _fmt(row.created_at),
    }


def _clamp_days(value: int | None) -> int:
    """有效期下限 1 天、上限 365 天；缺省 30 天。

    显式传 0 会被夹到 1 而不是回落默认值——静默放大到 30 天会让审批人以为只批了很短。
    """
    if value is None or value == "":
        return DEFAULT_DURATION_DAYS
    try:
        days = int(value)
    except (TypeError, ValueError):
        return DEFAULT_DURATION_DAYS
    return max(1, min(days, MAX_DURATION_DAYS))


def _next_no(db) -> str:
    """AR-0001 序列号：与导出号共用 id_sequence，原子分配不撞号。"""
    from sqlalchemy import text

    db.execute(text(
        "INSERT INTO id_sequence (name, next_val) VALUES ('access_request', LAST_INSERT_ID(1)) "
        "ON DUPLICATE KEY UPDATE next_val = LAST_INSERT_ID(next_val + 1)"))
    return f"AR-{int(db.execute(text('SELECT LAST_INSERT_ID()')).scalar_one()):04d}"


def _resolve_table(table_name: str) -> tuple[str, str]:
    """校验表存在并返回 (table_name, db_name)；元数据里没有的表不允许申请。"""
    from app.services.metadata_store import metadata_store

    wanted = (table_name or "").strip().lower()
    if not wanted:
        raise ValueError("表名必填")
    for t in metadata_store.tables():
        if str(t.get("table_name", "")).lower() == wanted:
            # 元数据字典里库名键是 schema；db_name 只作历史兼容
            return wanted, str(t.get("db_name") or t.get("schema") or "dwd_shop")
    raise ValueError(f"未找到数据表：{table_name}")


def create(requester: str, table_name: str, reason: str = "",
           duration_days: int = DEFAULT_DURATION_DAYS) -> dict:
    from app.db.models import AccessRequest
    from app.db.session import SessionLocal
    from app.services.db_access import db_access

    requester = (requester or "").strip()
    if not requester:
        raise ValueError("申请人必填")
    clean, db_name = _resolve_table(table_name)

    allowed = db_access.tables_for(requester)
    if allowed and clean in allowed:
        raise ValueError("您已具备该数据表的访问权限，无需申请")

    days = _clamp_days(duration_days)
    with SessionLocal() as db:
        pending = (db.query(AccessRequest)
                   .filter(AccessRequest.requester == requester,
                           AccessRequest.table_name == clean,
                           AccessRequest.status == "pending").one_or_none())
        if pending is not None:
            raise ValueError(f"该表已有待审批申请（{pending.id}），请等待管理员处理")
        row = AccessRequest(id=_next_no(db), requester=requester, db_name=db_name,
                            table_name=clean, reason=(reason or "")[:255],
                            duration_days=days, status="pending")
        db.add(row)
        db.commit()
        db.refresh(row)
        return _to_dict(row)


def list_requests(requester: str | None = None, status: str | None = None,
                  limit: int = 200) -> list[dict]:
    from app.db.models import AccessRequest
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        q = db.query(AccessRequest)
        if requester:
            q = q.filter(AccessRequest.requester == requester)
        if status:
            q = q.filter(AccessRequest.status == status)
        rows = (q.order_by(AccessRequest.created_at.desc(), AccessRequest.id.desc())
                .limit(max(1, min(limit, 500))).all())
        return [_to_dict(r) for r in rows]


def decide(request_id: str, action: str, reviewer: str, note: str = "",
           duration_days: int | None = None) -> dict:
    """审批：approve 授予带有效期的表权限；reject 只留痕。"""
    from app.db.models import AccessRequest
    from app.db.session import SessionLocal
    from app.services.db_access import db_access

    if action not in ("approve", "reject"):
        raise ValueError("action 只能是 approve 或 reject")

    with SessionLocal() as db:
        row = (db.query(AccessRequest).filter(AccessRequest.id == request_id)
               .with_for_update().one_or_none())
        if row is None:
            raise LookupError("申请不存在")
        if row.status != "pending":
            raise ValueError(f"该申请已处理（{row.status}）")
        if action == "reject":
            row.status = "rejected"
            row.reviewed_by = reviewer
            row.reviewed_at = datetime.now()
            row.review_note = (note or "")[:255]
            db.commit()
            db.refresh(row)
            return _to_dict(row)

        days = _clamp_days(duration_days if duration_days is not None else row.duration_days)
        expires_at = datetime.now() + timedelta(days=days)
        # 先授权再改状态：授权失败则申请保持 pending，管理员可重试，
        # 不会出现「显示已通过但实际没权限」的假成功
        db_access.grant_table(row.requester, row.table_name, granted_by=reviewer,
                              expires_at=expires_at, db_name=row.db_name)
        row.status = "approved"
        row.reviewed_by = reviewer
        row.reviewed_at = datetime.now()
        row.review_note = (note or "")[:255]
        row.duration_days = days
        row.expires_at = expires_at
        db.commit()
        db.refresh(row)
        return _to_dict(row)
