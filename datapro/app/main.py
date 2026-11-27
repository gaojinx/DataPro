"""DataPro API 入口。"""
import asyncio
import json
import logging
from contextlib import asynccontextmanager

import traceback

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import (routes_access, routes_admin, routes_auth, routes_chat, routes_exports,
                     routes_files, routes_inspections, routes_knowledge, routes_learning,
                     routes_schedules, routes_skills, routes_trace, routes_usage)
from app.config import settings
from app.core.errors import (error_body, code_for_status, message_for, status_message,
                             CODE_CATALOG, DataProError)
from app.core.trace import current_trace_id, new_trace_id
from app.db.session import init_db
from app.services.auth import seed_users

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        if settings.init_db_on_startup:
            init_db()   # B08/S03：初始化 query_record / audit_log 等业务表
        seed_users()  # B12：初始化演示登录账号
    except Exception as exc:  # noqa: BLE001 —— 业务库不可用时降级为仅日志，不阻断服务
        logging.getLogger("datapro").warning("业务库初始化失败，查询记录/审计将跳过：%s", exc)
    # U-B01：启动期探测一次真实最新分区，避免首个请求落到空分区；
    # 探测失败只告警，锚点回退日历 T-1，不阻断启动。
    try:
        from app.services.partition_clock import partition_clock

        await asyncio.to_thread(partition_clock.refresh)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("datapro").warning("分区时钟初始化失败：%s", exc)
    from app.services import tracing
    from app.services.schedule_runner import start_scheduler
    scheduler = start_scheduler()

    # 向量召回预热（A05）：后台跑，不阻塞启动。不预热的话，编码上百份元数据文档的开销
    # 会落在第一个真实提问上，而召回超时只有 3 秒——现象是「刚部署时召回特别差」。
    warm_task: asyncio.Task | None = None
    if settings.vector_recall_enabled:
        async def _warm() -> None:
            try:
                from app.services.vector_recall import vector_recall

                stats = await vector_recall.prewarm()
                logging.getLogger("datapro").info("向量召回预热：%s", stats)
            except Exception as exc:  # noqa: BLE001 —— 预热失败不影响主链路
                logging.getLogger("datapro").warning("向量召回预热失败，首次召回按需编码：%s", exc)

        warm_task = asyncio.create_task(_warm())

    yield
    if warm_task and not warm_task.done():
        warm_task.cancel()
    scheduler.shutdown(wait=False)
    tracing.flush()


app = FastAPI(title="DataPro", version="0.1.0", description="企业数据管家 Agent", lifespan=lifespan)


# ---- 统一错误映射：任何异常都返回 code/message/detail/trace_id，前端据此展示人话而非裸状态码 ----
def _error_response(status: int, code: str = "", message: str = "",
                    detail: str = "") -> JSONResponse:
    body = error_body(status=status, code=code, message=message,
                      detail=detail, trace_id=current_trace_id())
    return JSONResponse(status_code=status, content=body,
                        headers={"X-Trace-Id": current_trace_id()})


@app.exception_handler(DataProError)
async def handle_datapro_error(_: Request, exc: DataProError):
    return _error_response(exc.err.http_status, exc.err.code, exc.message)


@app.exception_handler(RequestValidationError)
async def handle_validation_error(_: Request, exc: RequestValidationError):
    # 参数校验失败：把字段名与原因拼成可读信息，避免只回一个 422
    issues = []
    for err in exc.errors()[:3]:
        loc = '.'.join(str(x) for x in err.get('loc', ()) if x != 'body')
        issues.append(f"{loc}: {err.get('msg', '格式不正确')}" if loc else err.get('msg', '格式不正确'))
    detail = "；".join(issues)
    return _error_response(422, message=f"提交内容未通过校验：{detail}", detail=detail)


@app.exception_handler(StarletteHTTPException)
async def handle_http_exception(_: Request, exc: StarletteHTTPException):
    status = exc.status_code
    code = code_for_status(status)
    raw = exc.detail if isinstance(exc.detail, str) else ""
    # 路由未匹配等场景 detail 是英文（Not Found）；此时用映射后的人话，同时 detail 保留原文。
    mapped = status_message(status)
    human = raw if raw and not raw.startswith(("Not Found", "Internal Server", "Method Not")) else mapped
    return _error_response(status, code=code, message=human, detail=raw or human)


@app.exception_handler(Exception)
async def handle_unexpected_error(request: Request, exc: Exception):
    # 兜底 500：日志留完整堆栈，响应只给映射文案 + trace_id，不泄露内部实现
    logging.getLogger("datapro").error(
        "未处理异常 %s %s: %s\n%s", request.method, request.url.path, exc,
        traceback.format_exc())
    code = CODE_CATALOG["E010"].code
    return _error_response(500, code=code,
                           message=message_for(500, code),
                           detail=f"{type(exc).__name__}: {exc}"[:300])

# 跨域：部署到公网的静态页（如 DataPro 产品页体验区）需要直接调用后端接口
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,                       # 认证走 Bearer Token，不依赖 Cookie
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
    expose_headers=["X-Trace-Id"],
    allow_private_network=True,                    # 公网 HTTPS 页面访问本机 127.0.0.1 后端
)


@app.middleware("http")
async def trace_middleware(request, call_next):
    new_trace_id()
    response = await call_next(request)
    response.headers["X-Trace-Id"] = current_trace_id()
    return response


@app.middleware("http")
async def admin_audit_middleware(request, call_next):
    """管理端写操作统一审计（B12）：只记操作摘要，不记密码/API Key 等敏感字段。"""
    if not request.url.path.startswith("/admin") or request.method in ("GET", "HEAD", "OPTIONS"):
        return await call_next(request)

    from app.core.trace import current_trace_id
    from app.services import persist

    actor = ""
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        from app.services.auth import decode_token
        payload = decode_token(authorization.split(" ", 1)[1].strip())
        actor = (payload or {}).get("sub", "")

    body_summary = ""
    if request.method in ("POST", "PUT", "PATCH"):
        try:
            raw = await request.body()
            if raw:
                payload = json.loads(raw)
                # 白名单式记录：只保留非敏感的键名与结构摘要，绝不写值中的秘钥
                body_summary = json.dumps(
                    {k: _summarize_value(v) for k, v in payload.items()
                     if k.lower() not in ("password", "api_key", "secret", "token")},
                    ensure_ascii=False)[:1000]
        except Exception:  # noqa: BLE001 —— 审计不能阻断请求
            body_summary = ""

    response = await call_next(request)
    persist.spawn(persist.save_audit({
        "trace_id": current_trace_id(),
        "user_id": actor,
        "node": "ADMIN",
        "question_digest": f"{request.method} {request.url.path}"[:255],
        "sql_text": None,
        "payload": json.dumps({"status_code": response.status_code,
                               "body_keys": body_summary}, ensure_ascii=False),
    }))
    return response


def _summarize_value(value):
    """审计值脱敏：列表/字典只保留类型与长度，标量只保留类型。"""
    if isinstance(value, (list, dict)):
        return {"type": type(value).__name__, "len": len(value)}
    return {"type": type(value).__name__}


@app.get("/health")
async def health():
    return {"status": "ok", "env": settings.app_env,
            "data_pro_enabled": settings.feature_data_pro_enabled}


@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics():
    """I03：Prometheus 抓取端点（纯文本，进程内计数器）。"""
    if not settings.metrics_enabled:
        raise HTTPException(status_code=404, detail="metrics disabled")
    from fastapi.responses import PlainTextResponse

    from app.services.metrics import metrics
    return PlainTextResponse(metrics.render(), media_type="text/plain; version=0.0.4")


app.include_router(routes_auth.router)
app.include_router(routes_chat.router)
app.include_router(routes_admin.router)
app.include_router(routes_files.router)
app.include_router(routes_skills.router)
app.include_router(routes_schedules.router)
app.include_router(routes_knowledge.router)
app.include_router(routes_trace.router)
app.include_router(routes_inspections.router)
app.include_router(routes_exports.router)
app.include_router(routes_access.router)
app.include_router(routes_learning.router)
app.include_router(routes_usage.router)
