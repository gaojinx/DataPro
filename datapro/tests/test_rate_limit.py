"""限流单测（B14/E007）：固定窗口计数、超限返回等待秒数、0 表示关闭。"""
from app.services.rate_limit import RateLimiter

DEAD_URL = "redis://127.0.0.1:16379/15"      # 无人监听，验证内存降级


async def test_memory_fallback_limit(monkeypatch):
    # 固定时钟：避免三次调用恰好跨分钟边界导致窗口重置（flaky）
    monkeypatch.setattr("app.services.rate_limit.time.time", lambda: 1_000_000.0)
    limiter = RateLimiter(url=DEAD_URL, limit=2)
    assert await limiter.hit("chat:alice") is None
    assert await limiter.hit("chat:alice") is None
    wait = await limiter.hit("chat:alice")
    assert wait is not None and 1 <= wait <= 60
    # 不同用户互不影响
    assert await limiter.hit("chat:bob") is None


async def test_zero_limit_disabled():
    limiter = RateLimiter(url=DEAD_URL, limit=0)
    for _ in range(5):
        assert await limiter.hit("chat:alice") is None
