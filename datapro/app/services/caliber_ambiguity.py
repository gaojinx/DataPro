"""口径歧义检测（P1）：同一指标词命中多个口径时先澄清，绝不猜口径。

- 检测：问题里同一个指标词同时命中 ≥2 个口径，且口径公式不同 → 歧义
- 澄清：给出 A/B 选项与定义摘要，由用户确认后重新取数
- 选择：支持 A/B、序号、口径编号、口径名/别名与 choice_keywords（如「注册」「支付」）
"""
from __future__ import annotations


def _match_terms(caliber: dict, q: str) -> set[str]:
    terms: set[str] = set()
    for name in [caliber.get("metric_name", "")] + list(caliber.get("aliases") or []):
        if not name:
            continue
        if name.lower() in q:
            terms.add(name)
            continue
        stem = name.rstrip("数率量")
        if len(stem) >= 3 and stem.lower() in q:
            terms.add(stem)
    return terms


def ambiguous_groups(calibers: list[dict], question: str) -> list[dict]:
    """返回 [{term, options}]；只包含同一词命中多口径且公式不同的组合。"""
    q = (question or "").lower()
    groups: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for i, left in enumerate(calibers):
        left_terms = _match_terms(left, q)
        if not left_terms:
            continue
        for right in calibers[i + 1:]:
            if left.get("metric_id") == right.get("metric_id"):
                continue
            if (left.get("logic") or "") == (right.get("logic") or ""):
                continue
            shared = left_terms & _match_terms(right, q)
            if not shared:
                continue
            key = tuple(sorted([str(left.get("metric_id")), str(right.get("metric_id"))]))
            if key in seen:
                continue
            seen.add(key)
            groups.append({"term": sorted(shared, key=len)[0], "options": [left, right]})
    return groups


def option_view(caliber: dict) -> dict:
    """会话里保存/前端展示用的口径选项（保留匹配所需关键词）。"""
    return {
        "metric_id": caliber.get("metric_id", ""),
        "metric_name": caliber.get("metric_name", ""),
        "definition": caliber.get("definition", ""),
        "aliases": caliber.get("aliases") or [],
        "choice_keywords": caliber.get("choice_keywords") or [],
    }


def clarify_text(group: dict) -> str:
    term = group.get("term") or "该指标"
    parts = [f"「{term}」有 {len(group['options'])} 种统计口径，请确认后我再取数："]
    for i, opt in enumerate(group["options"]):
        letter = "AB"[i] if i < 2 else str(i + 1)
        parts.append(f"{letter}. {opt.get('metric_name')}（{opt.get('definition')}，{opt.get('metric_id')}）；")
    parts.append("回复 A 或 B 即可按所选口径重新取数。")
    return "".join(parts)


def match_choice(message: str, options: list[dict]) -> dict | None:
    """解析用户的口径选择；无法判断时返回 None。"""
    m = (message or "").strip().lower()
    if not m or not options:
        return None
    for opt in options:                     # 口径编号优先（M004 这类）
        mid = str(opt.get("metric_id", "")).lower()
        if mid and mid in m:
            return opt
    for opt in options:                     # 显式关键词（注册 / 支付）
        for kw in opt.get("choice_keywords") or []:
            if kw and kw.lower() in m:
                return opt
    for opt in options:                     # 口径名/别名
        for name in [opt.get("metric_name", "")] + list(opt.get("aliases") or []):
            if name and name.lower() in m:
                return opt
    if len(m) <= 4:                         # 短回复：A/B、1/2、第一个/第二个
        tokens = {"a": 0, "b": 1, "1": 0, "2": 1,
                  "第一": 0, "第二": 1, "第一个": 0, "第二个": 1}
        for token, idx in tokens.items():
            if token in m and idx < len(options):
                return options[idx]
    return None
