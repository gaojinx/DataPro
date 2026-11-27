"""定时任务注册表：按每日/每周固定时间自动执行取数问题，并保留最近运行记录。

- JSON 文件持久化（data/schedules.json）
- 调度由 schedule_runner 用 APScheduler 的 cron 作业执行
- 多实例加固：mtime 缓存失效 + 跨进程锁内 read-modify-write + 原子写
"""
import time
import uuid
from pathlib import Path

from app.services.shared_json import SharedJson

DATA_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "schedules.json"
MAX_RUNS = 20


def _store() -> SharedJson:
    return SharedJson(DATA_FILE, default=[])


def _normalize_type(value: object) -> str:
    """任务类型：type=ingest 走采集器；其余（含缺省）一律 question，向后兼容。"""
    return "ingest" if str(value or "").strip().lower() == "ingest" else "question"


class ScheduleRegistry:
    def list(self, owner: str | None = None, all_users: bool = False) -> list[dict]:
        """owner + all_users：普通用户只看自己创建的任务，管理员可看全部。"""
        tasks = [dict(t) for t in _store().snapshot()]
        if all_users or not owner:
            return tasks
        return [t for t in tasks if t.get("owner") == owner]

    def get(self, task_id: str) -> dict | None:
        t = next((x for x in _store().snapshot() if x["id"] == task_id), None)
        return dict(t) if t else None

    def add(self, data: dict, owner: str) -> dict:
        item = {
            "id": uuid.uuid4().hex[:8],
            "name": data["name"].strip(),
            "type": _normalize_type(data.get("type")),
            "ingest_code": str(data.get("ingest_code") or "").strip(),
            "question": str(data.get("question") or "").strip(),
            "freq": "weekly" if data.get("freq") == "weekly" else "daily",
            "time": data.get("time", "09:00"),
            "weekday": int(data.get("weekday") or 1),   # 1=周一 … 7=周日
            "enabled": bool(data.get("enabled", True)),
            "owner": owner,
            "created_at": time.strftime("%Y-%m-%d %H:%M"),
            "last_run_at": None,
            "last_run_date": None,
            "last_status": None,
            "runs": [],
        }
        with _store().edit() as items:
            items.append(item)
        return dict(item)

    def update(self, task_id: str, patch: dict) -> dict | None:
        with _store().edit() as items:
            for t in items:
                if t["id"] == task_id:
                    for k in ("name", "question", "time", "enabled", "ingest_code"):
                        if k in patch:
                            t[k] = patch[k]
                    if "type" in patch:
                        t["type"] = _normalize_type(patch["type"])
                    if "freq" in patch:
                        t["freq"] = "weekly" if patch["freq"] == "weekly" else "daily"
                    if "weekday" in patch:
                        t["weekday"] = int(patch["weekday"] or 1)
                    return dict(t)
        return None

    def delete(self, task_id: str) -> bool:
        with _store().edit() as items:
            kept = [t for t in items if t["id"] != task_id]
            if len(kept) == len(items):
                return False
            items[:] = kept
            return True

    def delete_by_owner(self, owner: str) -> int:
        """删除某用户创建的全部任务（删除账号时清理），返回删除数量。"""
        owner = (owner or "").strip()
        if not owner:
            return 0
        with _store().edit() as items:
            kept = [t for t in items if t.get("owner") != owner]
            removed = len(items) - len(kept)
            if removed:
                items[:] = kept
            return removed

    def record_run(self, task_id: str, run: dict) -> None:
        with _store().edit() as items:
            for t in items:
                if t["id"] == task_id:
                    t["last_run_at"] = run["ts"]
                    t["last_run_date"] = run["ts"][:10]
                    t["last_status"] = run["status"]
                    t["runs"] = ([run] + t.get("runs", []))[:MAX_RUNS]
                    return


schedule_registry = ScheduleRegistry()
