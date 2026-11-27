"""技能包注册表（Skill）：可复用的问法模板，供首页建议与快速发起取数。

- JSON 文件持久化（data/skills.json）；全员可读启用项；增删改仅管理员（路由层校验）
- answer 非空时为「预设回复」技能：发送内容命中后直接返回该回复，不走取数链路
- 多实例加固：mtime 缓存失效 + 跨进程锁内 read-modify-write + 原子写
"""
import re
import time
import uuid
from pathlib import Path

from app.services.shared_json import SharedJson

DATA_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "skills.json"

SEED = [
    {"name": "支付订单数", "description": "按口径 M003 统计指定日期的支付订单数",
     "question": "查昨天的支付订单数", "enabled": True},
    {"name": "品类销量", "description": "按品类分组汇总销量", "question": "本周每个品类的销量", "enabled": True},
    {"name": "口径速查", "description": "询问指标口径与计算逻辑", "question": "支付订单数的口径是什么", "enabled": True},
]


class SkillRegistry:
    def __init__(self, path: Path | None = None):
        self._store = SharedJson(path or DATA_FILE, default=[])
        if not self._store.path.exists():
            with self._store.edit() as items:
                if not items:
                    items.extend([{
                        "id": uuid.uuid4().hex[:8],
                        "name": s["name"],
                        "description": s["description"],
                        "question": s["question"],
                        "answer": s.get("answer", ""),
                        "enabled": s["enabled"],
                        "created_at": time.strftime("%Y-%m-%d %H:%M"),
                    } for s in SEED])

    def list(self, only_enabled: bool = False) -> list[dict]:
        items = [dict(s) for s in self._store.snapshot()]
        return [s for s in items if s["enabled"]] if only_enabled else items

    def add(self, data: dict) -> dict:
        item = {
            "id": uuid.uuid4().hex[:8],
            "name": data["name"].strip(),
            "description": data.get("description", "").strip(),
            "question": data["question"].strip(),
            "answer": (data.get("answer") or "").strip(),
            "enabled": bool(data.get("enabled", True)),
            "created_at": time.strftime("%Y-%m-%d %H:%M"),
        }
        with self._store.edit() as items:
            items.append(item)
        return dict(item)

    def update(self, skill_id: str, patch: dict) -> dict | None:
        with self._store.edit() as items:
            for s in items:
                if s["id"] == skill_id:
                    for k in ("name", "description", "question", "answer", "enabled"):
                        if k in patch:
                            s[k] = patch[k].strip() if isinstance(patch[k], str) else patch[k]
                    return dict(s)
        return None

    def match_answer(self, question: str) -> dict | None:
        """命中「预设回复」技能：启用 + answer 非空 + 发送内容一致（忽略空白与标点差异）。"""
        def norm(t: str) -> str:
            return re.sub(r"[\s，。！？、,.!?：:；;]+", "", (t or "").lower())
        target = norm(question)
        if not target:
            return None
        for s in self._store.snapshot():
            if s.get("enabled") and (s.get("answer") or "").strip() and norm(s["question"]) == target:
                return dict(s)
        return None

    def delete(self, skill_id: str) -> bool:
        with self._store.edit() as items:
            kept = [s for s in items if s["id"] != skill_id]
            if len(kept) == len(items):
                return False
            items[:] = kept
            return True


skill_registry = SkillRegistry()
