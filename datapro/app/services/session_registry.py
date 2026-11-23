"""会话活跃登记（S07）：记录最后活跃时间，供定期回收会话 DB 账号与临时表。

说明：注册表为进程内存 + JSON 持久化；多副本部署时需改为 Redis/数据库共享状态。
"""
import json
import threading
from datetime import datetime, timedelta
from pathlib import Path

from app.services import file_store as fs


class SessionRegistry:
    def __init__(self, path: Path | None = None):
        self._lock = threading.Lock()
        self._path = path or (fs.ROOT / "sessions.json")
        self._data: dict[str, str] = {}
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self._data = {}

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")

    def touch(self, session_id: str) -> None:
        if not session_id:
            return
        with self._lock:
            self._data[session_id] = datetime.now().isoformat(timespec="seconds")
            self._persist()

    def expired(self, ttl_hours: int) -> list[str]:
        cutoff = datetime.now() - timedelta(hours=ttl_hours)
        with self._lock:
            return [sid for sid, ts in self._data.items()
                    if datetime.fromisoformat(ts) < cutoff]

    def remove(self, session_id: str) -> None:
        with self._lock:
            if self._data.pop(session_id, None) is not None:
                self._persist()

    def all(self) -> dict[str, str]:
        with self._lock:
            return dict(self._data)


session_registry = SessionRegistry()
