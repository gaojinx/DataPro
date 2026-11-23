"""对话主接口：SSE 流式返回（接口规范 §4.1）。"""
import asyncio
import json
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.config import settings
from app.core.errors import E003
from app.core.statemachine import Pipeline, PipelineContext
from app.core.trace import new_trace_id
from app.services import persist
from app.services.audit import audit_service
from app.services.auth import AuthUser, get_current_user
from app.services.intent import answer_service, intent_service
from app.services.generator import generator_service
from app.services.recall import recall_service
from app.services.executor import executor_service
from app.services.validator import validator_service

router = APIRouter(prefix="/api/v1", tags=["chat"])

# 骨架内存会话存储；G2 接 Redis（TTL 30min）
_SESSIONS: dict[str, list[dict]] = {}
_SESSION_OWNERS: dict[str, str] = {}


class ChatRequest(BaseModel):
    session_id: str | None = None
    message: str


def build_pipeline() -> Pipeline:
    return Pipeline(intent_service, recall_service, generator_service,
                    validator_service, executor_service, answer_service)


def sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"


@router.post("/chat")
async def chat(req: ChatRequest, user: AuthUser = Depends(get_current_user)):
    # 降级开关（Runbook §3：一键停服）
    if not settings.feature_data_pro_enabled:
        raise HTTPException(status_code=503, detail="DataPro 维护中")
    # 写操作/越界统一由状态机判定（保证 SSE 事件格式与审计记录完整）

    session_id = req.session_id or uuid.uuid4().hex
    owner = _SESSION_OWNERS.get(session_id)
    if owner and owner != user.username and user.role != "admin":
        raise HTTPException(status_code=403, detail="无权访问该会话")
    _SESSION_OWNERS.setdefault(session_id, user.username)
    history = _SESSIONS.setdefault(session_id, [])
    history.append({"role": "user", "content": req.message})
    trace_id = new_trace_id()

    pipeline = build_pipeline()
    ctx = PipelineContext(question=req.message, user_id=user.username, trace_id=trace_id,
                          history=history, session_id=session_id)

    async def stream():
        yield sse({"type": "node", "node": "START", "session_id": session_id, "trace_id": trace_id})
        started = time.perf_counter()
        saved = False
        answer = ""
        try:
            async for event in pipeline.run(ctx):
                await audit_service.record(event.get("node", event.get("type")), ctx, event)
                etype = event.get("type")
                if etype == "result":                 # B08：结果落库并回传 query_id
                    answer = event.get("summary") or ""
                    query_id = await persist.save_query(
                        ctx, answer, int((time.perf_counter() - started) * 1000))
                    if query_id:
                        event["query_id"] = query_id
                    saved = True
                elif etype in ("clarify", "reject", "escalate", "error"):
                    answer = event.get("question") or event.get("message") or answer
                yield sse(event)
        finally:
            if not saved:                              # 澄清/拒答/异常也留痕
                await persist.save_query(ctx, answer, int((time.perf_counter() - started) * 1000))
            history.append({"role": "assistant",
                            "content": json.dumps(ctx.result or {}, ensure_ascii=False, default=str)})

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.get("/sessions")
async def list_sessions(user: AuthUser = Depends(get_current_user)):
    return [{"session_id": sid, "turns": len(msgs)}
            for sid, msgs in _SESSIONS.items()
            if user.role == "admin" or _SESSION_OWNERS.get(sid) == user.username]


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, user: AuthUser = Depends(get_current_user)):
    owner = _SESSION_OWNERS.get(session_id)
    if owner and owner != user.username and user.role != "admin":
        raise HTTPException(status_code=403, detail="无权删除该会话")
    _SESSIONS.pop(session_id, None)
    _SESSION_OWNERS.pop(session_id, None)
    return {"deleted": session_id}


@router.post("/queries/{query_id}/feedback")
async def feedback(query_id: str, rating: str, reason: str | None = None,
                   _: AuthUser = Depends(get_current_user)):
    # B08：写 query_record.user_feedback；👍 后续可进历史问答候选池（口径库模板 §3）
    if rating not in ("up", "down"):
        raise HTTPException(status_code=400, detail="rating 仅支持 up/down")
    recorded = await persist.set_feedback(query_id, rating, reason)
    return {"query_id": query_id, "rating": rating, "reason": reason, "recorded": recorded}
