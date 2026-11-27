"""跨进程 JSON 状态原语单测：原子写、mtime 缓存失效、损坏恢复、跨进程锁。"""
import json
import multiprocessing as mp
import os
import time

from app.services.shared_json import SharedJson, file_lock, read_json, write_json


def _writer(path, key, value):
    """子进程写入：模拟多实例中的另一个进程。"""
    with SharedJson(path, default={}).edit() as data:
        data[key] = value


def test_atomic_write_and_read(tmp_path):
    path = tmp_path / "state.json"
    write_json(path, {"a": 1})
    assert read_json(path, {}) == {"a": 1}
    # 临时文件已清理
    assert not list(tmp_path.glob(".*.tmp"))


def test_mtime_cache_invalidation_across_processes(tmp_path):
    """一个进程写入后，另一个进程的 SharedJson 快照自动感知。"""
    path = tmp_path / "state.json"
    write_json(path, {"v": 1})
    store = SharedJson(path, default={})
    assert store.snapshot()["v"] == 1

    ctx = mp.get_context("spawn") if os.name == "nt" else mp.get_context("fork")
    proc = ctx.Process(target=_writer, args=(path, "v", 2))
    proc.start()
    proc.join(30)
    assert proc.exitcode == 0

    # 文件 mtime_ns 变化 → 缓存失效，读到子进程写入的值
    assert store.snapshot()["v"] == 2


def test_corrupt_file_recovers_from_backup(tmp_path):
    path = tmp_path / "state.json"
    write_json(path, {"a": 1})
    backup = path.with_suffix(".json.bak")
    backup.write_text(json.dumps({"a": 99}), encoding="utf-8")
    path.write_text("{broken", encoding="utf-8")
    assert read_json(path, {}) == {"a": 99}


def test_corrupt_file_without_backup_returns_default(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{broken", encoding="utf-8")
    assert read_json(path, {"fallback": True}) == {"fallback": True}
    assert path.with_suffix(".json.corrupt").exists()


def test_edit_is_atomic(tmp_path):
    """锁内 read-modify-write：两个并发线程各自 +1，最终为 2。"""
    path = tmp_path / "counter.json"
    write_json(path, {"n": 0})
    store = SharedJson(path, default={"n": 0})

    def increment():
        with store.edit() as data:
            value = data["n"]
            time.sleep(0.01)  # 放大竞态窗口；若无锁，两个线程会读到同一个值
            data["n"] = value + 1

    import threading
    threads = [threading.Thread(target=increment) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert store.snapshot()["n"] == 2


def _lock_holder(lock_path, events):
    with file_lock(lock_path):
        events.put("acquired")
        time.sleep(0.3)
        events.put("released")


def test_file_lock_mutual_exclusion(tmp_path):
    """跨进程锁：子进程持锁期间，父进程需等待。"""
    lock_path = tmp_path / "resource.json"
    events = mp.Queue()

    ctx = mp.get_context("spawn") if os.name == "nt" else mp.get_context("fork")
    proc = ctx.Process(target=_lock_holder, args=(lock_path, events))
    proc.start()
    assert events.get(timeout=5) == "acquired"
    start = time.monotonic()
    with file_lock(lock_path):
        waited = time.monotonic() - start
    proc.join(10)
    assert waited >= 0.15, f"父进程未等待子进程释放锁：{waited:.3f}s"
    assert events.get(timeout=5) == "released"


def test_doc_key_only_for_data_dir(tmp_path, monkeypatch):
    """只有 data/ 下的文档走库；测试临时目录保持纯文件模式。"""
    from app.services import shared_json as sj

    monkeypatch.setattr(sj.settings, "config_store", "db", raising=False)
    assert sj._doc_key(tmp_path / "x.json") is None
    assert sj._doc_key(sj.DATA_DIR / "skills.json") == "skills.json"
    assert sj._doc_key(sj.DATA_DIR / "uploads" / "index.json") == "uploads/index.json"

    monkeypatch.setattr(sj.settings, "config_store", "file", raising=False)
    assert sj._doc_key(sj.DATA_DIR / "skills.json") is None


def test_file_mode_ignores_db_backend(tmp_path, monkeypatch):
    """CONFIG_STORE=file 时必须只碰文件，绝不连库（回滚开关的语义）。"""
    from app.services import shared_json as sj

    monkeypatch.setattr(sj.settings, "config_store", "file", raising=False)
    path = tmp_path / "roles.json"
    with SharedJson(path, default=[]).edit() as data:
        data.append({"a": 1})
    assert json.loads(path.read_text(encoding="utf-8")) == [{"a": 1}]
