"""口径冲突裁决（D03）。

- 管理员对反复出现的歧义词（如「新增用户」）做出裁决：默认取哪个口径
- 记录写入 `data/caliber_rulings.json`：{term: {metric_id, note, by, at}}
- 歧义检测时若命中裁决且该口径在候选项中 → 直接采用，并在 assumptions 里声明；
  未裁决的歧义仍然澄清，绝不猜
"""
import logging
from datetime import datetime
from pathlib import Path

from app.services.shared_json import SharedJson

logger = logging.getLogger("datapro.caliber_rulings")

DATA_FILE = Path(__file__).resolve().parents[2] / "data" / "caliber_rulings.json"


class CaliberRulings:
    def __init__(self, path: Path | None = None):
        self._path = path or DATA_FILE

    def _store(self) -> SharedJson:
        return SharedJson(self._path, default={})

    def read(self) -> dict[str, dict]:
        data = self._store().snapshot()
        return data if isinstance(data, dict) else {}

    def resolve(self, term: str) -> dict | None:
        return self.read().get((term or "").strip().lower())

    def set(self, term: str, metric_id: str, note: str, by: str) -> dict:
        key = (term or "").strip().lower()
        if not key or not metric_id:
            raise ValueError("term 与 metric_id 必填")
        ruling = {"metric_id": metric_id, "note": note,
                  "by": by, "at": datetime.now().isoformat(timespec="seconds")}
        with self._store().edit(default={}) as data:
            data[key] = ruling
        return {"term": key, **ruling}

    def remove(self, term: str) -> bool:
        key = (term or "").strip().lower()
        with self._store().edit(default={}) as data:
            if key not in data:
                return False
            data.pop(key)
            return True


caliber_rulings = CaliberRulings()
