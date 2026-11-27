"""对话主接口：SSE 流式返回（接口规范 §4.1）。"""
import asyncio
import json
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.config import settings
from app.core.errors import E003, E007
from app.core.statemachine import Pipeline, PipelineContext
from app.core.trace import new_trace_id
from app.services import persist, tracing
from app.services.audit import audit_service
from app.services.auth import AuthUser, get_current_user, require_permission
from app.services.auto_learn import auto_learn
from app.services.caliber_ambiguity import match_choice
from app.services.intent import answer_service, intent_service
from app.services.generator import generator_service
from app.services.memory_store import memory_store
from app.services.recall import recall_service
from app.services.executor import executor_service
from app.services.feedback_pool import feedback_pool
from app.services.metrics import metrics
from app.services.rate_limit import rate_limiter
from app.services.skill_registry import skill_registry
from app.services.smalltalk import answer_general, should_answer_general
from app.services.session_state import session_state
from app.services.validator import validator_service
from app.services.vision import is_image_entry, vision_service

router = APIRouter(prefix="/api/v1", tags=["chat"])


class ChatRequest(BaseModel):
    session_id: str | None = None
    message: str
    # 附件引用快照（0018）：[{file_id,name,ext,size,origin}]，随消息落库供历史恢复
    attachments: list[dict] | None = None


# 终态事件的兜底错误码：状态机已自带 reason_code，这里只防老事件/自定义事件漏带，
# 否则记录里 outcome=escalate 但 error_code 为空，按 E00x 统计错误率时整段消失。
_REASON_CODES = {"SCAN_TOO_LARGE": "E009", "NO_SQL": "E010", "IR_INCONSISTENT": "E010"}

_ATTACHMENT_KEYS = ("file_id", "name", "ext", "size", "origin")
_MAX_ATTACHMENTS = 10


def _sanitize_attachments(raw: list[dict] | None, session_id: str, user: AuthUser) -> list[dict] | None:
    """附件引用白名单化并校验归属：只保留白名单字段；文件必须真实存在于该会话且可访问。

    客户端传什么都行，但落库的只会是「本会话内、本人（或历史公开）文件」的指针，
    防止把任意 file_id 塞进消息里当作跳板。
    """
    if not raw:
        return None
    from app.services.file_store import file_store

    out: list[dict] = []
    for item in raw[:_MAX_ATTACHMENTS]:
        fid = str(item.get("file_id") or "")
        entry = file_store.get(fid) if fid else None
        if not entry or entry.get("session_id") != session_id:
            continue                        # 文件不存在或不在本会话：丢弃该引用
        owner = entry.get("owner")
        if owner and owner != user.username and user.role != "admin":
            continue                        # 无权引用他人文件
        out.append({k: entry.get(k) for k in _ATTACHMENT_KEYS})
    return out or None


async def _can_read_session(session_id: str, user: AuthUser) -> bool:
    """历史消息读取权限：管理员、会话认领者；owner 记录过期时回退到「本人参与过」。"""
    if user.role == "admin":
        return True
    owner = await session_state.owner(session_id)
    if owner:
        return owner == user.username
    from app.services.persist import session_has_user

    return await session_has_user(session_id, user.username)


def fallback_reason_code(event: dict) -> str:
    if event.get("type") not in ("reject", "escalate", "error"):
        return ""
    return _REASON_CODES.get(str(event.get("reason") or ""), "E010")


def step_entry(event: dict) -> dict | None:
    """节点事件 → 步骤轨迹条目（F54）：START/DONE 是框架事件不进时间轴。

    与前端 handleEvent('node') 的构建规则保持一致：retry>0 表示对上一步的
    第 N 次修正，前端还原时会覆盖而不是追加。
    """
    if event.get("type") != "node":
        return None
    node = event.get("node")
    if not node or node in ("START", "DONE"):
        return None
    try:
        retry = int(event.get("retry") or 0)
    except (TypeError, ValueError):
        retry = 0
    return {"node": node, "retry": retry}


def build_pipeline() -> Pipeline:
    return Pipeline(intent_service, recall_service, generator_service,
                    validator_service, executor_service, answer_service)


def sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"


@router.post("/chat")
async def chat(req: ChatRequest, user: AuthUser = Depends(require_permission("chat.use"))):
    # 降级开关（Runbook §3：一键停服）
    if not settings.feature_data_pro_enabled:
        raise HTTPException(status_code=503, detail="DataPro 维护中")
    # 限流（B14/E007）：按用户每分钟计数，超限返回 429
    wait = await rate_limiter.hit(f"chat:{user.username}")
    if wait is not None:
        metrics.inc("datapro_rate_limited_total")
        raise HTTPException(status_code=E007.http_status,
                            detail=f"[E007] {E007.default_message}（{wait} 秒后可重试）")
    # 写操作/越界统一由状态机判定（保证 SSE 事件格式与审计记录完整）

    session_id = req.session_id or uuid.uuid4().hex
    owner = await session_state.owner(session_id)
    if owner and owner != user.username and user.role != "admin":
        raise HTTPException(status_code=403, detail="无权访问该会话")
    # 原子认领：并发同 session_id 时，后到者会被拒绝，避免跨用户上下文混用
    claimed = await session_state.claim_owner(session_id, user.username)
    if not claimed and user.role != "admin":
        raise HTTPException(status_code=403, detail="无权访问该会话")

    # 口径歧义（P1）：上一轮问过口径时，本轮先尝试解析用户的 A/B 选择
    pending = await session_state.pending_caliber(session_id)
    forced = match_choice(req.message, (pending or {}).get("options") or []) if pending else None
    question = req.message
    if forced:
        # 恢复原问题；口径由 forced_caliber_id 在召回时锁定，不能把"口径"字样拼进问题，
        # 否则会被意图识别当成口径问答（meta_qa）而不取数
        question = (pending or {}).get("question") or req.message
    if pending:
        await session_state.clear_pending_caliber(session_id)

    history = await session_state.add_user_message(session_id, req.message)
    trace_id = new_trace_id()

    pipeline = build_pipeline()
    ctx = PipelineContext(question=question, user_id=user.username, user_role=user.role,
                          trace_id=trace_id,
                          history=history, session_id=session_id,
                          clarify_count=await session_state.clarify_count(session_id),
                          prev_spec=await session_state.spec(session_id),
                          forced_caliber_id=forced.get("metric_id") if forced else None,
                          caliber_clarified=bool(pending and not forced),
                          memories=memory_store.for_user(user.username))
    # 预设回复技能（F38）：发送内容命中且配置了回复 → 直接回复，不走取数链路
    skill = skill_registry.match_answer(req.message)
    # 寒暄/能力说明/含糊输入：交给接入的 AI 模型回答（无模型时回退内置说明）
    # 口径选择（如"A"）必须走取数链路，不能被当成寒暄
    is_general = False if (skill or forced) else should_answer_general(req.message, history)

    mode = "skill" if skill else ("general" if is_general else "query")
    # 附件引用只解析一次：落库用它，图片理解也用它，避免两处各查一遍索引
    attachments = _sanitize_attachments(req.attachments, session_id, user)
    # 全量消息持久化：用户消息先落库（Redis 有 TTL，过期后仍可审计）；附件引用随消息持久化
    persist.spawn(persist.save_message(session_id, user.username, "user", req.message,
                                       kind=mode, trace_id=trace_id, attachments=attachments))

    async def stream():
        with tracing.trace(req.message, session_id=session_id, user_id=user.username,
                           tags=[mode]) as tr:
            ctx.langfuse_trace_id = tr.trace_id or ""
            yield sse({"type": "node", "node": "START", "session_id": session_id, "trace_id": trace_id})
            started = time.perf_counter()
            saved = False
            answer = ""
            reply_kind = mode
            terminal_payload: dict | None = None    # 终态事件快照（0019）：随助手消息落库
            step_trace: list[dict] = []             # 节点轨迹（F54）：还原步骤时间轴用

            # 图片理解：主模型不接受图片，先交给视觉模型转成文字描述。
            # 解析不出来就明确说明原因——绝不让模型在拿不到图的情况下自己编解释，
            # 那正是「图片分析报错」的真实成因：模型只看到文件名，只能猜。
            image_notes = ""
            image_skipped: list[dict] = []
            images = [e for e in (attachments or []) if is_image_entry(e)]
            if images:
                info = await vision_service.describe(images, req.message)
                image_notes = info.get("text") or ""
                image_skipped = info.get("skipped") or []
                if image_notes:
                    ctx.image_notes = image_notes
                yield sse({"type": "node", "node": "VISION",
                           "detail": (f"{len(info.get('used') or [])}/{len(images)} 张已识别"
                                      if image_notes else "图片未能识别")})
                step_trace.append({"node": "VISION", "retry": 0})

            if skill:
                events = preset_events(f"技能「{skill.get('name', '')}」的预设回复", skill.get("answer", ""))
            elif is_general:
                events = general_events(req.message, ctx.history, image_notes=image_notes,
                                        image_skipped=image_skipped)
            else:
                events = pipeline.run(ctx)
            metrics.inc("datapro_chat_requests_total", mode=mode)
            try:
                async for event in events:
                    await audit_service.record(event.get("node", event.get("type")), ctx, event)
                    etype = event.get("type")
                    if etype in ("result", "reject", "escalate", "clarify", "error"):
                        # 结果分类与错误码随记录落库：错误率统计不再依赖日志检索
                        ctx.outcome = etype
                        code = event.get("reason_code") or fallback_reason_code(event)
                        ctx.error_code = str(code) if code else ""
                        metrics.inc("datapro_chat_outcomes_total", type=etype)
                    if etype in ("reject", "error"):
                        metrics.inc("datapro_errors_total",
                                    code=str(event.get("reason_code") or "unknown"))
                    if etype == "node":
                        tr.event(f"node:{event.get('node')}", metadata={"detail": event.get("detail")})
                        entry = step_entry(event)
                        if entry:
                            step_trace.append(entry)
                    if etype == "result":                 # B08：结果落库并回传 query_id
                        answer = event.get("summary") or ""
                        query_id = await persist.save_query(
                            ctx, answer, int((time.perf_counter() - started) * 1000))
                        if query_id:
                            event["query_id"] = query_id
                            event["trace_code"] = f"DP-{query_id:04d}"   # 可复制的溯源标记码
                        tr.finish(output={"summary": answer, "sql": event.get("sql"),
                                          "row_count": event.get("row_count"), "query_id": query_id},
                                  metadata={"spec": event.get("spec_display"),
                                            "time_window": event.get("time_window")})
                        await session_state.clear_clarify(session_id)
                        await session_state.clear_pending_caliber(session_id)
                        if ctx.spec is not None:      # 结果轮：保存本轮槽位供下一轮继承
                            await session_state.set_spec(session_id, ctx.spec.to_dict())
                        saved = True
                    elif etype == "clarify":
                        reply_kind = "clarify"
                        await session_state.bump_clarify(session_id)
                        if ctx.spec is not None:      # 澄清轮保留槽位，下一轮继续继承
                            await session_state.set_spec(session_id, ctx.spec.to_dict())
                        if event.get("kind") == "caliber":
                            # 保存待选口径与原问题，下一轮用户回复 A/B 时恢复原问题
                            await session_state.set_pending_caliber(session_id, {
                                "question": event.get("original_question") or ctx.raw_question,
                                "options": event.get("options") or [],
                            })
                        answer = event.get("question") or answer
                    elif etype in ("reject", "escalate", "error"):
                        reply_kind = etype
                        await session_state.clear_clarify(session_id)
                        await session_state.clear_pending_caliber(session_id)
                        answer = event.get("question") or event.get("message") or answer
                    # 终态载荷快照（0019）：result 分支在上面已补 query_id/trace_code；
                    # steps/elapsed_ms（F54）让跨设备恢复时步骤时间轴与耗时也能还原
                    if etype in ("result", "clarify", "reject", "escalate", "error"):
                        terminal_payload = dict(event)
                        terminal_payload["steps"] = list(step_trace)
                        terminal_payload["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
                    yield sse(event)
            finally:
                if not saved:                              # 澄清/拒答/异常也留痕
                    await persist.save_query(ctx, answer, int((time.perf_counter() - started) * 1000))
                    tr.finish(output={"status": "unfinished", "answer": answer}, level="WARNING")
                await session_state.add_assistant_message(
                    session_id, json.dumps(ctx.result or {}, ensure_ascii=False, default=str))
                # 助手消息落库（含闲聊/技能/澄清/拒答；内容为空则跳过）；终态载荷随消息持久化，
                # 跨设备恢复消息流时能还原结果表格/KPI/澄清卡片，而不是纯文本
                persist.spawn(persist.save_message(session_id, user.username, "assistant",
                                                   answer, kind=reply_kind, trace_id=trace_id,
                                                   payload=terminal_payload))
                # A11 自动学习（旁路异步，失败不影响主链路）：
                # 成功取数 → 判断是否沉淀技能规范；每轮对话 → 判断是否值得长期记住
                if ctx.result and ctx.generated_sql:
                    caliber = ctx.recalled_calibers[0]["metric_name"] if ctx.recalled_calibers else ""
                    dims = list(getattr(ctx.spec, "dimensions", []) or [])
                    persist.spawn(auto_learn.observe_query(
                        username=user.username, question=ctx.raw_question or ctx.question,
                        sql=ctx.generated_sql, caliber=caliber, dimensions=dims))
                persist.spawn(auto_learn.observe_conversation(
                    username=user.username, question=req.message, answer=answer,
                    mode=reply_kind))

    return StreamingResponse(stream(), media_type="text/event-stream")


async def preset_events(explanation: str, answer: str):
    """固定回复的事件流：与取数链路保持同样的 START/节点/result/DONE 形状。"""
    yield {"type": "node", "node": "INTENT", "detail": "preset"}
    yield {"type": "node", "node": "PRESENT"}
    yield {"type": "result", "sql": None, "columns": [], "rows": [], "row_count": 0,
           "truncated": False, "explanation": explanation,
           "assumptions": [], "summary": answer}


def image_fallback_text(skipped: list[dict] | None) -> str:
    """图片收到了但读不了：给出确切原因和可执行的替代方案。

    这段话必须由服务端写，不能让模型生成：模型看不到附件状态，只会编一句
    「当前只接收文本和数仓表名」——既与事实不符（文件确实收到了），也没有任何下一步。
    """
    reasons = " ".join(str(s.get("reason") or "") for s in (skipped or []))
    if "未配置" in reasons:
        why, hint = "当前没有配置视觉模型，我读不到图片里的内容", "请联系管理员在模型设置中配置视觉模型后重试"
    elif "不支持" in reasons:
        why, hint = "当前模型不支持读取图片", "换用支持视觉的模型后重试"
    elif "大小上限" in reasons:
        why, hint = "图片超过了单张大小上限", "压缩或裁剪后再上传"
    elif "不是有效图片" in reasons:
        why, hint = "文件内容不是有效图片（可能扩展名被改过或文件已损坏）", "确认文件能正常打开后重新上传"
    elif "调用失败" in reasons:
        why, hint = "视觉模型调用失败", "稍后重试"
    else:
        why, hint = "暂时无法读取图片内容", "稍后重试"
    return (f"{why}。{hint}。\n"
            f"如果图里是表格或数据，直接导出为 CSV / Excel 上传，我可以直接查询并出数。")


async def general_events(question: str, history: list[dict] | None = None,
                         image_notes: str = "",
                         image_skipped: list[dict] | None = None):
    """寒暄/能力说明/图片说明的事件流：由 AI 模型回答，失败回退内置文案。"""
    yield {"type": "node", "node": "INTENT", "detail": "general"}
    if image_skipped and not image_notes:
        # 全部图片都没解析出来：直接给确定性答复，不花一次模型调用去复述同一件事
        answer, sources = image_fallback_text(image_skipped), []
    else:
        answer, sources = await answer_general(question, history, image_notes=image_notes)
    yield {"type": "node", "node": "PRESENT"}
    # image_notes 一并回传：识别结果会直接影响回答，用户必须能看到「识别到了什么」，
    # 否则识别错了也只能看到一段错答案，无从判断。
    yield {"type": "result", "sql": None, "columns": [], "rows": [], "row_count": 0,
           "truncated": False, "explanation": "由接入的 AI 模型回答",
           "assumptions": [], "summary": answer, "sources": sources,
           "image_notes": image_notes or None}


@router.get("/sessions")
async def list_sessions(user: AuthUser = Depends(get_current_user)):
    return await session_state.list_sessions(user.username, user.role == "admin")


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, user: AuthUser = Depends(get_current_user)):
    owner = await session_state.owner(session_id)
    if owner and owner != user.username and user.role != "admin":
        raise HTTPException(status_code=403, detail="无权删除该会话")
    await session_state.delete(session_id)
    return {"deleted": session_id}


@router.get("/sessions/{session_id}/messages")
async def session_messages(session_id: str, limit: int = 200,
                           user: AuthUser = Depends(get_current_user)):
    """会话历史消息（升序）：跨设备/清缓存后恢复消息流，附件引用可点击预览。"""
    if not await _can_read_session(session_id, user):
        raise HTTPException(status_code=403, detail="无权读取该会话")
    rows = await persist.list_messages(session_id, limit)
    return {"session_id": session_id, "messages": rows}


@router.get("/sessions/archive")
async def archive_sessions(limit: int = 100, user: AuthUser = Depends(get_current_user)):
    """服务端会话归档：本人参与过的会话列表（跨设备可见，localStorage 是单设备的）。"""
    return {"sessions": await persist.list_sessions_for_user(user.username, limit)}


@router.post("/queries/{query_id}/feedback")
async def feedback(query_id: str, rating: str, reason: str | None = None,
                   user: AuthUser = Depends(get_current_user)):
    # B08：写 query_record.user_feedback；👍 后续可进历史问答候选池（口径库模板 §3）
    if rating not in ("up", "down"):
        raise HTTPException(status_code=400, detail="rating 仅支持 up/down")
    # 归属校验：普通用户只能给自己的记录打分；"latest" 也限定到本人最新一条
    snapshot = await persist.query_snapshot(query_id if query_id != "latest" else "latest")
    if snapshot and user.role != "admin" and snapshot.get("username") != user.username:
        raise HTTPException(status_code=403, detail="无权评价该查询记录")
    recorded = await persist.set_feedback(query_id, rating, reason)
    if recorded:      # 反馈写进 Langfuse，用于按分数筛选表现差的 trace
        tracing.score(await persist.langfuse_trace_id(query_id), "user_feedback",
                      1.0 if rating == "up" else 0.0, reason)
    if rating == "up":
        # D04：👍 自动进入候选池，harvest 时并入草稿待人工复核（失败不影响反馈结果）
        snapshot = await persist.query_snapshot(query_id)
        if snapshot:
            feedback_pool.capture(snapshot)
    return {"query_id": query_id, "rating": rating, "reason": reason, "recorded": recorded}
