"""DataPro API 入口。"""
import logging

from fastapi import FastAPI

from app.api import routes_admin, routes_chat, routes_files
from app.config import settings
from app.core.trace import current_trace_id, new_trace_id

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

app = FastAPI(title="DataPro", version="0.1.0", description="企业数据管家 Agent")


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


app.include_router(routes_chat.router)
app.include_router(routes_admin.router)
app.include_router(routes_files.router)
