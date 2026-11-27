"""数据分区时钟（U-B01）：为「相对时间」提供真实的最新分区锚点。

为什么需要：相对窗口（最近 7 天）原先按日历 `today-1` 计算，而数仓实际可能只到 T-3；
查询会落在没有数据的分区上，返回 0 行被用户读成「业务为 0」。这是静默错误，
比缺数据更危险（章程：宁可说不确定，绝不返回看似正确的错误数据）。

设计约束：
- **不阻塞主链路**：`anchor()` 只读缓存，未探测过则回退日历 T-1；探测由启动期、
  worker 定时任务或显式 `refresh()` 触发，失败只告警不抛错。
- **可回滚**：`PARTITION_READ_ENABLED=false` 时行为与改造前完全一致。
- **显式日期不受影响**：只有相对表达（最近 N 天/本周/本月…）用锚点，
  「昨天」「11月20日」仍按日历解析（否则用户说的日期会被悄悄挪动）。

单独刷新：
    python -m app.services.partition_clock
"""
import logging
import re
import threading
import time
from datetime import date

import sqlalchemy as sa

from app.config import settings
from app.core.time_window import latest_partition

logger = logging.getLogger("datapro.partition_clock")

_SAFE_TABLE = re.compile(r"^[A-Za-z0-9_$.]+$")


def _fixed_anchor() -> date | None:
    """评测/离线场景的固定锚点：保证相对时间用例可复现。"""
    raw = (settings.eval_fixed_anchor or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        logger.warning("EVAL_FIXED_ANCHOR 不是合法日期，已忽略：%r", raw)
        return None


class PartitionClock:
    def __init__(self) -> None:
        self._anchor: date | None = None
        self._fetched_at = 0.0
        self._lock = threading.Lock()
        self._engine: sa.Engine | None = None
        self._warned = False

    # ---------- 读 ----------

    def anchor(self, today: date | None = None) -> date:
        """相对时间窗的锚点（非阻塞）：有探测结果用探测结果，否则日历 T-1。"""
        fixed = _fixed_anchor()
        if fixed is not None:
            return fixed
        if not settings.partition_read_enabled:
            return latest_partition(today)
        with self._lock:
            cached = self._anchor
        return cached or latest_partition(today)

    def confirmed(self) -> date | None:
        """只返回「已探测到」的锚点；从未探测成功时返回 None（不拿日历猜测冒充真实分区）。"""
        fixed = _fixed_anchor()
        if fixed is not None:
            return fixed
        if not settings.partition_read_enabled:
            return None
        with self._lock:
            return self._anchor

    def is_fresh(self) -> bool:
        ttl = max(settings.partition_cache_ttl_seconds, 1)
        with self._lock:
            return self._anchor is not None and (time.time() - self._fetched_at) < ttl

    def status(self) -> dict:
        # 注意：不要在这里调 is_fresh()——它也会加同一把非重入锁，会死锁
        ttl = max(settings.partition_cache_ttl_seconds, 1)
        with self._lock:
            fresh = self._anchor is not None and (time.time() - self._fetched_at) < ttl
            return {
                "enabled": settings.partition_read_enabled,
                "anchor": self._anchor.isoformat() if self._anchor else None,
                "fetched_at": int(self._fetched_at) or None,
                "fresh": fresh,
                "basis": settings.partition_anchor_table,
            }

    # ---------- 写（阻塞，必须放在线程池里调用） ----------

    def _get_engine(self) -> sa.Engine:
        if self._engine is None:
            self._engine = sa.create_engine(
                settings.data_source_url,
                pool_size=2, max_overflow=2, pool_pre_ping=True, pool_recycle=1800,
                connect_args={"connect_timeout": 3},
            )
        return self._engine

    def refresh(self) -> date | None:
        """对基准表执行一次 `MAX(dt)` 探针并更新缓存；失败返回 None 并保留旧值。"""
        fixed = _fixed_anchor()
        if fixed is not None:
            with self._lock:
                self._anchor = fixed
                self._fetched_at = time.time()
            return fixed
        if not settings.partition_read_enabled:
            return None
        table = (settings.partition_anchor_table or "").strip()
        if not table or not _SAFE_TABLE.match(table):
            logger.warning("分区基准表名不合法，跳过探测：%r", table)
            return None
        try:
            with self._get_engine().connect() as conn:
                conn.execute(sa.text(
                    f"SET SESSION max_execution_time = {int(settings.exec_timeout_ms)}"))
                value = conn.execute(sa.text(f"SELECT MAX(dt) FROM {table}")).scalar()
        except Exception as exc:  # noqa: BLE001 —— 探测失败不阻断主链路
            if not self._warned:
                logger.warning("分区探针失败，暂时沿用日历 T-1：%s", exc)
                self._warned = True
            return None

        if value is None:
            return None
        anchor = value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
        with self._lock:
            changed = anchor != self._anchor
            self._anchor = anchor
            self._fetched_at = time.time()
        if changed:
            logger.info("数据分区锚点更新为 %s（基准表 %s）", anchor, table)
        return anchor

    def reset(self) -> None:
        """清空缓存（测试与运维用）。"""
        with self._lock:
            self._anchor = None
            self._fetched_at = 0.0


partition_clock = PartitionClock()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    value = partition_clock.refresh()
    print(f"anchor = {value or '未更新（沿用日历 T-1）'}")
