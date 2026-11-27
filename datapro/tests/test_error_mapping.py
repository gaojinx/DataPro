"""统一错误映射回归测试：任何对外错误都带 code/message/detail/trace_id，
前端据此显示人话而非裸状态码（500/404/401 等）。"""
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from fastapi.exceptions import RequestValidationError

from app.core.errors import (error_body, code_for_status, status_message,
                             message_for, CODE_CATALOG, DataProError, E006)
from app.main import (handle_http_exception, handle_unexpected_error,
                      handle_validation_error, handle_datapro_error, app)


def test_code_catalog_and_status_mapping():
    assert code_for_status(404) == "E002"
    assert code_for_status(401) == "E008"
    assert code_for_status(500) == "E010"
    assert code_for_status(599) == "E010"          # 未知状态兜底
    assert "不存在" in status_message(404)          # 人话而非 Not Found
    assert CODE_CATALOG["E006"].default_message == message_for(503, "E006")


def test_error_body_shape():
    body = error_body(status=503, code="E006", message="数据源暂时不可用",
                      detail="pymysql.err", trace_id="tr-123")
    assert set(body) == {"code", "message", "detail", "trace_id"}
    assert body["code"] == "E006"
    assert body["trace_id"] == "tr-123"
    assert body["detail"] == "pymysql.err"          # 保留技术细节，不进 message


@pytest.mark.parametrize("status,code,keyword", [
    (404, "E002", "不存在"),
    (401, "E008", "登录"),
    (403, "E003", "权限"),
    (500, "E010", "重试"),
    (503, "E006", "不可用"),
])
def test_http_exception_handler_maps_code_and_human_message(status, code, keyword):
    # detail 是标准英文串（无展示价值）时，message 用映射后的人话
    probe = FastAPI()
    probe.add_exception_handler(HTTPException, handle_http_exception)

    @probe.get("/boom")
    def boom():
        raise HTTPException(status_code=status, detail="Not Found")

    body = TestClient(probe, raise_server_exceptions=False).get("/boom").json()
    assert body["code"] == code, body
    assert keyword in body["message"], body
    assert "trace_id" in body


def test_http_exception_keeps_meaningful_chinese_detail():
    # 后端给了具体中文 detail（如"仅管理员可删除"）时应保留原文，不套映射模板
    probe = FastAPI()
    probe.add_exception_handler(HTTPException, handle_http_exception)

    @probe.get("/x")
    def x():
        raise HTTPException(status_code=403, detail="仅管理员可删除该技能")

    body = TestClient(probe, raise_server_exceptions=False).get("/x").json()
    assert body["code"] == "E003"
    assert body["message"] == "仅管理员可删除该技能"


def test_unexpected_exception_maps_to_e010(monkeypatch):
    probe = FastAPI()
    probe.add_exception_handler(Exception, handle_unexpected_error)

    @probe.get("/crash")
    def crash():
        raise RuntimeError("内部连接串泄露 password=xxx")

    r = TestClient(probe, raise_server_exceptions=False).get("/crash")
    body = r.json()
    assert r.status_code == 500
    assert body["code"] == "E010"
    assert "异常" in body["message"] or "重试" in body["message"]
    # 内部细节可进 detail 供排障，但不能出现在给人看的 message
    assert "password" not in body["message"]
    assert "trace_id" in body          # 真实请求经 trace_middleware 注入，probe app 可为空串


def test_datapro_error_handler():
    probe = FastAPI()
    probe.add_exception_handler(DataProError, handle_datapro_error)

    @probe.get("/src")
    def src():
        raise DataProError(E006, "数据源 A 连接失败")

    r = TestClient(probe, raise_server_exceptions=False).get("/src")
    body = r.json()
    assert r.status_code == 503
    assert body["code"] == "E006"
    assert body["message"] == "数据源 A 连接失败"


def test_validation_error_handler_is_readable():
    from pydantic import BaseModel

    class Body(BaseModel):
        count: int

    probe = FastAPI()
    probe.add_exception_handler(RequestValidationError, handle_validation_error)

    @probe.post("/v")
    def v(body: Body):
        return body

    body = TestClient(probe, raise_server_exceptions=False).post("/v", json={"count": "x"}).json()
    assert body["code"] == "E004"
    assert "校验" in body["message"]
    assert "count" in body["detail"]


def test_real_app_404_uses_mapping():
    body = TestClient(app, raise_server_exceptions=False).get("/api/v1/__missing__").json()
    assert body["code"] == "E002"
    assert "不存在" in body["message"]
