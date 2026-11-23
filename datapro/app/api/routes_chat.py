"""对话主接口：SSE 流式返回（接口规范 §4.1）。"""
import asyncio
import json
import uuid

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.config import settings
from app.core.errors import E003
from app.core.statemachine import Pipeline, PipelineContext
from app.core.trace import new_trace_id
from app.services.audit import audit_service
from app.services.intent import answer_service, intent_service
from app.services.generator import generator_service
from app.services.recall import recall_service
from app.services.executor import executor_service
from app.services.validator import validator_service

router = APIRouter(prefix="/api/v1", tags=["chat"])

# 骨架内存会话存储；G2 接 Redis（TTL 30min）
_SESSIONS: dict[str, list[dict]] = {}


class ChatRequest(BaseModel):
    session_id: str | None = None
    message: str


def build_pipeline() -> Pipeline:
    return Pipeline(intent_service, recall_service, generator_service,
                    validator_service, executor_service, answer_service)


def sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"


@router.post("/chat")
async def chat(req: ChatRequest, user_id: str = "dev_user"):
    # 降级开关（Runbook §3：一键停服）
    if not settings.feature_data_pro_enabled:
        raise HTTPException(status_code=503, detail="DataPro 维护中")
    # 写操作/越界统一由状态机判定（保证 SSE 事件格式与审计记录完整）

    session_id = req.session_id or uuid.uuid4().hex
    history = _SESSIONS.setdefault(session_id, [])
    history.append({"role": "user", "content": req.message})
    trace_id = new_trace_id()

    pipeline = build_pipeline()
    ctx = PipelineContext(question=req.message, user_id=user_id,
                          history=history, session_id=session_id)

    async def stream():
        yield sse({"type": "node", "node": "START", "session_id": session_id, "trace_id": trace_id})
        try:
            async for event in pipeline.run(ctx):
                await audit_service.record(event.get("node", event.get("type")), ctx, event)
                yield sse(event)
        finally:
            history.append({"role": "assistant",
                            "content": json.dumps(ctx.result or {}, ensure_ascii=False, default=str)})

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.get("/sessions")
async def list_sessions():
    return [{"session_id": sid, "turns": len(msgs)} for sid, msgs in _SESSIONS.items()]


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    _SESSIONS.pop(session_id, None)
    return {"deleted": session_id}


@router.post("/queries/{query_id}/feedback")
async def feedback(query_id: str, rating: str, reason: str | None = None):
    # 写 query_record.user_feedback；👍 自动进历史问答候选池（口径库模板 §3）
    return {"query_id": query_id, "rating": rating, "reason": reason}
