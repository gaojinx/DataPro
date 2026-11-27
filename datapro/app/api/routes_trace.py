"""标记码溯源：按 DP-xxxx 标记码回查该次问答的原始问题与结果。"""
import json
import re

from fastapi import APIRouter, Depends, HTTPException

from app.db.models import QueryRecord
from app.db.session import SessionLocal
from app.services.auth import AuthUser, get_current_user

router = APIRouter(prefix="/api/v1/trace", tags=["trace"])

# DP-0072 / dp-72 / 72 均可
CODE_RE = re.compile(r"^(?:DP-?)?(\d{1,9})$", re.IGNORECASE)


def _context(raw: str | None) -> list[str]:
    try:
        data = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []
    return [str(x) for x in data] if isinstance(data, list) else []


@router.get("/{code}")
def get_trace(code: str, user: AuthUser = Depends(get_current_user)):
    m = CODE_RE.match(code.strip())
    if not m:
        raise HTTPException(status_code=400, detail="标记码格式不正确，示例：DP-0072")
    db = SessionLocal()
    try:
        rec = db.get(QueryRecord, int(m.group(1)))
        if rec is None:
            raise HTTPException(status_code=404, detail="未找到该标记码对应的对话")
        # 水平越权防线：标记码是自增 ID（DP-0001 可枚举），
        # 普通用户只能回看本人问答；管理员可溯源全部记录。
        if user.role != "admin" and rec.username != user.username:
            raise HTTPException(status_code=404, detail="未找到该标记码对应的对话")
        return {
            "code": f"DP-{rec.id:04d}",
            "question": rec.question,
            "question_context": _context(rec.question_context),
            "answer": rec.final_answer or "",
            "sql": rec.generated_sql,
            "session_id": rec.session_id,
            "trace_id": rec.trace_id,
            "row_count": rec.row_count,
            "latency_ms": rec.latency_ms,
            "created_at": rec.created_at.strftime("%Y-%m-%d %H:%M:%S") if rec.created_at else "",
        }
    finally:
        db.close()
