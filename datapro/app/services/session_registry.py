"""会话活跃登记（S07）：记录最后活跃时间，供定期回收会话 DB 账号与临时表。

多实例加固：每次读按 mtime 指纹失效缓存，touch/remove 走跨进程文件锁内的
read-modify-write，避免 worker 持有旧快照误删仍在活跃的会话。
"""
from datetime import datetime, timedelta
from pathlib import Path

from app.services import file_store as fs
from app.services.shared_json import SharedJson


class SessionRegistry:
    def __init__(self, path: Path | None = None):
        self._path = path or (fs.ROOT / "sessions.json")
        self._store = SharedJson(self._path, default={})

    def touch(self, session_id: str) -> None:
        if not session_id:
            return
        now = datetime.now().isoformat(timespec="seconds")
        with self._store.edit() as data:
            data[session_id] = now

    def expired(self, ttl_hours: int) -> list[str]:
        cutoff = datetime.now() - timedelta(hours=ttl_hours)
        data = self._store.snapshot()
        expired = []
        for sid, ts in data.items():
            try:
                if datetime.fromisoformat(ts) < cutoff:
                    expired.append(sid)
            except (ValueError, TypeError):
                expired.append(sid)  # 损坏时间戳保守按过期处理，但 DROP 前有二次校验
        return expired

    def remove(self, session_id: str) -> None:
        with self._store.edit() as data:
            data.pop(session_id, None)

    def all(self) -> dict[str, str]:
        return dict(self._store.snapshot())

    def recheck_active(self, session_id: str, ttl_hours: int) -> bool:
        """DROP 前二次校验：重新读文件确认会话确实已过期。"""
        ts = self._store.snapshot().get(session_id)
        if not ts:
            return False
        try:
            return datetime.fromisoformat(ts) >= datetime.now() - timedelta(hours=ttl_hours)
        except (ValueError, TypeError):
            return True  # 时间戳异常时保守不删


session_registry = SessionRegistry()
