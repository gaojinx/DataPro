"""把 embedding 模型转 OpenVINO IR 并做 INT8 量化（NNCF PTQ）。

用法：
  python quantize_embedding.py --model BAAI/bge-small-zh-v1.5
  python quantize_embedding.py --model BAAI/bge-small-zh-v1.5 --preset mixed --fp16
  python quantize_embedding.py --model BAAI/bge-m3          # 需要更多内存与时间

产物：`.cache/<model>.xml/.bin`（fp32 IR）、`.cache/<model>.int8.xml/.bin`（INT8）、
`.cache/<model>.fp16.xml/.bin`（fp16，仅在传 --fp16 时生成）
"""
import argparse
from pathlib import Path

import numpy as np

TEXTS = [
    "昨天各渠道的支付订单数是多少", "上周新增用户数按品类拆分", "本月销售额环比增长多少",
    "近 7 天日活跃用户趋势", "上周各渠道的新增用户和 GMV 是多少", "查一下 2026-11-20 的支付订单数",
    "上月退款订单金额合计", "各城市订单量排名前十", "最近 30 天新客复购率", "本周广告投放 ROI 是多少",
    "按小时看昨天的下单量分布", "上周内容完播率是多少", "不同达人带来的 GMV 对比", "直播间平均停留时长",
    "近 14 天涨粉趋势", "各商品类目销售额占比", "昨天注册且当日下单的用户数", "本月客单价与上月对比",
    "渠道转化率从浏览到支付", "最近 7 天退货率",
]

# 量化校准语料：业务问句 + 口径定义 + 维度/术语，覆盖短句与长句
_DEFS = [
    "支付订单数：当日支付成功且未退款的订单去重数",
    "新增用户（注册口径）：当日完成注册的新用户数，按注册日期统计",
    "新增用户（下单口径）：当日有支付成功订单的去重用户数，按下单行为统计",
    "日活跃用户数：当日有登录行为的用户去重数",
    "销售额：支付成功订单的金额合计", "GMV：成交总额，通常等于支付成功订单金额合计",
    "客单价：销售额除以支付订单数", "复购率：重复购买用户数占购买用户数的比例",
    "转化率：支付用户数除以访问用户数", "退款率：退款订单数除以支付订单数",
    "退货率：退货订单数除以支付订单数", "完播率：完整播放次数除以播放次数",
    "涨粉率：新增粉丝数除以期初粉丝数", "互动率：点赞、评论、转发、收藏之和除以曝光量",
    "点击率：点击量除以曝光量", "千次曝光成本：广告消耗除以曝光量再乘以一千",
    "广告投放 ROI：广告带来的成交金额除以广告消耗", "平均停留时长：用户观看总时长除以观看人数",
    "峰值在线人数：直播期间同时在线的最高人数", "曝光量：内容或广告被展示的次数",
    "优惠券核销率：核销数量除以领取数量",
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

_METRICS = ["支付订单数", "销售额", "GMV", "新增用户", "日活跃用户数", "客单价", "复购率",
            "转化率", "退款率", "退货率", "完播率", "涨粉率", "互动率", "点击率",
            "千次曝光成本", "广告投放 ROI", "平均停留时长", "峰值在线人数", "曝光量", "优惠券核销率"]
_TIMES = ["昨天", "上周", "本月", "上月", "近 7 天", "近 30 天", "今天", "最近 14 天"]
_DIMS = ["各渠道", "各品类", "各城市", "按小时", "按达人", "按账号"]
_SCENES = ["抖音直播间", "小红书笔记", "视频号直播", "快手短视频", "淘宝店铺"]


def build_calib(multiplier: int = 2) -> list[str]:
    """组合生成校准语料：指标 × 时间 × 维度 × 句式，保证长度与表达分布接近真实查询。"""
    texts = list(TEXTS) + list(_DEFS)
    patterns = ["{t}{d}的{m}是多少", "查一下{t}{d}的{m}", "{t}{d}的{m}按天看趋势",
                "请统计{t}{d}的{m}并排序", "{m}怎么算", "{m}的口径是什么"]
    for i, metric in enumerate(_METRICS):
        for j in range(multiplier):
            t = _TIMES[(i + j) % len(_TIMES)]
            d = _DIMS[(i + j * 2) % len(_DIMS)]
            scene = _SCENES[(i + j) % len(_SCENES)]
            texts.append(patterns[(i + j) % len(patterns)].format(t=t, d=d, m=metric))
            texts.append(f"{scene}{t}的{metric}是多少")
    return list(dict.fromkeys(texts))


CALIB = build_calib()


def ensure_ir(model_id: str, cache_dir: Path) -> Path:
    import openvino as ov
    from transformers import AutoModel, AutoTokenizer

    ir = cache_dir / (model_id.replace("/", "__") + ".xml")
    if ir.exists():
        return ir
    tok = AutoTokenizer.from_pretrained(model_id)
    model = AutoModel.from_pretrained(model_id).eval()
    example = tok(["示例文本"], padding=True, truncation=True, max_length=64,
                  return_tensors="pt")
    ov_model = ov.convert_model(model, example_input=dict(example))
    cache_dir.mkdir(parents=True, exist_ok=True)
    ov.save_model(ov_model, str(ir))
    return ir


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="BAAI/bge-small-zh-v1.5")
    ap.add_argument("--cache-dir", default=str(Path(__file__).parent / ".cache"))
    ap.add_argument("--max-length", type=int, default=128)
    ap.add_argument("--preset", choices=["performance", "mixed"], default="mixed",
                    help="NNCF 量化 preset；mixed 对激活值更保守，通常精度更好")
    ap.add_argument("--fp16", action="store_true", help="同时导出 fp16 IR 作为对照")
    ap.add_argument("--weights-only", action="store_true",
                    help="额外做仅权重 INT8 压缩（不做激活量化，精度通常接近无损）")
    ap.add_argument("--skip-int8", action="store_true",
                    help="跳过全量 INT8 PTQ（大模型可只做 fp16 与仅权重压缩）")
    args = ap.parse_args()

    import nncf
    import openvino as ov
    from transformers import AutoTokenizer

    cache = Path(args.cache_dir)
    ir_path = ensure_ir(args.model, cache)
    stem = args.model.replace("/", "__")

    tok = AutoTokenizer.from_pretrained(args.model)
    model = ov.Core().read_model(str(ir_path))
    input_names = [i.get_any_name() for i in model.inputs]
    print("IR 输入：", input_names)

    items = []
    if not args.skip_int8:
        for text in CALIB:
            # padding=True 与线上推理一致；max_length 加大以覆盖长句激活范围
            enc = tok(text, padding=True, max_length=args.max_length,
                      truncation=True, return_tensors="np")
            items.append({name: np.asarray(enc[name], dtype=np.int64)
                          for name in input_names if name in enc})
        dataset = nncf.Dataset(items)
        preset = (nncf.QuantizationPreset.MIXED if args.preset == "mixed"
                  else nncf.QuantizationPreset.PERFORMANCE)
        print(f"校准样本：{len(items)} 条，preset={args.preset}，开始 INT8 量化……")
        quantized = nncf.quantize(model, dataset, preset=preset,
                                  subset_size=min(300, len(items)))
        out_path = cache / f"{stem}.int8.xml"
        ov.save_model(quantized, str(out_path))
        print("INT8 模型已保存：", out_path)

    if args.fp16:
        fp16_path = cache / f"{stem}.fp16.xml"
        ov.save_model(model, str(fp16_path), compress_to_fp16=True)
        print("fp16 模型已保存：", fp16_path)

    if args.weights_only:
        w_path = cache / f"{stem}.int8w.xml"
        compressed = nncf.compress_weights(model, mode=nncf.CompressWeightsMode.INT8_ASYM)
        ov.save_model(compressed, str(w_path))
        print("仅权重 INT8 模型已保存：", w_path)


if __name__ == "__main__":
    main()
