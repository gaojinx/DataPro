"""轻量文件编辑锁（进程内，单实例部署；多实例需换 Redis，见 B11）。"""
import threading
import time

TTL_SECONDS = 300

_lock = threading.Lock()
_holds: dict[str, tuple[str, float]] = {}


def acquire(file_id: str, owner: str) -> tuple[bool, str]:
    now = time.time()
    with _lock:
        holder, expires = _holds.get(file_id, ("", 0.0))
        if holder and holder != owner and expires > now:
            return False, "文件正在被其他会话编辑，请稍后再试"
        _holds[file_id] = (owner, now + TTL_SECONDS)
        return True, ""


def release(file_id: str, owner: str) -> None:
    with _lock:
        holder, _ = _holds.get(file_id, ("", 0.0))
        if holder == owner:
            _holds.pop(file_id, None)
