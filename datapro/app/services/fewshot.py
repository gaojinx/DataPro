"""few-shot 检索：按**查询形状**挑示例，而不是按字面相似度。

形状 = 输出粒度（单值 / 时间序列 / 维度分组）× 指标类型（计数 / 求和 / 均值）。
每条示例都过校验器才入库；示例错一条会污染全部生成。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger("datapro.fewshot")

_PATH = Path(__file__).resolve().parents[2] / "prompts" / "fewshot.json"
_DYNAMIC_PATH = Path(__file__).resolve().parents[2] / "data" / "fewshot_dynamic.jsonl"
_EXAMPLES: list[dict] | None = None
_DYNAMIC_MTIME: float | None = None

_SUM_WORDS = ("销售额", "金额", "GMV", "gmv", "收入", "流水", "成本")
_AVG_WORDS = ("客单价", "均价", "平均", "人均")
_RATIO_WORDS = ("率", "占比", "比例")


def metric_kind(text: str) -> str:
    """指标类型：avg / sum / ratio / count（示例入库与检索共用同一判定）。"""
    metric = (text or "").lower()
    if any(w in metric for w in _AVG_WORDS):
        return "avg"
    if any(w in metric for w in _SUM_WORDS):
        return "sum"
    if any(w in metric for w in _RATIO_WORDS):
        return "ratio"
    return "count"


def _load_dynamic() -> list[dict]:
    """动态示例（A06）：来自人工复核过的 golden，SQL 必须用日期占位符。

    文件变化时自动重载；硬编码日期的示例一律跳过——示例会污染全部生成。
    """
    global _DYNAMIC_MTIME
    if not _DYNAMIC_PATH.exists():
        return []
    mtime = _DYNAMIC_PATH.stat().st_mtime
    if _DYNAMIC_MTIME == mtime:
        return _DYNAMIC_CACHE
    rows = []
    try:
        for line in _DYNAMIC_PATH.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            ex = json.loads(line)
            sql = ex.get("sql") or ""
            if not ex.get("question") or "{date_start}" not in sql or "{date_end}" not in sql:
                continue
            ex.setdefault("source", "golden")
            rows.append(ex)
    except Exception as exc:  # noqa: BLE001 —— 动态示例损坏不影响静态示例
        logger.warning("动态 few-shot 加载失败：%s", exc)
        rows = []
    _DYNAMIC_CACHE.clear()
    _DYNAMIC_CACHE.extend(rows)
    _DYNAMIC_MTIME = mtime
    return _DYNAMIC_CACHE


_DYNAMIC_CACHE: list[dict] = []


def _load() -> list[dict]:
    global _EXAMPLES
    if _EXAMPLES is None:
        try:
            static = json.loads(_PATH.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 —— 示例缺失不影响主链路
            logger.warning("few-shot 示例加载失败：%s", exc)
            static = []
        merged, seen = [], set()
        for ex in static + _load_dynamic():
            q = ex.get("question") or ""
            if q in seen:
                continue
            seen.add(q)
            merged.append(ex)
        _EXAMPLES = merged
    return _EXAMPLES


def _grain_kind(spec) -> str:
    if spec is None:
        return "total"
    if spec.dimensions:
        return "dim"
    if spec.grain in ("day", "week", "month"):
        return "time"
    return "total"


def _metric_kind(spec) -> str:
    return metric_kind(getattr(spec, "metric", "") or "")


def pick(spec, tables: list[str] | None, k: int = 2) -> list[dict]:
    """按形状打分取前 k 条，保证至少覆盖一个同形状示例。"""
    examples = _load()
    if not examples:
        return []
    gk, mk = _grain_kind(spec), _metric_kind(spec)
    n_dims = min(len(getattr(spec, "dimensions", None) or []), 2)
    tables = [t.lower() for t in (tables or [])]

    scored = []
    for ex in examples:
        score = 0.0
        if ex.get("grain") == gk:
            score += 4
        if ex.get("metric") == mk:
            score += 3
        if gk == "dim":
            ex_dims, ex_tables = ex.get("dims", 1), [t.lower() for t in ex.get("tables", [])]
            score -= 0.5 * abs(min(ex_dims, 2) - n_dims)
            if len(ex_tables) > 1 and len(tables) > 1:
                score += 1.5          # 需要 JOIN 时优先给 JOIN 示例
        if set(tables) & {t.lower() for t in ex.get("tables", [])}:
            score += 1
        if ex.get("case_id") or ex.get("source") == "golden":
            score += 1.0          # 历史示例（复核过的 golden）优先占用同形状名额
        scored.append((score, ex))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    picked, per_kind = [], {}
    for score, ex in scored:
        if score <= 0:
            continue
        kind = ex.get("grain")
        if per_kind.get(kind, 0) >= 2:
            continue              # 同形状最多 2 条：静态示例 + 历史示例，控制 prompt 长度
        per_kind[kind] = per_kind.get(kind, 0) + 1
        picked.append(ex)
        if len(picked) >= k:
            break
    return picked


def render(spec, tables: list[str] | None, k: int = 2) -> str:
    """渲染成 prompt 片段；无示例时明确写「无」，避免模型臆想存在示例。"""
    picked = pick(spec, tables, k)
    if not picked:
        return "无"
    blocks = []
    for idx, ex in enumerate(picked, start=1):
        blocks.append(f"### 示例{idx}（{ex.get('grain')}/{ex.get('metric')}）\n"
                      f"问题：{ex.get('question')}\n"
                      f"SQL：{ex.get('sql')}")
    return "\n\n".join(blocks)
