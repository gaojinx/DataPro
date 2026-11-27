"""召回服务：向量 + 关键词双路召回 → 精排（HLD §3）。

骨架阶段：关键词内存召回 + 降级模式；接入 Milvus 后启用向量路。
召回不到 → 空 tables → 状态机直接 REJECT(E002)，绝不给模型自由发挥空间。

粒度：表级 + **字段级（二级检索）**。字段级不改变「召回哪些表」的语义，
而是回答「这张表里哪些字段跟问题有关」——用于表补位（表关键词没覆盖到的字段名）
与生成阶段把相关字段前置。
"""
import asyncio
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.config import settings
from app.services.metadata_store import metadata_store
from app.services.vector_recall import flatten_columns, vector_recall

logger = logging.getLogger("datapro.recall")


@dataclass
class RecallResult:
    tables: list[dict] = field(default_factory=list)   # [{table_name, schema, description, score}]
    calibers: list[dict] = field(default_factory=list) # [{metric_id, metric_name, definition, logic}]
    knowledge: list[dict] = field(default_factory=list)  # 行业知识库命中（术语/定义/口径）
    column_hits: dict[str, list[str]] = field(default_factory=dict)  # 表 → 命中字段名


# 骨架种子元数据：由元数据字典（DataPro-元数据与口径库模板）导入替换
# keywords 为人工维护的检索关键词（接入向量库后由语义召回替代）
SEED_TABLES = [
    {"table_name": "dwd_order_detail_di", "schema": "dwd_shop",
     "description": "订单明细日报表，一行=一笔订单一个商品，含支付状态、退款标记、地域与商品信息",
     "keywords": ["订单", "销量", "销售额", "支付", "退款", "GMV", "品类", "渠道", "区域",
                  "城市", "商品", "购买", "买过", "复购", "连带"],
     "columns": ["order_id", "user_id", "pay_status", "amount", "category", "channel", "dt",
                 "region", "province", "city", "goods_id", "goods_name", "qty"]},
    {"table_name": "dwd_user_di", "schema": "dwd_shop",
     "description": "用户行为日报表，含注册、登录、活跃标记",
     "keywords": ["用户", "注册", "登录", "活跃", "日活", "新增用户"],
     "columns": ["user_id", "register_date", "login_date", "is_active", "dt"]},
    {"table_name": "dim_channel", "schema": "dwd_shop",
     "description": "渠道维表：channel_id 为数字 ID，channel_name 为渠道名称；"
                    "订单表 dwd_order_detail_di.channel 存的是渠道名称，关联时请用 o.channel = c.channel_name",
     "keywords": ["渠道", "流量来源"], "columns": ["channel_id", "channel_name"]},
    {"table_name": "dim_goods", "schema": "dwd_shop",
     "description": "商品维表：商品名称、所属品类、品牌、标准单价，用于商品与品类维度分析",
     "keywords": ["商品", "产品", "SKU", "品牌", "品类", "单价"],
     "columns": ["goods_id", "goods_name", "category", "brand", "unit_price", "launch_date",
                 "status", "supplier"]},
    {"table_name": "dim_region", "schema": "dwd_shop",
     "description": "地域维表：区域-省份-城市三级地域，城市等级（一线/新一线/二线）",
     "keywords": ["区域", "省份", "城市", "地域", "华东", "华北", "华南", "华中", "西南", "西北", "东北"],
     "columns": ["region_id", "region_name", "province", "city", "city_level", "is_active", "remark"]},
    {"table_name": "dws_category_daily", "schema": "dwd_shop",
     "description": "品类日汇总表：按天×品类聚合销售额、支付订单数、销量与退款订单数",
     "keywords": ["品类", "汇总", "日报", "销量", "销售额"],
     "columns": ["dt", "category", "sales_amount", "order_cnt", "qty", "refund_cnt"]},
    {"table_name": "dws_channel_daily", "schema": "dwd_shop",
     "description": "渠道日汇总表：按天×渠道聚合销售额、支付订单数、销量与退款订单数",
     "keywords": ["渠道", "汇总", "日报", "销售额"],
     "columns": ["dt", "channel", "sales_amount", "order_cnt", "qty", "refund_cnt"]},
    {"table_name": "dim_campaign", "schema": "dwd_shop",
     "description": "投放计划维表：活动名称、投放渠道、预算、起止日期与状态",
     "keywords": ["投放", "广告", "计划", "活动", "预算", "渠道", "ROI", "推广"],
     "columns": ["campaign_id", "campaign_name", "channel", "budget", "start_date",
                 "end_date", "status", "owner"]},
    {"table_name": "dwd_campaign_daily", "schema": "dwd_shop",
     "description": "投放日数据表：按天×计划聚合花费、曝光、点击、转化与成交 GMV",
     "keywords": ["广告", "投放", "花费", "成本", "消耗", "曝光", "点击", "转化", "ROI", "GMV", "推广"],
     "columns": ["dt", "campaign_id", "channel", "cost", "impressions", "clicks",
                 "conversions", "gmv"]},
    {"table_name": "dwd_refund_detail_di", "schema": "dwd_shop",
     "description": "退款明细表：订单退款金额、原因、状态与申请/完成日期",
     "keywords": ["退款", "售后", "退款原因", "退款单"],
     "columns": ["refund_id", "order_id", "user_id", "amount", "refund_reason",
                 "refund_status", "apply_date", "finish_date", "dt"]},
    {"table_name": "dws_goods_daily", "schema": "dwd_shop",
     "description": "商品日汇总表：按天×商品聚合销售额、销量与支付订单数",
     "keywords": ["商品", "销量", "销售额", "品类", "品牌", "日报", "SKU"],
     "columns": ["dt", "goods_id", "goods_name", "category", "brand", "sales_amount",
                 "qty", "order_cnt"]},
]

SEED_CALIBERS = [
    {"metric_id": "M003", "metric_name": "支付订单数", "aliases": ["付费订单量", "成交订单数", "支付订单", "支付单量", "成交单量", "订单笔数"],
     "table": "dwd_order_detail_di",
     "definition": "当日支付成功且未退款的订单去重数",
     "logic": "COUNT(DISTINCT order_id) WHERE pay_status=1 AND dt='{date}'"},
    {"metric_id": "M001", "metric_name": "日活跃用户数", "aliases": ["日活", "DAU", "活跃用户", "日活跃", "活跃人数"],
     "table": "dwd_user_di",
     "definition": "当日有登录行为的用户去重数",
     "logic": "COUNT(DISTINCT user_id) WHERE dt='{date}'"},
    # 「新增用户」存在两种口径（P1 口径歧义检测示例）：注册口径 vs 下单口径
    {"metric_id": "M004", "metric_name": "新增用户数（注册口径）",
     "aliases": ["新增用户", "新用户", "拉新用户", "新客", "拉新人数"],
     "table": "dwd_user_di",
     "definition": "当日完成注册的新用户数，按注册日期统计",
     "logic": "COUNT(DISTINCT user_id) WHERE register_date='{date}'",
     "choice_keywords": ["注册", "拉新"]},
    {"metric_id": "M005", "metric_name": "新增用户数（下单口径）",
     "aliases": ["新增用户", "新用户", "拉新用户", "新客", "拉新人数"],
     "table": "dwd_order_detail_di",
     "definition": "当日有支付成功订单的去重用户数，按下单行为统计",
     "logic": "COUNT(DISTINCT user_id) WHERE pay_status=1 AND dt='{date}'",
     "choice_keywords": ["支付", "下单", "成交"]},
    {"metric_id": "M006", "metric_name": "销售额",
     "aliases": ["GMV", "成交金额", "营收", "总销售额", "营业额", "流水", "成交额"],
     "table": "dwd_order_detail_di",
     "definition": "当日支付成功订单的商品金额合计，不含退款",
     "logic": "SUM(amount) WHERE pay_status=1 AND dt='{date}'"},
    {"metric_id": "M007", "metric_name": "销量",
     "aliases": ["销售量", "件数", "销售件数", "卖出件数"],
     "table": "dwd_order_detail_di",
     "definition": "当日支付成功订单的商品件数合计",
     "logic": "SUM(qty) WHERE pay_status=1 AND dt='{date}'"},
    {"metric_id": "M008", "metric_name": "客单价",
     "aliases": ["平均订单金额", "每单金额", "平均客单价", "单均价", "客单"],
     "table": "dwd_order_detail_di",
     "definition": "当日支付金额除以支付订单数",
     "logic": "SUM(amount)/COUNT(DISTINCT order_id) WHERE pay_status=1 AND dt='{date}'"},
    {"metric_id": "M009", "metric_name": "退款金额",
     "aliases": ["退款额", "退货金额", "退款总额", "退货额"],
     "table": "dwd_order_detail_di",
     "definition": "当日退款状态订单的金额合计",
     "logic": "SUM(amount) WHERE pay_status=2 AND dt='{date}'"},
    {"metric_id": "M010", "metric_name": "动销商品数",
     "aliases": ["在售商品数", "动销SKU数", "动销商品"],
     "table": "dwd_order_detail_di",
     "definition": "当日有支付成功订单的去重商品数",
     "logic": "COUNT(DISTINCT goods_id) WHERE pay_status=1 AND dt='{date}'"},
    {"metric_id": "M011", "metric_name": "支付用户数",
     "aliases": ["成交用户数", "付费用户数", "下单用户数"],
     "table": "dwd_order_detail_di",
     "definition": "当日有支付成功订单的去重用户数",
     "logic": "COUNT(DISTINCT user_id) WHERE pay_status=1 AND dt='{date}'"},
    {"metric_id": "M012", "metric_name": "退货率",
     "aliases": ["退款率", "退货比例", "退款比例"],
     "table": "dwd_order_detail_di",
     "definition": "当日退款订单数占支付订单数的比例（百分比）",
     "logic": "ROUND(SUM(pay_status=2)/SUM(pay_status=1)*100,2) WHERE dt='{date}'"},
    {"metric_id": "M013", "metric_name": "广告花费",
     "aliases": ["投放花费", "广告消耗", "推广费用", "推广花费", "广告费", "投放成本"],
     "table": "dwd_campaign_daily",
     "definition": "当日投放计划的花费合计",
     "logic": "SUM(cost) WHERE dt='{date}'"},
    {"metric_id": "M014", "metric_name": "广告ROI",
     "aliases": ["投放ROI", "投入产出比", "ROAS", "广告ROI", "广告投产比", "投产比", "投放回报"],
     "table": "dwd_campaign_daily",
     "definition": "当日成交金额除以投放花费",
     "logic": "ROUND(SUM(gmv)/NULLIF(SUM(cost),0),2) WHERE dt='{date}'"},
    {"metric_id": "M015", "metric_name": "退款订单数",
     "aliases": ["退款单数", "售后单数", "退款笔数", "退款订单量", "退货单数"],
     "table": "dwd_refund_detail_di",
     "definition": "当日退款完成状态的去重订单数",
     "logic": "COUNT(DISTINCT order_id) WHERE refund_status=1 AND dt='{date}'"},
]

_IGNORED_NAME_PARTS = {"dwd", "dim", "di", "df", "ods", "dws"}


def _table_match(table: dict, q: str) -> bool:
    """表名或核心片段命中：支持"order_detail表"这类用户口语引用。"""
    name = table["table_name"].lower()
    if name in q:
        return True
    return any(p in q for p in name.split("_")
               if len(p) >= 4 and p not in _IGNORED_NAME_PARTS)


def _caliber_match(caliber: dict, q: str) -> bool:
    """口径匹配：全名、别名，以及去掉"数/率/量"后缀的词根（如"支付订单"）。

    `q` 是**已转小写**的问题，所以别名也要转小写再比：否则 `GMV`/`DAU`/`ROAS`/`广告ROI`
    这些带大写的别名永远匹配不上（用户输入会被统一成小写），等于白配。
    """
    for name in [caliber["metric_name"]] + caliber.get("aliases", []):
        lowered = name.lower()
        if lowered in q:
            return True
        stem = lowered.rstrip("数率量")
        if len(stem) >= 3 and stem in q:
            return True
    return False


_MIN_CN_COL = 2      # 中文字段名：两字起（如"省份""预算"）
_MIN_EN_COL = 4      # 英文列名：短名（dt/id/qty）噪音太大，宁可放过
# 中文复合词的中心语在后（计划预算→预算、商品品类→品类），但通用后缀不能当信号，
# 否则「商品名称」「订单编号」会把整库的表都拉进来
_GENERIC_CN_SUFFIX = {"名称", "编号", "状态", "日期", "时间", "金额", "数量", "类型",
                      "标记", "备注", "描述"}


def _cn_forms(cn: str) -> list[str]:
    """字段中文名的可匹配形式：全名 + 长度≥2 的中心语后缀（通用后缀除外）。"""
    forms = [cn]
    for i in range(2, max(len(cn) - 1, 2)):
        tail = cn[i:]
        if len(tail) >= 2 and tail not in _GENERIC_CN_SUFFIX and tail not in forms:
            forms.append(tail)
    return forms


def _column_hits(question: str, tables: list[dict]) -> dict[str, list[str]]:
    """字段级「明确命中」：字段中文名全名，或英文列名整词出现。

    只认这两种：**表补位用它**。「工单数量统计」里的「单数」能碎片命中「支付订单数」的
    中心语，若拿这种命中去补表，一个本该 E002 拒答的问题会静默查到订单表——宁可少召回。

    英文按整词匹配（`(?<![a-z0-9_])`），否则 `amount` 会命中 `total_amount`。
    表内只保留匹配最长的那批，避免「支付订单数」与「退款订单数」同时被推荐。
    """
    q = (question or "").lower()
    hits: dict[str, list[str]] = {}
    if not q:
        return hits
    for t in tables:
        matched: dict[str, int] = {}
        for cm in t.get("column_meta") or []:
            name = cm.get("name") or ""
            if not name:
                continue
            cn = (cm.get("cn_name") or "").strip()
            en = name.lower()
            if cn and len(cn) >= _MIN_CN_COL and cn.lower() in q:
                matched[name] = len(cn)
            elif len(en) >= _MIN_EN_COL and re.search(
                    rf"(?<![a-z0-9_]){re.escape(en)}(?![a-z0-9_])", q):
                matched[name] = len(en)
        if matched:
            longest = max(matched.values())
            hits[t["table_name"]] = [n for n, ln in matched.items() if ln == longest]
    return hits


def _derived_column_hits(question: str, tables: list[dict]) -> dict[str, list[str]]:
    """字段级「中心语命中」：用户说「预算」而字段叫「计划预算」。

    只在**已召回的表**内做字段提示，**不参与表补位**：中文碎片太容易误伤
    （「工单数量」→「单数」→ 支付订单数），补表必须用明确命中。
    """
    q = (question or "").lower()
    hits: dict[str, list[str]] = {}
    if not q:
        return hits
    for t in tables:
        matched: dict[str, int] = {}
        for cm in t.get("column_meta") or []:
            name = cm.get("name") or ""
            cn = (cm.get("cn_name") or "").strip()
            if not name or not cn:
                continue
            # 全名命中要一起参与「最长优先」比较，否则「支付订单数」与「退款订单数」
            # 在「支付订单数是多少」里会并列命中（后者靠中心语「订单数」）
            candidates = [len(cn)] if cn.lower() in q else []
            candidates += [len(f) for f in _cn_forms(cn)[1:]
                           if len(f) >= _MIN_CN_COL and f.lower() in q]
            best = max(candidates, default=0)
            if best:
                matched[name] = best
        if matched:
            longest = max(matched.values())
            hits[t["table_name"]] = [n for n, ln in matched.items() if ln == longest]
    return hits


class RecallService:
    async def recall(self, question: str, session_id: str | None = None) -> RecallResult:
        q = question.lower()
        all_tables, all_calibers = metadata_store.tables(), metadata_store.calibers()
        tables = [t for t in all_tables
                  if _table_match(t, q) or any(k.lower() in q for k in t.get("keywords", []))]
        calibers = [c for c in all_calibers if _caliber_match(c, q)]
        # 向量路（A05）：关键词未命中的按语义相似度补位；未启用/无编码器时为空。
        # `RECALL_DEGRADED_MODE=force_keyword` 是回滚开关：向量召回效果不及预期时，
        # 不用改代码也不用重启配置中心，只切这一项就能立刻退回纯关键词。
        # 必须带超时：首次编码或上游 embedding 抖动时，宁可只走关键词路，也不能让
        # 整个召回卡住——用户等的是数据，不是「更聪明的召回」。
        if settings.recall_degraded_mode == "force_keyword":
            vec_tables, vec_calibers, vec_columns = [], [], []
        else:
            try:
                vec_tables, vec_calibers, vec_columns = await asyncio.wait_for(
                    vector_recall.search(question, all_tables, all_calibers,
                                         flatten_columns(all_tables)),
                    timeout=settings.vector_timeout)
            except asyncio.TimeoutError:
                logger.warning("向量召回超时（%ss），本轮仅用关键词路", settings.vector_timeout)
                vec_tables, vec_calibers, vec_columns = [], [], []
            except Exception as exc:  # noqa: BLE001 —— 向量路是补位，失败不影响主链路
                logger.warning("向量召回失败，本轮仅用关键词路：%s", exc)
                vec_tables, vec_calibers, vec_columns = [], [], []
        # 口径依赖的表必须进入召回：如下单口径需要订单明细表，否则生成阶段拿不到字段
        by_name = {t["table_name"]: t for t in tables}
        seed_by_name = {t["table_name"]: t for t in all_tables}
        seen = {c["metric_id"] for c in calibers}
        calibers += [c for c in vec_calibers if c["metric_id"] not in seen]
        for c in calibers:
            name = c.get("table")
            if name and name not in by_name and name in seed_by_name:
                by_name[name] = seed_by_name[name]
        for t in vec_tables:
            by_name.setdefault(t["table_name"], t)
        # 字段级：明确命中（可补表）+ 中心语命中（只提示），再叠加向量补位字段
        exact_hits = _column_hits(question, all_tables)
        # 字段名是比表关键词更细的信号：明确命中字段所属表即使表名/关键词没命中也要补进来
        for table_name in exact_hits:
            if table_name not in by_name and table_name in seed_by_name:
                by_name[table_name] = seed_by_name[table_name]
        for c in vec_columns:
            table_name, col_name = c.get("table_name"), c.get("name")
            if not table_name or not col_name:
                continue
            if table_name not in by_name and table_name in seed_by_name:
                by_name[table_name] = seed_by_name[table_name]
            exact_hits.setdefault(table_name, [])
            if col_name not in exact_hits[table_name]:
                exact_hits[table_name].append(col_name)
        # 拷贝后再挂命中字段：元数据是全局缓存对象，直接改会把状态串到下个请求
        tables = []
        column_hits: dict[str, list[str]] = {}
        for t in by_name.values():
            name = t["table_name"]
            derived = _derived_column_hits(question, [t]).get(name, [])
            merged = list(dict.fromkeys(exact_hits.get(name, []) + derived))
            if merged:
                column_hits[name] = merged
            tables.append(dict(t, hit_columns=merged))
        # 新媒体行业知识库（术语/指标定义）
        from app.services.knowledge_base import knowledge_base
        knowledge = knowledge_base.match(question)
        # 会话内已解析的上传文件表（F27）
        from app.services.file_store import file_store
        tables = self._file_tables(q, session_id, file_store) + tables
        return RecallResult(tables=tables, calibers=calibers, knowledge=knowledge,
                            column_hits=column_hits)

    @staticmethod
    def _file_tables(q: str, session_id: str | None, store) -> list[dict]:
        if not session_id:
            return []
        parsed = store.parsed_files(session_id)
        if not parsed:
            return []
        hits = []
        for f in parsed:
            p = f["parsed"]
            stem = Path(f["name"]).stem.lower()
            matched = bool(stem) and stem in q
            # 会话内仅一个已解析文件时，"这个文件/表里"等指代默认命中
            if not matched and len(parsed) == 1 and any(
                    k in q for k in ("文件", "表格", "上传", "附件", "这份", "这个表", "表里")):
                matched = True
            if not matched:
                continue
            tables_meta = p.get("tables") or [p]
            multi = len(tables_meta) > 1
            for tm in tables_meta:
                desc = (f"用户上传文件「{f['name']}」"
                        + (f" · 工作表「{tm.get('sheet') or 'Sheet1'}」" if multi else "")
                        + f"（{tm['row_count']} 行 × {tm['col_count']} 列）")
                hits.append({
                    "table_name": tm["table"],
                    "schema": tm.get("schema") or p.get("schema"),
                    "description": desc,
                    "keywords": [],
                    "columns": tm["columns"],
                    "column_labels": tm["labels"],
                })
        return hits


recall_service = RecallService()
