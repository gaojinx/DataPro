"""DataPro API 入口。"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import routes_admin, routes_auth, routes_chat, routes_files
from app.config import settings
from app.core.trace import current_trace_id, new_trace_id
from app.db.session import init_db
from app.services.auth import seed_users

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        init_db()   # B08/S03：初始化 query_record / audit_log 等业务表
        seed_users()  # B12：初始化演示登录账号
    except Exception as exc:  # noqa: BLE001 —— 业务库不可用时降级为仅日志，不阻断服务
        logging.getLogger("datapro").warning("业务库初始化失败，查询记录/审计将跳过：%s", exc)
    yield


app = FastAPI(title="DataPro", version="0.1.0", description="企业数据管家 Agent", lifespan=lifespan)


@app.middleware("http")
async def trace_middleware(request, call_next):
    new_trace_id()
    response = await call_next(request)
    response.headers["X-Trace-Id"] = current_trace_id()
    return response


@app.get("/health")
async def health():
    return {"status": "ok", "env": settings.app_env,
            "data_pro_enabled": settings.feature_data_pro_enabled}


app.include_router(routes_auth.router)
app.include_router(routes_chat.router)
app.include_router(routes_admin.router)
app.include_router(routes_files.router)
