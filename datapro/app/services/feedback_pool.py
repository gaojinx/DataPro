"""👍 反馈自动候选池（D04）。

- 用户在结果上点 👍 时，把问题、上下文与 SQL 追加到 `data/feedback_candidates.jsonl`
- harvest 会把池里的条目并入草稿（reviewed=false，无断言），人工复核后才 promote
- 只做候选，不自动进评测集；👎 不进池，走 failure_taxonomy
- 多实例加固：跨进程锁内 read-modify-write，写入先落临时文件再 replace
"""
import json
import logging
import os
import uuid
from pathlib import Path

from app.services.shared_json import file_lock

logger = logging.getLogger("datapro.feedback_pool")

DATA_FILE = Path(__file__).resolve().parents[2] / "data" / "feedback_candidates.jsonl"
MAX_ENTRIES = 500


class FeedbackPool:
    def __init__(self, path: Path | None = None):
        self._path = path or DATA_FILE

    def _read(self) -> list[dict]:
        if not self._path.exists():
            return []
        out = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("跳过损坏的候选行：%s", line[:80])
        return out

    def _write(self, entries: list[dict]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(f".{self._path.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp")
        try:
            tmp.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in entries),
                           encoding="utf-8")
            os.replace(tmp, self._path)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    def capture(self, entry: dict) -> bool:
        """按 query_id 去重追加；返回是否新增。文件不可写只记日志，不影响反馈主流程。"""
        query_id = entry.get("query_id")
        if query_id is None:
            return False
        try:
            with file_lock(self._path):
                entries = self._read()
                if any(e.get("query_id") == query_id for e in entries):
                    return False
                entries.append(entry)
                self._write(entries[-MAX_ENTRIES:])
                return True
        except Exception as exc:  # noqa: BLE001 —— 候选池是旁路，不能拖垮反馈
            logger.warning("写入 👍 候选池失败：%s", exc)
            return False

    def read_all(self) -> list[dict]:
        try:
            with file_lock(self._path):
                return self._read()
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取 👍 候选池失败：%s", exc)
            return []


feedback_pool = FeedbackPool()
