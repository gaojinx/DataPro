"""OpenVINO embedding 加速 POC（独立脚本，不接入主链路）。

对比同一模型在 CPU 上的两种推理方式：
  1) PyTorch（transformers，基线）
  2) OpenVINO IR（ov.convert_model 直接转换 PyTorch 模型）

用法（在 POC 的 venv 中执行）：
  python bench_embedding.py --backend both --batch 1 8 32 --repeat 5
  set HF_ENDPOINT=https://hf-mirror.com   # 国内网络下载模型时使用

输出：终端对比表 + `results.md`（含延迟、吞吐、两种后端向量一致性）。
"""
import argparse
import json
import os
import statistics
import time
from pathlib import Path

TEXTS = [
    "昨天各渠道的支付订单数是多少",
    "上周新增用户数按品类拆分",
    "本月销售额环比增长多少",
    "近 7 天日活跃用户趋势",
    "上周各渠道的新增用户和 GMV 是多少",
    "查一下 2026-11-20 的支付订单数",
    "上月退款订单金额合计",
    "各城市订单量排名前十",
    "最近 30 天新客复购率",
    "本周广告投放 ROI 是多少",
    "按小时看昨天的下单量分布",
    "上周内容完播率是多少",
    "不同达人带来的 GMV 对比",
    "直播间平均停留时长",
    "近 14 天涨粉趋势",
    "各商品类目销售额占比",
    "昨天注册且当日下单的用户数",
    "本月客单价与上月对比",
    "渠道转化率从浏览到支付",
    "最近 7 天退货率",
]


def _normalize(mat):
    import numpy as np
    return mat / (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-12)


def build_torch(model_id: str):
    import torch
    from transformers import AutoModel, AutoTokenizer

    torch.set_num_threads(os.cpu_count() or 4)
    tok = AutoTokenizer.from_pretrained(model_id)
    model = AutoModel.from_pretrained(model_id).eval()

    def encode(texts):
        inputs = tok(texts, padding=True, truncation=True, max_length=128,
                     return_tensors="pt")
        with torch.no_grad():
            hidden = model(**inputs).last_hidden_state[:, 0]
            vec = torch.nn.functional.normalize(hidden, dim=-1)
        return vec.numpy()

    return tok, model, encode


def build_openvino(model_id: str, cache_dir: str):
    import numpy as np
    import openvino as ov
    from transformers import AutoTokenizer

    tok, model, _ = build_torch(model_id)
    example = tok(["示例文本"], padding=True, truncation=True, max_length=128,
                  return_tensors="pt")
    core = ov.Core()
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    ir_path = cache / (model_id.replace("/", "__") + ".xml")

    if ir_path.exists():
        ov_model = core.read_model(str(ir_path))
    else:
        ov_model = ov.convert_model(model, example_input=dict(example))
        ov.save_model(ov_model, str(ir_path))
    compiled = core.compile_model(ov_model, "CPU")
    out_port = compiled.output(0)

    def encode(texts):
        inputs = tok(texts, padding=True, truncation=True, max_length=128,
                     return_tensors="pt")
        feed = {k: v.numpy() for k, v in inputs.items()}
        hidden = np.asarray(compiled(feed)[out_port])[:, 0]
        return _normalize(hidden)

    return tok, compiled, encode


def measure(encode, texts, batch: int, repeat: int) -> dict:
    batches = [texts[i:i + batch] for i in range(0, len(texts), batch)]
    for _ in range(2):                      # warmup
        for b in batches:
            encode(b)
    samples = []
    started = time.perf_counter()
    for _ in range(repeat):
        for b in batches:
            t0 = time.perf_counter()
            encode(b)
            samples.append((time.perf_counter() - t0) * 1000)
    total = time.perf_counter() - started
    samples.sort()
    return {
        "batch": batch,
        "repeat": repeat,
        "texts": len(texts),
        "p50_ms": round(statistics.median(samples), 2),
        "p95_ms": round(samples[int(len(samples) * 0.95) - 1], 2),
        "mean_ms": round(statistics.fmean(samples), 2),
        "texts_per_sec": round(len(texts) * repeat / total, 2),
    }


def cosine(a, b) -> float:
    import numpy as np
    return float(np.mean(np.sum(a * b, axis=1)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="BAAI/bge-small-zh-v1.5")
    ap.add_argument("--backend", choices=["torch", "ov", "both"], default="both")
    ap.add_argument("--batch", type=int, nargs="+", default=[1, 8, 32])
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--cache-dir", default=str(Path(__file__).parent / ".cache"))
    ap.add_argument("--out", default=str(Path(__file__).parent / "results.md"))
    args = ap.parse_args()

    encoders, meta = {}, {"model": args.model, "cpu": os.cpu_count()}
    if args.backend in ("torch", "both"):
        _, _, encoders["torch"] = build_torch(args.model)
    if args.backend in ("ov", "both"):
        _, _, encoders["ov"] = build_openvino(args.model, args.cache_dir)

    rows = []
    for name, encode in encoders.items():
        for batch in args.batch:
            row = measure(encode, TEXTS, batch, args.repeat)
            row["backend"] = name
            rows.append(row)
            print(f"[{name}] batch={batch} p50={row['p50_ms']}ms "
                  f"p95={row['p95_ms']}ms qps={row['texts_per_sec']} texts/s")

    if len(encoders) == 2:
        sample = TEXTS[:8]
        sim = cosine(encoders["torch"](sample), encoders["ov"](sample))
        meta["cosine_torch_vs_ov"] = round(sim, 6)
        print(f"向量一致性（torch vs ov，8 条均值余弦）：{sim:.6f}")

    speedup = {}
    for batch in args.batch:
        t = next((r for r in rows if r["backend"] == "torch" and r["batch"] == batch), None)
        o = next((r for r in rows if r["backend"] == "ov" and r["batch"] == batch), None)
        if t and o:
            speedup[batch] = round(t["p50_ms"] / o["p50_ms"], 2)
    if speedup:
        meta["speedup_p50"] = speedup
        print("OpenVINO 相对 PyTorch 的 p50 加速比：", speedup)

    lines = ["# OpenVINO embedding POC 结果", "",
             f"- 模型：`{args.model}`",
             f"- CPU 逻辑核：{meta['cpu']}",
             f"- 文本数：{len(TEXTS)}，每个 batch 重复 {args.repeat} 次", "",
             "| 后端 | batch | p50 (ms) | p95 (ms) | texts/s |",
             "|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['backend']} | {r['batch']} | {r['p50_ms']} | {r['p95_ms']} | {r['texts_per_sec']} |")
    if "cosine_torch_vs_ov" in meta:
        lines += ["", f"向量一致性（8 条均值余弦）：`{meta['cosine_torch_vs_ov']}`"]
    if speedup:
        lines += ["", f"p50 加速比（torch/ov）：`{json.dumps(speedup, ensure_ascii=False)}`"]
    lines += ["", "> POC 仅评估推理性能；是否接入 A05 向量召回需另做召回质量评测。"]
    Path(args.out).write_text("\n".join(lines), encoding="utf-8")
    print("结果已写入", args.out)


if __name__ == "__main__":
    main()
