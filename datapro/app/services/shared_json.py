"""跨进程共享 JSON 状态原语（多实例加固）。

解决四类问题：
1. 原子写：临时文件 + os.replace，读者不会看到半截 JSON
2. 跨进程互斥：fcntl(Unix) / msvcrt(Windows) 文件锁，保护 read-modify-write
3. mtime 缓存失效：每个进程按 (mtime_ns, size) 判断外部写入，自动重载
4. 配置入库：`CONFIG_STORE=db` 时同一份文档落到业务库 `config_doc`，
   多实例读写走数据库行锁，避免「各自持有文件、互相覆盖」

降级约定：
- 库不可用时熔断 30 秒再试，期间回退本地文件（不只读，写入也落文件）
- 无锁平台/锁获取失败时退化为进程内 threading.RLock（单实例仍正确，多实例记告警）
- 损坏文件：优先读 .bak，仍失败则用调用方 default，并把损坏文件改名保留现场
- 首次切到 db 时，若库里没有该文档，自动用现有文件内容种子化，升级不丢配置
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from app.config import settings

logger = logging.getLogger("datapro.shared_json")


class _DbUnavailable(RuntimeError):
    """库连接/加锁失败：调用方可以安全降级到文件；业务异常不会走这里。"""

DATA_DIR = Path(__file__).resolve().parents[2] / "data"

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()

# 库不可用后的重试时间点（模块级：不同实例共享同一熔断窗口）
_DB_RETRY_AT = 0.0
_DB_RETRY_SECONDS = 30.0
# 文档读缓存：配置读多写少，1 秒跨实例延迟可接受，避免每次请求都打库
_DOC_CACHE: dict[str, tuple[float, Any]] = {}
_DOC_CACHE_LOCK = threading.Lock()
_MISSING = object()


def _thread_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = _LOCKS[key] = threading.RLock()
        return lock


@contextmanager
def file_lock(path: Path, *, timeout: float = 5.0) -> Iterator[None]:
    """跨进程独占锁；同一进程内的线程也要互斥，故叠加 threading.RLock。"""
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _thread_lock(path):
        with lock_path.open("a+b") as handle:
            acquired = False
            if os.name == "nt":  # pragma: no cover - Windows 分支在本机验证
                try:
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                    acquired = True
                except OSError as exc:
                    logger.warning("文件锁获取失败（%s），退化为进程内锁", exc)
            else:
                try:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                    acquired = True
                except OSError as exc:
                    logger.warning("文件锁获取失败（%s），退化为进程内锁", exc)
            try:
                yield
            finally:
                if acquired:
                    try:
                        if os.name == "nt":
                            import msvcrt

                            handle.seek(0)
                            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl

                            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass


def _fingerprint(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
        return stat.st_mtime_ns, stat.st_size
    except OSError:
        return None


def read_json(path: Path, default: Any) -> Any:
    """读 JSON；损坏时尝试 .bak，仍失败则保留损坏文件并返回 default。"""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("JSON 读取失败 %s：%s", path, exc)
        backup = path.with_suffix(path.suffix + ".bak")
        if backup.exists():
            try:
                data = json.loads(backup.read_text(encoding="utf-8"))
                logger.warning("从备份恢复 %s", path)
                return data
            except (json.JSONDecodeError, OSError):
                pass
        try:
            path.rename(path.with_suffix(path.suffix + ".corrupt"))
        except OSError:
            pass
        return default


def write_json(path: Path, data: Any) -> None:
    """原子写：临时文件同目录 + fsync + os.replace，避免跨设备 rename 失败。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


# ---------------- 库后端（config_doc） ----------------

def _copy(value: Any) -> Any:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, dict):
        return dict(value)
    return value


def _db_enabled() -> bool:
    return (getattr(settings, "config_store", "file") or "file").strip().lower() == "db"


def _db_ready() -> bool:
    return _db_enabled() and time.monotonic() >= _DB_RETRY_AT


def _db_fail(exc: Exception) -> None:
    global _DB_RETRY_AT
    _DB_RETRY_AT = time.monotonic() + _DB_RETRY_SECONDS
    logger.warning("config_doc 不可用，降级文件（%s 秒后重试）：%s", int(_DB_RETRY_SECONDS), exc)


def _doc_key(path: Path) -> str | None:
    """仅 data/ 目录下的文档走库；测试用的临时目录保持纯文件模式。"""
    if not _db_enabled():
        return None
    try:
        return Path(path).resolve().relative_to(DATA_DIR.resolve()).as_posix()
    except (ValueError, OSError):
        return None


def _cache_get(key: str) -> Any:
    ttl = float(getattr(settings, "config_store_cache_ttl", 1.0) or 0)
    with _DOC_CACHE_LOCK:
        item = _DOC_CACHE.get(key)
        if item is None:
            return _MISSING
        expires, data = item
        if ttl <= 0 or time.monotonic() >= expires:
            _DOC_CACHE.pop(key, None)
            return _MISSING
        return data


def _cache_put(key: str, data: Any) -> None:
    ttl = float(getattr(settings, "config_store_cache_ttl", 1.0) or 0)
    with _DOC_CACHE_LOCK:
        _DOC_CACHE[key] = (time.monotonic() + max(ttl, 0.05), data)


def _cache_drop(key: str) -> None:
    with _DOC_CACHE_LOCK:
        _DOC_CACHE.pop(key, None)


def _db_load(key: str) -> str | None:
    from app.db.models import ConfigDoc
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        row = db.get(ConfigDoc, key)
        return None if row is None else row.payload


def _db_seed(key: str, data: Any, updated_by: str = "bootstrap") -> None:
    from app.db.models import ConfigDoc
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        if db.get(ConfigDoc, key) is not None:
            return
        db.add(ConfigDoc(doc_key=key, payload=json.dumps(data, ensure_ascii=False),
                         version=1, updated_by=updated_by))
        try:
            db.commit()
        except Exception:  # noqa: BLE001 —— 并发种子化时另一边已插入，忽略即可
            db.rollback()


class SharedJson:
    """带 mtime 缓存失效的 JSON 状态容器（库优先、文件兜底）。

    用法：
        store = SharedJson(path, default=[])
        snap = store.snapshot()          # 读：自动感知其他进程的写入
        with store.edit() as data:       # 写：跨进程锁内 read-modify-write
            data.append(item)
    """

    def __init__(self, path: Path, default: Any):
        self.path = Path(path)
        self.default = default
        self._cache: Any = None
        self._fp: tuple[int, int] | None = None
        self._cache_lock = threading.RLock()

    def invalidate(self) -> None:
        key = _doc_key(self.path)
        if key:
            _cache_drop(key)
        with self._cache_lock:
            self._cache = None
            self._fp = None

    # ---------- 读 ----------
    def snapshot(self) -> Any:
        """返回最新数据的浅副本；库模式下按短 TTL 缓存，文件模式按指纹失效。"""
        key = _doc_key(self.path)
        if key and _db_ready():
            cached = _cache_get(key)
            if cached is not _MISSING:
                return _copy(cached)
            try:
                raw = _db_load(key)
                if raw is None:
                    # 首次切库：用现有文件内容种子化，保证升级不丢配置
                    data = read_json(self.path, self.default)
                    _db_seed(key, data)
                else:
                    data = json.loads(raw)
                _cache_put(key, data)
                return _copy(data)
            except Exception as exc:  # noqa: BLE001 —— 库不可用时读文件
                _db_fail(exc)

        fp = _fingerprint(self.path)
        with self._cache_lock:
            if self._cache is None or fp != self._fp:
                self._cache = read_json(self.path, self.default)
                self._fp = fp
            data = self._cache
        return _copy(data)

    # ---------- 写 ----------
    @contextmanager
    def edit(self, *, default: Any = None, updated_by: str = "") -> Iterator[Any]:
        """跨进程互斥的 read-modify-write；退出时落库（或原子落盘）并刷新缓存。

        只有「连接/加锁失败」才降级文件；业务体抛错或提交失败一律向上抛，
        避免「调用方以为写成功、实际回退到文件又没带上本次修改」。
        """
        base = self.default if default is None else default
        key = _doc_key(self.path)
        if key and _db_ready():
            cm = self._edit_db(key, base, updated_by)
            try:
                data = cm.__enter__()
            except _DbUnavailable as exc:
                _db_fail(exc)
            else:
                try:
                    yield data
                except BaseException:
                    cm.__exit__(*sys.exc_info())
                    raise
                else:
                    cm.__exit__(None, None, None)
                return

        with file_lock(self.path):
            data = read_json(self.path, base)
            yield data
            write_json(self.path, data)
            with self._cache_lock:
                self._cache = data
                self._fp = _fingerprint(self.path)

    @contextmanager
    def _edit_db(self, key: str, base: Any, updated_by: str) -> Iterator[Any]:
        from app.db.models import ConfigDoc
        from app.db.session import SessionLocal

        try:
            db = SessionLocal()
            row = db.get(ConfigDoc, key, with_for_update=True)
            if row is None:
                # 首次写：把现有文件内容带进库，避免覆盖掉历史配置
                legacy = read_json(self.path, base)
                row = ConfigDoc(doc_key=key, version=1, updated_by=updated_by or "bootstrap",
                                payload=json.dumps(legacy, ensure_ascii=False))
                db.add(row)
                try:
                    db.flush()
                except Exception:  # noqa: BLE001 —— 并发首个写入者已插入，改走加锁读
                    db.rollback()
                    row = db.get(ConfigDoc, key, with_for_update=True)
            if row is None:
                raise _DbUnavailable("config_doc 行不可用")
        except _DbUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 —— 连接/加锁失败：交给调用方降级
            raise _DbUnavailable(str(exc)) from exc

        try:
            data = json.loads(row.payload) if row.payload else _copy(base)
            yield data
            row.payload = json.dumps(data, ensure_ascii=False)
            row.version = (row.version or 0) + 1
            if updated_by:
                row.updated_by = updated_by
            db.commit()
            _cache_put(key, data)
        finally:
            db.close()
