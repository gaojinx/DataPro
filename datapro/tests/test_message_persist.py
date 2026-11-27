"""全量消息持久化（message 表）：空内容跳过，不触发写入。"""
from app.services import persist


async def test_save_message_skips_blank_content():
    assert await persist.save_message("s1", "tester", "user", "   ") is None
    assert await persist.save_message("s1", "tester", "assistant", "") is None
