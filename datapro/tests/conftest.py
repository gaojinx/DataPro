"""测试全局隔离：不让单测写真实业务库配置或读脏缓存。

- `CONFIG_STORE=file`：SharedJson 默认 `CONFIG_STORE=db`；单测里的临时目录路径
  本就不在 `data/` 下、会自动走文件模式，但全局单例（如 skill_registry）会指向
  真实 data/ 目录，跑测试就会改写真实配置，这里统一强制文件模式。
- 密钥引用解析缓存（secret_ref）按进程缓存 60 秒，monkeypatch.setenv 换的值
  会被上一个用例的缓存污染，每个用例前清空。
- 数据新鲜度锚点（partition_clock）是全局单例：任何触发应用启动的用例（如
  TestClient 上下文）都会把**真实数仓最新分区**写进它，后续用例的「最近 7 天」
  就跟着真实日期漂移，断言固定日期的用例会随机失败。每个用例前后清空。
"""
import os

import pytest

os.environ["CONFIG_STORE"] = "file"


@pytest.fixture(autouse=True)
def _isolate_secret_refs():
    from app.services import secret_ref

    secret_ref.invalidate()
    secret_ref._envfile_cache = None      # .env 兜底缓存同样隔离
    yield
    secret_ref.invalidate()
    secret_ref._envfile_cache = None


@pytest.fixture(autouse=True)
def _isolate_partition_clock():
    from app.services.partition_clock import partition_clock

    def reset():
        partition_clock._anchor = None
        partition_clock._fetched_at = 0.0

    reset()
    yield
    reset()
