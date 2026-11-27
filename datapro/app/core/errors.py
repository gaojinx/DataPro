"""统一错误码（接口规范 §5）。

所有对外错误响应统一为三层结构，前端据此做「错误码 → 可读文案」映射：
  {
    "code": "E006",                       # 业务错误码；详情见下方 CODE_CATALOG
    "message": "数据源暂时不可用...",      # 可直接展示给用户的人话
    "detail": "原始异常摘要（仅日志/排障）", # 可选，技术细节
    "trace_id": "..."                     # 关联日志的追踪号
  }
HTTPException 的 detail 仍保留原有字符串（兼容既有前端/测试），同时补 code/message。
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorCode:
    code: str
    http_status: int
    default_message: str


E001 = ErrorCode("E001", 400, "问题信息不足，请补充时间范围、指标口径或输出粒度")
E002 = ErrorCode("E002", 404, "未找到相关数据表或指标口径，请联系数据团队")
E003 = ErrorCode("E003", 403, "该请求超出 DataPro 的服务范围或您的数据权限")
E004 = ErrorCode("E004", 422, "查询语句校验未通过，请调整问法")
E005 = ErrorCode("E005", 408, "模型服务超时，请稍后重试")
E006 = ErrorCode("E006", 503, "数据源暂时不可用，请联系管理员")
E007 = ErrorCode("E007", 429, "请求过于频繁，请稍后重试")
E008 = ErrorCode("E008", 401, "登录已失效，请重新登录")
E009 = ErrorCode("E009", 422, "查询扫描量过大，请缩小时间范围或增加筛选条件")
E010 = ErrorCode("E010", 500, "系统内部异常，已转人工处理")

# 错误码目录：供前端映射与文档对齐（E008 补在认证场景，E010 兜底 500）
CODE_CATALOG: dict[str, ErrorCode] = {
    e.code: e for e in (E001, E002, E003, E004, E005, E006, E007, E008, E009, E010)
}

# HTTP 状态码 → 业务错误码：非业务异常（鉴权/404/校验/5xx）也给出稳定映射
HTTP_STATUS_CODE: dict[int, str] = {
    400: "E004", 401: "E008", 403: "E003", 404: "E002",
    405: "E004", 408: "E005", 409: "E004", 422: "E004",
    429: "E007", 500: "E010", 502: "E006", 503: "E006", 504: "E005",
}

# HTTP 状态码 → 默认人话（当后端没有更具体 detail 时展示）
HTTP_STATUS_MESSAGE: dict[int, str] = {
    400: "请求参数有误，请检查后重试",
    401: "登录已失效，请重新登录",
    403: "没有权限执行该操作，或请求超出服务范围",
    404: "请求的资源不存在或已被移除",
    405: "不支持该操作方式",
    408: "请求超时，请稍后重试",
    409: "操作冲突，请刷新后重试",
    422: "提交内容未通过校验，请检查后重试",
    429: "操作过于频繁，请稍后重试",
    500: "服务开小差了，请稍后重试；若持续出现请联系管理员",
    502: "上游服务暂时不可用，请稍后重试",
    503: "服务暂时不可用，请稍后重试",
    504: "服务响应超时，请稍后重试",
}


def code_for_status(status: int) -> str:
    """HTTP 状态码映射到业务错误码（未知状态统一 E010）。"""
    return HTTP_STATUS_CODE.get(status, "E010")


def status_message(status: int) -> str:
    """HTTP 状态码的人话（用于路由级 404/405/422 等非业务异常）。"""
    return HTTP_STATUS_MESSAGE.get(status, HTTP_STATUS_MESSAGE[500])


def message_for(status: int, code: str = "") -> str:
    """优先返回错误码默认人话，其次状态码人话，最后兜底。"""
    err = CODE_CATALOG.get(code or code_for_status(status))
    if err:
        return err.default_message
    return status_message(status)


def error_body(*, status: int, code: str = "", message: str = "",
               detail: str = "", trace_id: str = "") -> dict:
    """构造统一错误响应体；detail 保留字符串以兼容旧前端。"""
    code = code or code_for_status(status)
    return {
        "code": code,
        "message": message or message_for(status, code),
        "detail": detail or message or message_for(status, code),
        "trace_id": trace_id,
    }


class DataProError(Exception):
    def __init__(self, err: ErrorCode, message: str | None = None):
        self.err = err
        self.message = message or err.default_message
        super().__init__(f"[{err.code}] {self.message}")
