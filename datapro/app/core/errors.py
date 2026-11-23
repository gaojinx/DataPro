"""统一错误码（接口规范 §5）。"""
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
E010 = ErrorCode("E010", 500, "系统内部异常，已转人工处理")


class DataProError(Exception):
    def __init__(self, err: ErrorCode, message: str | None = None):
        self.err = err
        self.message = message or err.default_message
        super().__init__(f"[{err.code}] {self.message}")
