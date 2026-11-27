"""新媒体行业知识库：常见指标的术语、别名、定义与计算口径。

- JSON 持久化（data/knowledge.json）
- 全员可读；增删改仅管理员
- 用于元数据/口径问答（"什么是完播率""涨粉率怎么算"），命中即回答，不编造
"""
from __future__ import annotations  # 类方法 list 会遮蔽内建 list，注解需延迟求值

import re
import time
import uuid
from pathlib import Path

from app.services.shared_json import SharedJson

DATA_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "knowledge.json"

SEED = [
    {"term": "曝光量", "aliases": ["展现量", "曝光次数"], "category": "流量",
     "definition": "内容在信息流中被展示的次数，同一用户多次看到重复计数",
     "formula": "曝光量 = 内容被展示的总次数"},
    {"term": "播放量", "aliases": ["播放次数", "VV"], "category": "流量",
     "definition": "内容被实际播放的次数，同一用户多次播放重复计数",
     "formula": "播放量 = 有效播放次数（各平台对有效播放时长有不同门槛）"},
    {"term": "完播率", "aliases": ["完整播放率"], "category": "内容质量",
     "definition": "完整观看内容的人数占播放人数的比例，衡量内容吸引力",
     "formula": "完播率 = 完播次数 / 播放次数 × 100%"},
    {"term": "互动率", "aliases": ["互动成本"], "category": "内容质量",
     "definition": "点赞、评论、分享、收藏等互动行为占播放或曝光的比例",
     "formula": "互动率 =（点赞 + 评论 + 分享 + 收藏）/ 播放量 × 100%"},
    {"term": "点赞率", "aliases": [], "category": "内容质量",
     "definition": "点赞数占播放量的比例，反映内容认可度",
     "formula": "点赞率 = 点赞数 / 播放量 × 100%"},
    {"term": "评论率", "aliases": [], "category": "内容质量",
     "definition": "评论数占播放量的比例，反映内容引发讨论的程度",
     "formula": "评论率 = 评论数 / 播放量 × 100%"},
    {"term": "分享率", "aliases": ["转发率"], "category": "内容质量",
     "definition": "分享数占播放量的比例，反映内容的传播意愿",
     "formula": "分享率 = 分享数 / 播放量 × 100%"},
    {"term": "涨粉率", "aliases": ["粉丝增长率"], "category": "账号增长",
     "definition": "新增粉丝数占曝光或播放量的比例，衡量内容转化为粉丝的效率",
     "formula": "涨粉率 = 新增粉丝数 / 播放量 × 100%"},
    {"term": "取关率", "aliases": ["流失率"], "category": "账号增长",
     "definition": "取消关注人数占期初粉丝数的比例",
     "formula": "取关率 = 取关人数 / 期初粉丝数 × 100%"},
    {"term": "粉丝净增", "aliases": ["净增粉丝"], "category": "账号增长",
     "definition": "统计周期内新增粉丝减去取关粉丝的净变化",
     "formula": "粉丝净增 = 新增粉丝 - 取关粉丝"},
    {"term": "CTR", "aliases": ["点击率", "点击通过率"], "category": "广告投放",
     "definition": "用户点击次数占曝光次数的比例，衡量素材或标题的吸引力",
     "formula": "CTR = 点击次数 / 曝光次数 × 100%"},
    {"term": "CPM", "aliases": ["千次曝光成本"], "category": "广告投放",
     "definition": "广告每获得一千次曝光所需的花费",
     "formula": "CPM = 广告花费 / 曝光量 × 1000"},
    {"term": "CPC", "aliases": ["单次点击成本"], "category": "广告投放",
     "definition": "广告每次被点击所花费的平均成本",
     "formula": "CPC = 广告花费 / 点击次数"},
    {"term": "CPA", "aliases": ["转化成本"], "category": "广告投放",
     "definition": "每获得一次目标转化（下单、注册、留资等）的平均成本",
     "formula": "CPA = 广告花费 / 转化数"},
    {"term": "ROI", "aliases": ["投入产出比"], "category": "广告投放",
     "definition": "投放带来的收入与花费的比值，衡量投放效率",
     "formula": "ROI = 收入 / 广告花费"},
    {"term": "转化率", "aliases": ["CVR"], "category": "转化",
     "definition": "完成目标行为的用户占触达用户的比例",
     "formula": "转化率 = 转化数 / 触达人数 × 100%"},
    {"term": "UV", "aliases": ["独立访客"], "category": "流量",
     "definition": "统计周期内的去重访客数，同一用户多次访问只计一次",
     "formula": "UV = 去重后的访客数"},
    {"term": "PV", "aliases": ["页面浏览量"], "category": "流量",
     "definition": "页面被访问的总次数，同一用户多次访问重复计数",
     "formula": "PV = 页面访问总次数"},
    {"term": "DAU", "aliases": ["日活跃用户数", "日活"], "category": "用户",
     "definition": "统计日内有活跃行为的去重用户数",
     "formula": "DAU = 当日活跃用户去重数"},
    {"term": "MAU", "aliases": ["月活跃用户数", "月活"], "category": "用户",
     "definition": "统计月内有活跃行为的去重用户数",
     "formula": "MAU = 当月活跃用户去重数"},
    {"term": "GMV", "aliases": ["成交总额"], "category": "电商",
     "definition": "一定周期内成交订单的总金额，含未支付/退款订单时需注明口径",
     "formula": "GMV = 成交订单金额之和"},
    {"term": "客单价", "aliases": ["ARPU", "平均订单金额"], "category": "电商",
     "definition": "平均每个下单用户贡献的成交金额",
     "formula": "客单价 = GMV / 下单用户数"},
    {"term": "爆款率", "aliases": ["爆文率"], "category": "内容质量",
     "definition": "播放或互动超过阈值的作品数占发布作品数的比例",
     "formula": "爆款率 = 爆款作品数 / 发布作品数 × 100%"},
    {"term": "流量池", "aliases": ["推荐池"], "category": "平台机制",
     "definition": "平台推荐机制中按内容表现逐级放量的流量分层",
     "formula": "概念指标，无固定计算公式"},
    {"term": "私域", "aliases": ["私域流量"], "category": "平台机制",
     "definition": "品牌可重复触达、低成本运营的自有用户阵地（社群、企微、会员等）",
     "formula": "概念指标，按私域用户数与复购率衡量"},
    {"term": "种草", "aliases": ["内容种草"], "category": "平台机制",
     "definition": "通过内容激发用户购买兴趣的行为，常用搜索量、加购率衡量",
     "formula": "种草效果 = 搜索增长 / 内容曝光"},
    {"term": "KOL", "aliases": ["关键意见领袖"], "category": "达人合作",
     "definition": "在特定领域有较大影响力和粉丝基础的创作者",
     "formula": "按粉丝量级与互动率分层评估"},
    {"term": "KOC", "aliases": ["关键意见消费者"], "category": "达人合作",
     "definition": "粉丝量较小但真实感强、转化效果好的创作者",
     "formula": "按合作成本与转化效果评估"},
]


class KnowledgeBase:
    def __init__(self, path: Path | None = None):
        self._store = SharedJson(path or DATA_FILE, default=[])
        if not self._store.path.exists():
            with self._store.edit() as items:
                if not items:
                    items.extend([{
                        "id": uuid.uuid4().hex[:8],
                        "term": k["term"], "aliases": k["aliases"], "category": k["category"],
                        "definition": k["definition"], "formula": k["formula"],
                        "created_at": time.strftime("%Y-%m-%d %H:%M"),
                    } for k in SEED])

    def list(self, keyword: str = "") -> list[dict]:
        items = [dict(i) for i in self._store.snapshot()]
        kw = (keyword or "").strip().lower()
        if not kw:
            return items
        return [i for i in items
                if kw in i["term"].lower() or any(kw in a.lower() for a in i.get("aliases", []))
                or kw in (i.get("definition") or "").lower() or kw in (i.get("category") or "").lower()]

    def match(self, question: str, limit: int = 3) -> list[dict]:
        """问题中出现的术语/别名（按命中长度降序，取前几条）。"""
        q = re.sub(r"[\s，。！？、,.!?：:；;]+", "", (question or "").lower())
        hits = []
        for item in self._store.snapshot():
            names = [item["term"]] + list(item.get("aliases") or [])
            best = max((len(n) for n in names if len(n) >= 2 and n.lower() in q), default=0)
            if best:
                hits.append((best, dict(item)))
        hits.sort(key=lambda x: -x[0])
        return [h for _, h in hits[:limit]]

    def add(self, data: dict) -> dict:
        item = {
            "id": uuid.uuid4().hex[:8],
            "term": data["term"].strip(),
            "aliases": [a.strip() for a in (data.get("aliases") or []) if a.strip()],
            "category": (data.get("category") or "通用").strip(),
            "definition": (data.get("definition") or "").strip(),
            "formula": (data.get("formula") or "").strip(),
            "created_at": time.strftime("%Y-%m-%d %H:%M"),
        }
        with self._store.edit() as items:
            items.append(item)
        return dict(item)

    def update(self, item_id: str, patch: dict) -> dict | None:
        with self._store.edit() as items:
            for i in items:
                if i["id"] == item_id:
                    for k in ("term", "category", "definition", "formula"):
                        if k in patch:
                            i[k] = (patch[k] or "").strip()
                    if "aliases" in patch:
                        i["aliases"] = [a.strip() for a in (patch["aliases"] or []) if a.strip()]
                    return dict(i)
        return None

    def delete(self, item_id: str) -> bool:
        with self._store.edit() as items:
            kept = [i for i in items if i["id"] != item_id]
            if len(kept) == len(items):
                return False
            items[:] = kept
            return True


knowledge_base = KnowledgeBase()
