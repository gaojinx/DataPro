"""终态事件的错误码：outcome 有值时必须带 error_code，否则按 E00x 统计会整段消失。"""
import asyncio

from app.api.routes_chat import fallback_reason_code
from app.core.statemachine import Pipeline, PipelineContext


def test_fallback_reason_code_covers_terminal_events():
    assert fallback_reason_code({"type": "escalate", "reason": "SCAN_TOO_LARGE"}) == "E009"
    assert fallback_reason_code({"type": "escalate", "reason": "NO_SQL"}) == "E010"
    assert fallback_reason_code({"type": "escalate"}) == "E010"
    assert fallback_reason_code({"type": "reject", "reason": "USER_DENIED"}) == "E010"
    # 非终态不产生错误码：澄清/出数不是错误
    assert fallback_reason_code({"type": "clarify"}) == ""
    assert fallback_reason_code({"type": "result"}) == ""


class _NoSqlGenerator:
    async def generate(self, ctx):        # 模拟模型连续给不出 SQL
        return None


def test_no_sql_escalation_carries_reason_code_and_message():
    pipe = Pipeline(None, None, _NoSqlGenerator(), None, None, None)
    ctx = PipelineContext(question="查一下", user_id="u")

    async def collect():
        return [ev async for ev in pipe._run_query(ctx)]

    events = asyncio.run(collect())
    last = events[-1]
    assert last["type"] == "escalate"
    # 空事件等于「用户看到一条没有任何说明的转人工」，所以码与文案都必须有
    assert last["reason_code"] == "E010"
    assert last["message"]
    assert last["reason"] == "NO_SQL"
