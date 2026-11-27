"""量化后的召回排序一致性评测：fp32 IR vs INT8 IR。

用 50 条业务问句对 30 条候选文档（指标口径/表/维度/术语）做 embedding 检索，
比较两种模型的 Top-1/Top-5 一致率——量化不能只看向量余弦，还要看排序是否稳定。
"""
import argparse
from pathlib import Path

import numpy as np

DOCS = [
    "支付订单数：当日支付成功且未退款的订单去重数",
    "日活跃用户数：当日有登录行为的用户去重数",
    "新增用户（注册口径）：当日完成注册的新用户数，按注册日期统计",
    "新增用户（下单口径）：当日有支付成功订单的去重用户数，按下单行为统计",
    "销售额：支付成功订单的金额合计",
    "GMV：成交总额，通常等于支付成功订单金额合计",
    "客单价：销售额除以支付订单数",
    "复购率：重复购买用户数占购买用户数的比例",
    "转化率：支付用户数除以访问用户数",
    "退款率：退款订单数除以支付订单数",
    "退货率：退货订单数除以支付订单数",
    "完播率：完整播放次数除以播放次数",
    "涨粉率：新增粉丝数除以期初粉丝数",
    "互动率：点赞、评论、转发、收藏之和除以曝光量",
    "点击率：点击量除以曝光量",
    "千次曝光成本：广告消耗除以曝光量再乘以一千",
    "广告投放 ROI：广告带来的成交金额除以广告消耗",
    "平均停留时长：用户观看总时长除以观看人数",
    "峰值在线人数：直播期间同时在线的最高人数",
    "曝光量：内容或广告被展示的次数",
    "订单明细日报表 dwd_order_detail_di：订单、支付、退款、金额、渠道、品类",
    "用户行为日报表 dwd_user_di：注册、登录、活跃、新增用户",
    "渠道维表 dim_channel：渠道 ID 与渠道名称",
    "渠道维度：抖音、快手、小红书、视频号、淘宝、京东",
    "品类维度：服饰、美妆、食品、数码、家居",
    "城市维度：北京、上海、广州、深圳、杭州、成都",
    "时间维度：昨天、上周、本月、近 7 天、近 30 天",
    "直播间指标：观看人数、平均停留时长、峰值在线、互动率",
    "达人指标：粉丝数、涨粉、播放量、互动率",
    "优惠券：领取量、使用量、核销率",
]

QUERIES = [
    "昨天各渠道的支付订单数是多少", "上周新增用户数按品类拆分", "本月销售额环比增长多少",
    "近 7 天日活跃用户趋势", "上周各渠道的新增用户和 GMV 是多少", "查一下 2026-11-20 的支付订单数",
    "上月退款订单金额合计", "各城市订单量排名前十", "最近 30 天新客复购率", "本周广告投放 ROI 是多少",
    "按小时看昨天的下单量分布", "上周内容完播率是多少", "不同达人带来的 GMV 对比", "直播间平均停留时长",
    "近 14 天涨粉趋势", "各商品类目销售额占比", "昨天注册且当日下单的用户数", "本月客单价与上月对比",
    "渠道转化率从浏览到支付", "最近 7 天退货率",
    "完播率怎么算", "涨粉率怎么算", "GMV 是什么意思", "客单价怎么算", "复购率怎么算",
    "互动率怎么算", "点击率怎么算", "千次曝光成本怎么算", "ROI 怎么计算", "退款率怎么算",
    "平均停留时长怎么算", "峰值在线人数是什么意思", "曝光量是什么意思", "优惠券核销率怎么算",
    "订单明细日报表有哪些字段", "用户行为日报表包含什么", "渠道维表怎么关联订单表",
    "抖音渠道的销售额", "小红书渠道的转化率", "视频号直播间的互动率",
    "服饰品类的销售额占比", "美妆品类的复购率", "食品品类的退货率",
    "数码品类的客单价", "家居品类的订单量", "北京和上海的销售额对比",
    "深圳地区的新增用户", "杭州地区的客单价", "成都地区的转化率", "近 30 天的 GMV 趋势",
]


def load(ir_path: Path, tokenizer):
    import openvino as ov

    compiled = ov.Core().compile_model(str(ir_path), "CPU")
    out_port = compiled.output(0)
    input_names = [i.get_any_name() for i in compiled.inputs]

    def encode(texts, batch=16):
        vecs = []
        for i in range(0, len(texts), batch):
            part = texts[i:i + batch]
            enc = tokenizer(part, padding=True, truncation=True, max_length=128,
                            return_tensors="np")
            feed = {name: np.asarray(enc[name], dtype=np.int64)
                    for name in input_names if name in enc}
            hidden = np.asarray(compiled(feed)[out_port])[:, 0]
            vecs.append(hidden / (np.linalg.norm(hidden, axis=1, keepdims=True) + 1e-12))
        return np.vstack(vecs)

    return encode


def ranking(query_vecs, doc_vecs):
    sims = query_vecs @ doc_vecs.T
    return np.argsort(-sims, axis=1)


def evaluate(name, ref, other):
    q32, qx = ref(QUERIES), other(QUERIES)
    d32, dx = ref(DOCS), other(DOCS)
    r32, rx = ranking(q32, d32), ranking(qx, dx)
    top1 = float(np.mean(r32[:, 0] == rx[:, 0]))
    top5 = float(np.mean([len(set(r32[i, :5]) & set(rx[i, :5])) / 5 for i in range(len(QUERIES))]))
    cos_shift = float(np.mean(np.sum(q32 * qx, axis=1)))
    rank_shift = float(np.mean([abs(int(np.where(rx[i] == r32[i, 0])[0][0]))
                                for i in range(len(QUERIES))]))
    # 召回是否仍保住 fp32 的 Top-1：即使排序翻转，只要还在 Top-5 内，实际检索结果不变差
    top1_in_top5 = float(np.mean([r32[i, 0] in rx[i, :5] for i in range(len(QUERIES))]))
    flips = [{"query": QUERIES[i], "fp32": DOCS[r32[i, 0]][:24], "other": DOCS[rx[i, 0]][:24]}
             for i in range(len(QUERIES)) if r32[i, 0] != rx[i, 0]]
    return {"name": name, "top1": top1, "top5": top5, "cos": cos_shift, "rank": rank_shift,
            "top1_in_top5": top1_in_top5, "flips": flips}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="BAAI/bge-small-zh-v1.5")
    ap.add_argument("--cache-dir", default=str(Path(__file__).parent / ".cache"))
    ap.add_argument("--out", default=str(Path(__file__).parent / "recall_consistency.md"))
    args = ap.parse_args()

    from transformers import AutoTokenizer

    cache = Path(args.cache_dir)
    stem = args.model.replace("/", "__")
    tok = AutoTokenizer.from_pretrained(args.model)
    fp32 = load(cache / f"{stem}.xml", tok)
    variants = [(f"ov-{p.name[len(stem) + 1:-4]}", p) for p in sorted(cache.glob(f"{stem}.*.xml"))]

    results = []
    for name, path in variants:
        r = evaluate(name, fp32, load(path, tok))
        results.append(r)
        print(f"[{name}] Top-1 一致率：{r['top1']:.2%}  Top-5 重合率：{r['top5']:.2%}  "
              f"fp32 Top-1 仍在 Top-5：{r['top1_in_top5']:.2%}  余弦：{r['cos']:.4f}  "
              f"平均位次：{r['rank']:.2f}")
        if name == "ov-int8":
            print("  Top-1 翻转明细：")
            for f in r["flips"]:
                print(f"  - {f['query']}：fp32→{f['fp32']} / int8→{f['other']}")

    lines = ["# 量化召回排序一致性", "",
             f"- 模型：`{args.model}`", f"- 查询：{len(QUERIES)} 条", f"- 候选文档：{len(DOCS)} 条", "",
             "| 变体 | Top-1 一致率 | Top-5 重合率 | fp32 Top-1 仍在 Top-5 | 查询向量余弦 | fp32 Top-1 平均位次 |",
             "|---|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r['name']} | {r['top1']:.2%} | {r['top5']:.2%} | {r['top1_in_top5']:.2%} | "
                     f"{r['cos']:.4f} | {r['rank']:.2f} |")
    lines += ["",
              "> 判定：Top-5 重合率 ≥ 95% 且 Top-1 一致率 ≥ 90% 时，量化版本可用于在线 query embedding；否则改用 fp16 或继续扩校准集。"]
    Path(args.out).write_text("\n".join(lines), encoding="utf-8")
    print("结果已写入", args.out)


if __name__ == "__main__":
    main()
